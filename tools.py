import os
import re
import sys
import subprocess
import shutil
from pathlib import Path
from config import WORKSPACE_DIR

_WORKSPACE_ROOT = Path(WORKSPACE_DIR).resolve()

# #40 批处理模式标志（由 agent.set_batch_mode() 设置）
_BATCH_MODE = False
# 严格模式：批处理下仍然拒绝 Level-3 需确认命令（pip/npm install、git checkout/reset）
_BATCH_STRICT = os.getenv("YANSH_BATCH_STRICT", "").lower() in ("1", "true", "yes")


def set_batch_mode(enabled: bool, strict: bool | None = None):
    global _BATCH_MODE, _BATCH_STRICT
    _BATCH_MODE = enabled
    if strict is not None:
        _BATCH_STRICT = strict


def _con():
    """返回 Console 实例；批处理/JSON 模式下输出到 stderr"""
    from rich.console import Console
    import sys
    return Console(file=sys.stderr) if _BATCH_MODE else Console()

_DANGEROUS_PATTERNS = [
    (r"rm\s+(-[a-zA-Z]*r[a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*r|-f\b)", "rm -rf / rm -f"),
    (r"\bsudo\b",                                                  "sudo"),
    (r"(curl|wget)\s+.*\|\s*(ba)?sh",                             "curl/wget | sh"),
    (r"chmod\s+(-R\s+)?777",                                       "chmod 777"),
    (r"\bmkfs\b",                                                  "mkfs"),
    (r"\bdd\s+if=",                                               "dd if="),
    (r":\(\)\s*\{.*:\|:.*\}",                                     "fork bomb"),
    # Windows 危险命令
    (r"\brd\s+/s",                                                "rd /s /q"),
    (r"\brmdir\s+/s",                                             "rmdir /s"),
    (r"\bdel\s+(/[a-zA-Z]+\s+)+",                                "del /f /s /q"),
    (r"\bformat\s+[a-zA-Z]:",                                     "format c:"),
    (r"\breg\s+delete\b",                                         "reg delete"),
    (r"\bbcdedit\b",                                              "bcdedit"),
    (r"\bshutdown\s+/[rs]\b",                                     "shutdown /r|/s"),
    (r"\btaskkill\s+/f\b",                                        "taskkill /f"),
    (r"\bnetsh\s+.*firewall\b",                                   "netsh firewall"),
    (r"\bpowershell\b.*-e(nc)?\b",                               "powershell -enc"),
    (r"\biex\b|\bInvoke-Expression\b",                            "iex/Invoke-Expression"),
    # #39 新增 deny
    (r"\bpython\s+-c\b",                                          "python -c (内联执行)"),
    (r"\bfind\b.*-delete\b",                                      "find -delete"),
    (r"\bgit\s+clean\b.*-f\b",                                    "git clean -f"),
    (r"\brm\s+-r\b",                                              "rm -r"),
    (r"\bsh\s+-c\b",                                              "sh -c"),
]

# 直接执行，无需确认
_SAFE_PATTERNS = [
    r"^pytest(\s|$)",
    r"^python\s+-m\s+pytest\b",
    r"^ruff\s+(check|format)\b",
    r"^mypy\b",
    r"^npm\s+test(\s|$)",
    r"^npm\s+run\s+lint\b",
    r"^go\s+test\b",
    r"^cargo\s+test\b",
    r"^(ls|dir)(\s|$)",
    r"^(cat|type)\s+\S",
    r"^echo\b",
    r"^python\s+\S+\.py(\s|$)",
]

# 执行前需用户确认
_CONFIRM_PATTERNS = [
    (r"^pip\s+(install|uninstall)\b",  "pip install/uninstall"),
    (r"^npm\s+install\b",              "npm install"),
    (r"^git\s+checkout\b",             "git checkout"),
    (r"^git\s+reset\b",                "git reset"),
]

def _validate_path(filename):
    """校验 filename 是否合法（非绝对路径、非越界、无符号链接逃逸）。
    返回 (resolved_path, None) 或 (None, error_dict)。"""
    p = Path(filename)
    if p.is_absolute():
        return None, {"error": "路径越界：不允许访问workspace外的文件"}
    if ".." in p.parts:
        return None, {"error": "路径越界：不允许访问workspace外的文件"}
    candidate = (_WORKSPACE_ROOT / p).resolve()
    if not candidate.is_relative_to(_WORKSPACE_ROOT):
        return None, {"error": "路径越界：不允许访问workspace外的文件"}
    return candidate, None

