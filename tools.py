import os
import re
import subprocess
import shutil
from pathlib import Path
from config import WORKSPACE_DIR

_WORKSPACE_ROOT = Path(WORKSPACE_DIR).resolve()

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
            from rich.console import Console
            Console().print(f"[安全拦截] 检测到危险命令：{label}", highlight=False)
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
    """在workspace目录下执行命令，30秒超时，两线程并发读stdout/stderr防死锁"""
    danger = _check_dangerous(command)
    if danger:
        return danger

    import threading

    try:
        process = subprocess.Popen(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=WORKSPACE_DIR,
        )

        stdout_lines = []
        stderr_lines = []

        def _read_stdout():
            for line in process.stdout:
                print(line, end='', flush=True)
                stdout_lines.append(line)

        def _read_stderr():
            for line in process.stderr:
                stderr_lines.append(line)

        t_out = threading.Thread(target=_read_stdout, daemon=True)
        t_err = threading.Thread(target=_read_stderr, daemon=True)
        t_out.start()
        t_err.start()

        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            return {"error": "命令执行超时（30秒）"}

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
        return {"error": f"在 {filename} 中找到 {count} 处匹配，需唯一匹配"}

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

def list_files():
    """列出workspace目录下的所有文件"""
    os.makedirs(WORKSPACE_DIR, exist_ok=True)
    files = []
    for root, dirs, filenames in os.walk(WORKSPACE_DIR):
        for filename in filenames:
            rel_path = os.path.relpath(os.path.join(root, filename), WORKSPACE_DIR)
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
    
    # 递归搜索所有文件
    for file_path in workspace.rglob("*"):
        # 跳过目录
        if file_path.is_dir():
            continue
        
        # 跳过 .git 目录
        if ".git" in file_path.parts:
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
            if current is not None:
                hunks.append(current)
            current = {
                'old_start': int(m.group(1)) - 1,  # 转为 0-based
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