def _check_dangerous(command):
    """检查命令是否包含危险模式。返回 None 表示安全，否则返回 error_dict。"""
    for pattern, label in _DANGEROUS_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE | re.DOTALL):
            _con().print(f"[安全拦截] 检测到危险命令：{label}", highlight=False)
            return {"error": f"安全拦截：检测到危险命令（{label}），已阻止执行",
                    "returncode": -2, "stdout": "", "stderr": ""}
    return None

def write_file(filename, content):
    """在workspace目录下写入文件"""
    resolved, err = _validate_path(filename)
    if err:
        return err
    os.makedirs(WORKSPACE_DIR, exist_ok=True)
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding='utf-8')
        return {"success": f"文件 {filename} 写入成功"}
    except Exception as e:
        return {"error": str(e)}

def read_file(filename):
    """读取workspace目录下的文件"""
    resolved, err = _validate_path(filename)
    if err:
        return err
    try:
        return {"content": resolved.read_text(encoding='utf-8')}
    except Exception as e:
        return {"error": str(e)}

def execute_command(command):
    """在workspace目录下执行命令，30秒超时，三级命令策略（deny/safe/confirm）"""
    # Level 1: deny
    danger = _check_dangerous(command)
    if danger:
        return danger

    cmd_stripped = command.strip()

    # Level 2: safe — 直接执行
    is_safe = any(re.match(p, cmd_stripped, re.IGNORECASE) for p in _SAFE_PATTERNS)

    # Level 3: confirm — 批处理模式默认自动确认，strict 下拒绝；交互模式提示用户
    if not is_safe:
        for pattern, label in _CONFIRM_PATTERNS:
            if re.search(pattern, cmd_stripped, re.IGNORECASE):
                if _BATCH_MODE and _BATCH_STRICT:
                    _con().print(f"[batch-strict] 拒绝执行需确认命令: {label}", highlight=False)
                    return {"error": f"批处理严格模式拒绝执行: {label}", "returncode": -1, "stdout": "", "stderr": ""}
                if _BATCH_MODE:
                    _con().print(f"[batch] 自动确认执行: {command}", highlight=False)
                else:
                    _c = _con()
                    _c.print(f"[确认] 即将执行: {command}", highlight=False)
                    try:
                        answer = _c.input("继续？(y/n) ").strip().lower()
                    except EOFError:
                        answer = "n"
                    if answer != "y":
                        return {"error": "用户取消执行", "returncode": -1, "stdout": "", "stderr": ""}
                break

    import threading

    try:
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        process = subprocess.Popen(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=WORKSPACE_DIR,
            env=env,
        )

        stdout_lines = []
        stderr_lines = []

        def _read_stdout():
            for line in process.stdout:
                # batch 模式下 stdout 保留给 --json 输出，实时打印走 stderr
                print(line, end='', flush=True, file=sys.stderr if _BATCH_MODE else sys.stdout)
                stdout_lines.append(line)

        def _read_stderr():
            for line in process.stderr:
                stderr_lines.append(line)

        t_out = threading.Thread(target=_read_stdout, daemon=True)
        t_err = threading.Thread(target=_read_stderr, daemon=True)
        t_out.start()
        t_err.start()

        import interrupt
        import time
        start_time = time.time()
        try:
            while True:
                if interrupt.is_interrupted():
                    process.terminate()
                    process.wait(timeout=1)
                    raise interrupt.Interrupted()
                
                try:
                    process.wait(timeout=0.1)
                    break # Finished
                except subprocess.TimeoutExpired:
                    if time.time() - start_time > 30:
                        process.kill()
                        return {"error": "命令执行超时（30秒）"}
        except interrupt.Interrupted:
            raise
        except Exception as e:
            return {"error": str(e)}

        t_out.join()
        t_err.join()

        return {
            "stdout": ''.join(stdout_lines),
            "stderr": ''.join(stderr_lines),
            "returncode": process.returncode
        }
    except Exception as e:
        return {"error": str(e)}

def delete_file(filename):
    """删除workspace目录下的文件"""
    resolved, err = _validate_path(filename)
    if err:
        return err
    try:
        resolved.unlink()
        return {"success": f"文件 {filename} 删除成功"}
    except FileNotFoundError:
        return {"error": f"文件 {filename} 不存在"}
    except Exception as e:
        return {"error": str(e)}

def replace_in_file(filename, old_str, new_str):
    """在workspace文件中精确替换字符串。old_str必须唯一匹配，否则返回错误"""
    resolved, err = _validate_path(filename)
    if err:
        return err

    try:
        content = resolved.read_text(encoding='utf-8')
    except FileNotFoundError:
        return {"error": f"文件 {filename} 不存在"}
    except Exception as e:
        return {"error": str(e)}

    count = content.count(old_str)
    if count == 0:
        return {"error": f"在 {filename} 中未找到要替换的字符串"}
    if count > 1:
        return {"error": (
            f"在 {filename} 中找到 {count} 处匹配，需唯一匹配。"
            "请在 old_str 中增加上下文行（前后多带几行代码）以确保唯一；"
            "或改用 replace_symbol 按函数/类名整体替换。"
        )}

    content = content.replace(old_str, new_str, 1)
    try:
        resolved.write_text(content, encoding='utf-8')
        return {
            "success": f"文件 {filename} 替换成功",
            "filename": filename,
            "old_str": old_str,
            "new_str": new_str
        }
    except Exception as e:
        return {"error": str(e)}

def _get_ignore_spec():
    import pathspec
    gitignore_path = Path(WORKSPACE_DIR) / ".gitignore"
    if gitignore_path.exists():
        try:
            with open(gitignore_path, "r", encoding="utf-8") as f:
                return pathspec.PathSpec.from_lines("gitwildmatch", f)
        except Exception:
            return None
    return None

def list_files():
    """列出workspace目录下的所有文件（遵循.gitignore）"""
    os.makedirs(WORKSPACE_DIR, exist_ok=True)
    files = []
    spec = _get_ignore_spec()
    for root, dirs, filenames in os.walk(WORKSPACE_DIR):
        # 跳过 .git 目录
        if ".git" in root:
            continue
        for filename in filenames:
            rel_path = os.path.relpath(os.path.join(root, filename), WORKSPACE_DIR)
            # 统一使用正斜杠匹配
            if spec and spec.match_file(rel_path.replace("\\", "/")):
                continue
            files.append(rel_path)
    return {"files": files}
def move_file(src, dst):
    """移动文件从src到dst（相对于workspace）
    - 自动创建dst父目录
    - src不存在返回错误
    - 路径越界返回错误
    """
    src_path, err = _validate_path(src)
    if err:
        return err
    dst_path, err = _validate_path(dst)
    if err:
        return err

    if not src_path.exists():
        return {"error": f"Source file {src} does not exist"}
    
    # Create dst parent directory
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Move file
    try:
        shutil.move(str(src_path), str(dst_path))
        return {"success": f"File moved from {src} to {dst} successfully"}
    except Exception as e:
        return {"error": str(e)}

def search_in_files(pattern, workspace=None, regex=False, extensions=None):
    """在workspace目录下搜索文件内容
    
    Args:
        pattern: 搜索模式（字符串或正则）
        workspace: 搜索目录（默认使用WORKSPACE_DIR）
        regex: 是否使用正则表达式匹配
        extensions: 文件扩展名过滤列表（如 [".py", ".md"]）
    
    Returns:
        {"matches": [...], "total": int}
    """
    import re
    
    if workspace is None:
        workspace = Path(WORKSPACE_DIR)
    else:
        workspace = Path(workspace)
    
    # 路径安全检查
    abs_workspace = Path(WORKSPACE_DIR).resolve()
    abs_search_path = workspace.resolve()
    
    try:
        if not abs_search_path.is_relative_to(abs_workspace):
            return {"error": "Search path exceeds workspace directory"}
    except ValueError:
        return {"error": "Search path exceeds workspace directory"}
    
    matches = []
    spec = _get_ignore_spec()

    # 递归搜索所有文件
    for file_path in workspace.rglob("*"):
        # 跳过目录
        if file_path.is_dir():
            continue

        # 跳过 .git 目录
        if ".git" in file_path.parts:
            continue

        rel_path = os.path.relpath(file_path, abs_workspace).replace("\\", "/")
        if spec and spec.match_file(rel_path):
            continue
        # 扩展名过滤
        if extensions is not None:
            if file_path.suffix not in extensions:
                continue
        
        # 读取文件并搜索
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                for line_num, line in enumerate(f, start=1):
                    # 匹配逻辑
                    if regex:
                        if re.search(pattern, line):
                            rel_path = file_path.relative_to(workspace)
                            matches.append({
                                "file": str(rel_path).replace("\\", "/"),
                                "line": line_num,
                                "content": line.rstrip()
                            })
                    else:
                        if pattern in line:
                            rel_path = file_path.relative_to(workspace)
                            matches.append({
                                "file": str(rel_path).replace("\\", "/"),
                                "line": line_num,
                                "content": line.rstrip()
                            })
        except (UnicodeDecodeError, PermissionError):
            # 跳过二进制文件或无权限文件
            continue
    
    return {"matches": matches, "total": len(matches)}

def get_symbol_definition(symbol_name, file_path=None):
    """用 tree-sitter 精确查找函数或类定义，返回文件、行号、完整代码。
    file_path 可选；不填则搜索整个 workspace 的 .py 文件。"""
    try:
        import tree_sitter_python as tspython
        from tree_sitter import Language, Parser
    except ImportError:
        return {"error": "tree-sitter 未安装，请运行: pip install tree-sitter tree-sitter-python"}

    py_lang = Language(tspython.language())
    parser = Parser(py_lang)

    def _collect(node, src_bytes, parent_type=None):
        hits = []
        if node.type == "decorated_definition":
            for ch in node.children:
                if ch.type in ("function_definition", "class_definition"):
                    for grandch in ch.children:
                        if grandch.type == "identifier" and grandch.text.decode("utf-8") == symbol_name:
                            start_line = node.start_point[0] + 1
                            code = src_bytes[node.start_byte:node.end_byte].decode("utf-8")
                            hits.append({"line": start_line, "code": code})
                            break
                    break
        elif node.type in ("function_definition", "class_definition"):
            if parent_type != "decorated_definition":
                for ch in node.children:
                    if ch.type == "identifier" and ch.text.decode("utf-8") == symbol_name:
                        start_line = node.start_point[0] + 1
                        code = src_bytes[node.start_byte:node.end_byte].decode("utf-8")
                        hits.append({"line": start_line, "code": code})
                        break
        for ch in node.children:
            hits.extend(_collect(ch, src_bytes, parent_type=node.type))
        return hits

    def _search_file(abs_path):
        try:
            src_bytes = abs_path.read_bytes()
            tree = parser.parse(src_bytes)
            hits = _collect(tree.root_node, src_bytes)
            rel = str(abs_path.relative_to(_WORKSPACE_ROOT)).replace("\\", "/")
            return [{"file": rel, "line": h["line"], "code": h["code"]} for h in hits]
        except Exception:
            return []

    results = []
    if file_path:
        resolved, err = _validate_path(file_path)
        if err:
            return err
        results = _search_file(resolved)
    else:
        for py_file in _WORKSPACE_ROOT.rglob("*.py"):
            if ".git" in py_file.parts:
                continue
            results.extend(_search_file(py_file))

    if not results:
        return {"error": f"未找到符号 '{symbol_name}'"}
    return {"matches": results, "total": len(results)}

def apply_patch(patch_text, file_path=None):
    """应用 unified diff 格式的 patch 到文件"""
    import re

    lines = patch_text.splitlines(keepends=True)

    # 从 patch 推断目标文件
    if file_path is None:
        for line in lines:
            if line.startswith("+++ "):
                path = line[4:].strip()
                if path.startswith("b/"):
                    path = path[2:]
                file_path = path
                break
        if file_path is None:
            return {"error": "无法从 patch 推断目标文件路径，请指定 file_path"}

    resolved, err = _validate_path(file_path)
    if err:
        return err

    try:
        file_lines = resolved.read_text(encoding='utf-8').splitlines(keepends=True)
    except FileNotFoundError:
        return {"error": f"文件 {file_path} 不存在"}
    except Exception as e:
        return {"error": str(e)}

    hunk_re = re.compile(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@')
    hunks = []
    current = None

    for line in lines:
        m = hunk_re.match(line)
        if m:
            old_start = int(m.group(1))
            if old_start < 1:
                return {"error": f"补丁行号不合法: 行号 {old_start} < 1"}
            if old_start > len(file_lines):
                return {"error": f"补丁行号不合法: 起始行号 {old_start} > 文件总行数 {len(file_lines)}"}
            if m.group(2) and old_start > int(m.group(2)):
                return {"error": f"补丁行号不合法: start ({old_start}) > end ({int(m.group(2))})"}

            if current is not None:
                hunks.append(current)
            current = {
                'old_start': old_start - 1,  # 转为 0-based
                'lines': []
            }
        elif current is not None and not line.startswith(('--- ', '+++ ')):
            current['lines'].append(line)

    if current is not None:
        hunks.append(current)

    if not hunks:
        return {"error": "patch 中未找到有效的 hunk"}

    result = list(file_lines)
    offset = 0  # 已应用 hunk 导致的行号偏移

    for hunk in hunks:
        old_lines, new_lines = [], []
        for line in hunk['lines']:
            if line.startswith(' '):
                old_lines.append(line[1:])
                new_lines.append(line[1:])
            elif line.startswith('-'):
                old_lines.append(line[1:])
            elif line.startswith('+'):
                new_lines.append(line[1:])
        start = hunk['old_start'] + offset
        result[start:start + len(old_lines)] = new_lines
        offset += len(new_lines) - len(old_lines)

    try:
        resolved.write_text(''.join(result), encoding='utf-8')
        return {"success": f"patch 应用成功: {file_path}"}
    except Exception as e:
        return {"error": str(e)}


# ---------- #41 符号级编辑 ----------

def _load_ts_parser():
    try:
        import tree_sitter_python as tspython
        from tree_sitter import Language, Parser
        py_lang = Language(tspython.language())
        parser = Parser(py_lang)
        return parser, None
    except ImportError:
        return None, {"error": "tree-sitter 未安装，请运行: pip install tree-sitter tree-sitter-python"}


def list_symbols(file_path):
    """列出文件中所有函数和类，返回 name/type/line 列表"""
    parser, err = _load_ts_parser()
    if err:
        return err
    resolved, err = _validate_path(file_path)
    if err:
        return err
    try:
        src_bytes = resolved.read_bytes()
    except Exception as e:
        return {"error": str(e)}

    tree = parser.parse(src_bytes)
    symbols = []

    def _collect(node):
        if node.type in ("function_definition", "class_definition"):
            for ch in node.children:
                if ch.type == "identifier":
                    symbols.append({
                        "name": ch.text.decode("utf-8"),
                        "type": "function" if node.type == "function_definition" else "class",
                        "line": node.start_point[0] + 1,
                    })
                    break
        for ch in node.children:
            _collect(ch)

    _collect(tree.root_node)
    return {"symbols": symbols, "total": len(symbols)}


def replace_symbol(symbol_name, new_code, file_path):
    """用 tree-sitter 定位符号起止行，整体替换其实现"""
    import textwrap
    parser, err = _load_ts_parser()
    if err:
        return err
    resolved, err = _validate_path(file_path)
    if err:
        return err
    try:
        src_bytes = resolved.read_bytes()
        content = resolved.read_text(encoding="utf-8")
    except Exception as e:
        return {"error": str(e)}

    tree = parser.parse(src_bytes)

    def _find(node, parent_type=None):
        if node.type == "decorated_definition":
            for ch in node.children:
                if ch.type in ("function_definition", "class_definition"):
                    for grandch in ch.children:
                        if grandch.type == "identifier" and grandch.text.decode("utf-8") == symbol_name:
                            return node
                    break
        elif node.type in ("function_definition", "class_definition") and parent_type != "decorated_definition":
            for ch in node.children:
                if ch.type == "identifier" and ch.text.decode("utf-8") == symbol_name:
                    return node
        for ch in node.children:
            r = _find(ch, parent_type=node.type)
            if r:
                return r
        return None

    target = _find(tree.root_node)
    if target is None:
        return {"error": f"未找到符号 '{symbol_name}'"}

    start_line = target.start_point[0]   # 0-based
    end_line   = target.end_point[0]     # 0-based, inclusive

    lines = content.splitlines(keepends=True)

    # 缩进修复：取原符号首行的实际缩进字符串（保留 tab/space 原样，不 expand）
    first_line = lines[start_line]
    # 用 re 精确提取前导空白，兼容 tab/space 混用
    import re as _re
    _indent_match = _re.match(r'^(\s*)', first_line)
    target_indent = _indent_match.group(1) if _indent_match else ""

    # dedent 新代码后，逐行加上原缩进
    new_code = textwrap.dedent(new_code)
    new_code_lines = new_code.splitlines()
    indented_code = "".join(
        target_indent + line + "\n" if line.strip() else line + "\n"
        for line in new_code_lines
    )
    if not indented_code.endswith("\n"):
        indented_code += "\n"

    new_lines = lines[:start_line] + [indented_code] + lines[end_line + 1:]
    try:
        resolved.write_text("".join(new_lines), encoding="utf-8")
        return {
            "success": f"符号 '{symbol_name}' 替换成功",
            "file": file_path,
            "lines_replaced": end_line - start_line + 1,
        }
    except Exception as e:
        return {"error": str(e)}

def fetch_webpage(url):
    """读取网页内容，提取正文文本，截断到3000字符"""
    try:
        import requests
        from bs4 import BeautifulSoup
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        text = soup.get_text(separator='\n', strip=True)
        return {"content": text[:3000]}
    except Exception as e:
        return {"error": str(e)}

def search_docs(query):
    """搜索文档，优先使用 ddgs（duckduckgo_search 新包名），返回前3条结果的标题+摘要+URL"""
    # 优先用新包名 ddgs
    try:
        from ddgs import DDGS
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=3))
            if results:
                return {"results": results}
    except ImportError:
        pass
    except Exception:
        pass

    # 兼容旧包名 duckduckgo_search
    try:
        from duckduckgo_search import DDGS
        with DDGS() as ddgs:
            results = []
            for backend in ("api", "html", "lite"):
                try:
                    results = list(ddgs.text(query, max_results=3, backend=backend))
                except Exception:
                    pass
                if results:
                    break
            if results:
                return {"results": results}
    except ImportError:
        pass
    except Exception:
        pass

    # 备用：requests 直接抓 DuckDuckGo HTML
    try:
        import requests
        from bs4 import BeautifulSoup
        headers = {"User-Agent": "Mozilla/5.0 (compatible; yansh-code/1.0)"}
        resp = requests.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers=headers,
            timeout=10,
        )
        soup = BeautifulSoup(resp.text, "html.parser")
        results = []
        for r in soup.select(".result__body")[:3]:
            title_el = r.select_one(".result__title")
            snippet_el = r.select_one(".result__snippet")
            url_el = r.select_one(".result__url")
            results.append({
                "title": title_el.get_text(strip=True) if title_el else "",
                "body": snippet_el.get_text(strip=True) if snippet_el else "",
                "href": url_el.get_text(strip=True) if url_el else "",
            })
        if results:
            return {"results": results}
    except Exception:
        pass

    return {"results": "未找到相关结果"}


def append_to_file(filename, content):
    """向指定文件末尾追加内容
    - 路径校验（不能越出workspace）
    - 写入前自动补一个换行符，避免和原有内容粘连
    """
    resolved, err = _validate_path(filename)
    if err:
        return err
    try:
        prefix = ""
        if resolved.exists() and resolved.stat().st_size > 0:
            with open(resolved, "rb") as f:
                f.seek(-1, 2)
                if f.read(1) != b'\n':
                    prefix = "\n"

        with open(resolved, "a", encoding="utf-8") as f:
            f.write(prefix + content)

        return {"success": f"文件 {filename} 追加成功"}
    except Exception as e:
        return {"error": str(e)}


def find_references(symbol, path="."):
    """在指定目录下递归搜索所有 .py 文件中的符号引用
    排除定义行（即包含 def symbol 或 class symbol 的行）
    返回格式：文件路径:行号: 该行内容
    """
    from pathlib import Path
    import re

    resolved_root, err = _validate_path(path)
    if err:
        return err

    # 构建排除定义的正则
    # 匹配 def symbol, class symbol, async def symbol
    def_pattern = re.compile(rf"\b(def|class|async\s+def)\s+{re.escape(symbol)}\b")
    # 匹配符号引用（单词边界）
    ref_pattern = re.compile(rf"\b{re.escape(symbol)}\b")

    references = []

    # 遵循 .gitignore
    spec = _get_ignore_spec()
    abs_workspace = Path(WORKSPACE_DIR).resolve()

    for file_path in resolved_root.rglob("*.py"):
        if ".git" in file_path.parts:
            continue

        rel_path_ws = os.path.relpath(file_path, abs_workspace).replace("\\", "/")
        if spec and spec.match_file(rel_path_ws):
            continue

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                for line_num, line in enumerate(f, start=1):
                    # 检查是否包含符号
                    if ref_pattern.search(line):
                        # 排除定义行
                        if not def_pattern.search(line):
                            rel_path = os.path.relpath(file_path, WORKSPACE_DIR).replace("\\", "/")
                            references.append(f"{rel_path}:{line_num}: {line.strip()}")
        except Exception:
            continue

    return {"references": references, "total": len(references)}