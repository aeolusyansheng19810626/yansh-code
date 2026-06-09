import os
import re
import sys
import subprocess
import shutil
import threading
from pathlib import Path
from config import WORKSPACE_DIR

_WORKSPACE_ROOT = Path(WORKSPACE_DIR).resolve()


# #P0_3 错误恢复闭环：标准化 error_kind 分类
# execute_command 输出截断：保留头尾，省略中间噪音（如 pytest 逐条 PASSED 行）
_CMD_OUTPUT_HEAD = 3000  # chars
_CMD_OUTPUT_TAIL = 3000  # chars


def _truncate_cmd_output(text: str) -> str:
    if not text or len(text) <= _CMD_OUTPUT_HEAD + _CMD_OUTPUT_TAIL:
        return text
    omitted = len(text) - _CMD_OUTPUT_HEAD - _CMD_OUTPUT_TAIL
    return (text[:_CMD_OUTPUT_HEAD]
            + f"\n[... {omitted} chars truncated — middle output omitted, head/tail preserved ...]\n"
            + text[-_CMD_OUTPUT_TAIL:])


_STATE_CMD_RE = re.compile(r'^\s*(py\b|python[0-9.]*|pytest)', re.IGNORECASE)
_STATE_FILE_LOCK = threading.Lock()


def _update_agent_state(command: str, returncode: int) -> None:
    """框架自动维护 .yansh/agent_state.md：记录 python/pytest 命令的成功/失败。"""
    if not _STATE_CMD_RE.match(command):
        return
    # 跳过多行命令和超长命令（debug 脚本，对跨 run 无复用价值）
    cmd_stripped = command.strip()
    if "\n" in cmd_stripped or len(cmd_stripped) > 160:
        return
    try:
        state_dir = Path(_get_workspace()) / ".yansh"
        state_path = state_dir / "agent_state.md"
        state_dir.mkdir(parents=True, exist_ok=True)
        entry_line = f"- `{command.strip()}`\n"
        correct_section = "## 已验证命令（exit=0）" if returncode == 0 else "## 失败命令（exit≠0）"
        with _STATE_FILE_LOCK:
            existing = state_path.read_text(encoding="utf-8") if state_path.exists() else ""
            lines = existing.splitlines(keepends=True)
            # 精确行匹配，确定 entry_line 当前在哪个 section
            section_for_entry = None
            current_section = None
            for l in lines:
                if l.startswith("## "):
                    current_section = l.rstrip("\n")
                if l == entry_line:
                    section_for_entry = current_section
            if section_for_entry == correct_section:
                return  # 已在正确 section
            # 从错误 section 移除（先失败后成功 / 先成功后失败）
            if section_for_entry is not None:
                lines = [l for l in lines if l != entry_line]
                existing = "".join(lines)
            # 追加到正确 section
            if not existing:
                existing = "# 框架自动维护 — 环境知识（跨 run 复用）\n"
            if correct_section + "\n" in existing:
                existing = existing.replace(correct_section + "\n", correct_section + "\n" + entry_line, 1)
            else:
                existing = existing.rstrip("\n") + f"\n\n{correct_section}\n{entry_line}"
            state_path.write_text(existing, encoding="utf-8")
    except Exception:
        pass


ERROR_KINDS = frozenset({
    "invalid_args",  # 参数格式/取值错
    "not_found",     # 文件/符号不存在
    "permission",    # 路径越界 / 权限
    "security",      # 黑名单命令拦截
    "timeout",       # 执行/网络超时
    "transient",     # 网络/外部依赖一时挂
    "internal",      # 工具自身 bug / 解析失败 / 兜底 Exception
})


def _err(kind: str, msg: str, tool: str = None, **extra) -> dict:
    """统一错误构造：保留 'error' 键兼容老调用方，新增 'error_kind'。
    tool 用于标注异常源工具名（位置参数可传，便于 agent.py 异常分发处直接 _err(kind, msg, name)）。
    extra 用于附加 returncode/stdout/stderr 之类的辅助字段。"""
    assert kind in ERROR_KINDS, f"unknown error_kind: {kind}"
    out = {"error": msg, "error_kind": kind}
    if tool:
        out["tool"] = tool
    if extra:
        out.update(extra)
    return out


def task_complete(success: bool, summary: str) -> dict:
    """LLM 主动声明任务结束（fix/audit 循环识别 sentinel 后退出）。

    success=True  → 任务完成；success=False → 主动放弃（"我做不了"）。
    summary 用一句话说清楚做了什么/为什么放弃。
    沉默退出（这一轮不调任何工具）= 默认 success=True。
    """
    return {
        "_task_complete": True,
        "success": bool(success),
        "summary": str(summary or ""),
    }


def update_plan_draft(content: str) -> dict:
    """[P2 #7] Plan Mode 专用：写入/更新当前 plan 草稿。
    返回 sentinel `_plan_draft_update`，由 plan_chat() 循环捕获后写入 state。
    草稿是 markdown 文本，建议结构：## 目标 / ## 步骤 / ## 关键文件 / ## 风险。
    """
    return {
        "_plan_draft_update": True,
        "content": str(content or ""),
    }


def exit_plan_mode_signal(reason: str = "") -> dict:
    """[P2 #7] Plan Mode 专用：LLM 表示当前轮次的探索/写草稿已完成，等待用户审批。
    不强制退出 Plan Mode——用户可继续追问 / 调整。仅作为"请审阅"的礼貌信号。
    """
    return {
        "_exit_plan_mode_signal": True,
        "reason": str(reason or ""),
    }


def save_memory(name: str, type: str, description: str, body: str,
                scope: str = "project") -> dict:
    """[P2 #12] 写一条跨 session memory。LLM 主动调——遇到值得长期记住的事实就写。
    透传到 memory.save_memory（避免 tools 模块依赖 LLM 模块；workspace_dir 这里取）。
    """
    import memory as _mem
    return _mem.save_memory(
        name=name, type=type, description=description, body=body,
        scope=scope, workspace_dir=_get_workspace(),
    )


def recall_memory(name: str) -> dict:
    """[P2 #12] 按 name 读一条 memory 的完整内容。索引在 system prompt 里给了，
    LLM 看到相关索引调这个工具拉详情——不要凭印象猜。
    """
    import memory as _mem
    mem = _mem.find_memory(name, workspace_dir=_get_workspace())
    if mem is None:
        return {"error": f"memory 不存在: {name}"}
    return {
        "name": mem.name,
        "type": mem.type,
        "description": mem.description,
        "scope": mem.scope,
        "body": mem.body,
    }


def dispatch_subagent(task: str, role: str = "explorer", max_steps: int = 8) -> dict:
    """[P2 #9] Sentinel：LLM 调时返回标记，由 agent._subagent_handler 拦截并实际执行。

    这里只做参数透传——真正的子 agent loop 在 agent._run_subagent。这样保持 tools 模块
    无 LLM 依赖（agent 模块才依赖 llm_client），避免 import 循环。
    """
    return {
        "_subagent_dispatch": True,
        "task": str(task or ""),
        "role": str(role or "explorer"),
        "max_steps": int(max_steps or 8),
    }


def _reinit_paths():
    """--cwd 变更后重新初始化模块级路径变量（由 main.py 调用）"""
    global _WORKSPACE_ROOT
    from config import WORKSPACE_DIR as _WD
    _WORKSPACE_ROOT = Path(_WD).resolve()


def _get_workspace() -> str:
    """每次调用都从 config 读取最新 WORKSPACE_DIR，确保 --cwd 生效后不使用旧值。"""
    import config as _cfg_mod
    return _cfg_mod.WORKSPACE_DIR

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
        return None, _err("permission", "路径越界：不允许访问workspace外的文件")
    if ".." in p.parts:
        return None, _err("permission", "路径越界：不允许访问workspace外的文件")
    candidate = (_WORKSPACE_ROOT / p).resolve()
    if not candidate.is_relative_to(_WORKSPACE_ROOT):
        return None, _err("permission", "路径越界：不允许访问workspace外的文件")
    return candidate, None

def _check_dangerous(command):
    """检查命令是否包含危险模式。返回 None 表示安全，否则返回 error_dict。"""
    for pattern, label in _DANGEROUS_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE | re.DOTALL):
            _con().print(f"[安全拦截] 检测到危险命令：{label}", highlight=False)
            return _err("security", f"安全拦截：检测到危险命令（{label}），已阻止执行",
                        returncode=-2, stdout="", stderr="")
    return None

def write_file(filename, content):
    """在workspace目录下写入文件"""
    resolved, err = _validate_path(filename)
    if err:
        return err
    os.makedirs(_get_workspace(), exist_ok=True)
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding='utf-8')
        _invalidate_ast_cache(resolved)
        return {"success": f"文件 {filename} 写入成功"}
    except Exception as e:
        return _err("internal", str(e))

# P2 #4-A2: read_file 默认值——防大文件 input token 爆炸（参考 yscode 200KB 硬限 / cc 2000 行默认）
READ_FILE_DEFAULT_LIMIT = 2000      # 行数上限：> 2000 行需显式传 limit 或用 offset 分页
READ_FILE_DEFAULT_MAX_BYTES = 200_000  # 字节上限：> 200KB 截断并附 truncated 提示


def read_file(filename, offset=None, limit=None, max_bytes=None):
    """读取workspace目录下的文件。
    可选 offset/limit 按行截取（offset 1-based 起始行；limit 行数上限，**默认 2000**）。
    可选 max_bytes 限制读取的字节数（**默认 200_000**）；超过时截断内容并在返回 dict 中加入 truncated=True。
    LLM 显式传 limit=巨大值 / max_bytes=巨大值 可绕过默认限制，但应优先用 offset 分页读。"""
    resolved, err = _validate_path(filename)
    if err:
        return err
    try:
        text = resolved.read_text(encoding='utf-8')
    except FileNotFoundError:
        return _err("not_found", f"文件 {filename} 不存在")
    except Exception as e:
        return _err("internal", str(e))

    # P2 #4-A2: 应用默认值（None → 默认）
    effective_limit = READ_FILE_DEFAULT_LIMIT if limit is None else int(limit)
    effective_max_bytes = READ_FILE_DEFAULT_MAX_BYTES if max_bytes is None else int(max_bytes)

    # P2 #4-A2 review M1: 先按行切（total 反映真实文件行数），再对切片做 byte 截断。
    # 颠倒过来会让 max_bytes 优先截断 → total_lines 报错且 offset 续读永远读不到后半。
    lines = text.splitlines(keepends=True)
    total = len(lines)
    start = max(0, (offset or 1) - 1)
    end = min(total, start + max(0, effective_limit))
    sliced = "".join(lines[start:end])

    truncated = False
    encoded = sliced.encode('utf-8')
    if len(encoded) > effective_max_bytes:
        sliced = encoded[:effective_max_bytes].decode('utf-8', errors='replace')
        truncated = True

    result = {
        "content": sliced,
        "total_lines": total,
        "offset": start + 1,
        "lines_returned": end - start,
    }
    if truncated:
        result["truncated"] = True
        result["hint"] = (f"slice truncated at {effective_max_bytes} bytes; "
                          f"narrow limit or pass max_bytes explicitly to override")
    if end < total:
        result["hint_more_lines"] = (f"only lines {start + 1}-{end} returned (total {total}); "
                                     f"use offset={end + 1} to continue")
    return result

def execute_command(command, _timeout_sec=30):
    """在workspace目录下执行命令，30秒超时，三级命令策略（deny/safe/confirm）"""
    # Level 1: deny
    danger = _check_dangerous(command)
    if danger:
        return danger

    cmd_stripped = command.strip()

    # Level 2: safe — 直接执行
    is_safe = any(re.match(p, cmd_stripped, re.IGNORECASE) for p in _SAFE_PATTERNS)

    # Level 3: confirm — 默认所有非 _SAFE_PATTERNS 命令都需要用户确认。
    # 命中 _CONFIRM_PATTERNS 的额外提供识别标签（如 "pip install"）；未命中则归为 "未识别命令"。
    # batch + strict：未识别命令直接拒；batch 非 strict：自动确认（保留向后兼容）。
    if not is_safe:
        label = next(
            (lbl for pat, lbl in _CONFIRM_PATTERNS
             if re.search(pat, cmd_stripped, re.IGNORECASE)),
            "未识别命令",
        )
        if _BATCH_MODE and _BATCH_STRICT:
            _con().print(f"[batch-strict] 拒绝执行: {label} -> {command}", highlight=False)
            return _err("security", f"批处理严格模式拒绝执行: {label}", returncode=-1, stdout="", stderr="")
        if _BATCH_MODE:
            _con().print(f"[batch] 自动确认执行 ({label}): {command}", highlight=False)
        else:
            _c = _con()
            _c.print(f"[确认] 即将执行 ({label}): {command}", highlight=False)
            try:
                answer = _c.input("继续？(y/n) ").strip().lower()
            except EOFError:
                answer = "n"
            if answer != "y":
                return _err("security", "用户取消执行", returncode=-1, stdout="", stderr="")

    import threading
    # P1 #6：opt-in 沙箱——若启用，把命令包成 docker run 形态；默认禁用，行为不变
    try:
        import sandbox as _sandbox
        run_command = _sandbox.wrap_command(command, _get_workspace())
    except Exception:
        run_command = command

    try:
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        process = subprocess.Popen(
            run_command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=_get_workspace(),
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
                    if time.time() - start_time > _timeout_sec:
                        process.kill()
                        t_out.join(timeout=2)
                        t_err.join(timeout=2)
                        return _err("timeout", f"命令执行超时（{_timeout_sec}秒）",
                                    "execute_command",
                                    stdout=''.join(stdout_lines),
                                    stderr=''.join(stderr_lines),
                                    returncode=-1)
        except interrupt.Interrupted:
            raise
        except Exception as e:
            return _err("internal", str(e))

        t_out.join()
        t_err.join()

        _update_agent_state(command, process.returncode)
        return {
            "stdout": _truncate_cmd_output(''.join(stdout_lines)),
            "stderr": _truncate_cmd_output(''.join(stderr_lines)),
            "returncode": process.returncode
        }
    except Exception as e:
        return _err("internal", str(e))

def delete_file(filename):
    """删除workspace目录下的文件"""
    resolved, err = _validate_path(filename)
    if err:
        return err
    try:
        resolved.unlink()
        return {"success": f"文件 {filename} 删除成功"}
    except FileNotFoundError:
        return _err("not_found", f"文件 {filename} 不存在")
    except Exception as e:
        return _err("internal", str(e))

def replace_in_file(filename, old_str, new_str):
    """在workspace文件中精确替换字符串。old_str必须唯一匹配，否则返回错误"""
    resolved, err = _validate_path(filename)
    if err:
        return err

    try:
        content = resolved.read_text(encoding='utf-8')
    except FileNotFoundError:
        return _err("not_found", f"文件 {filename} 不存在")
    except Exception as e:
        return _err("internal", str(e))

    count = content.count(old_str)
    if count == 0:
        return _err("not_found", f"在 {filename} 中未找到要替换的字符串")
    if count > 1:
        return _err("invalid_args", (
            f"在 {filename} 中找到 {count} 处匹配，需唯一匹配。"
            "请在 old_str 中增加上下文行（前后多带几行代码）以确保唯一；"
            "或改用 replace_symbol 按函数/类名整体替换。"
        ))

    content = content.replace(old_str, new_str, 1)
    try:
        resolved.write_text(content, encoding='utf-8')
        _invalidate_ast_cache(resolved)
        return {
            "success": f"文件 {filename} 替换成功",
            "filename": filename,
            "old_str": old_str,
            "new_str": new_str
        }
    except Exception as e:
        return _err("internal", str(e))

def _get_ignore_spec():
    import pathspec
    gitignore_path = Path(_get_workspace()) / ".gitignore"
    if gitignore_path.exists():
        try:
            with open(gitignore_path, "r", encoding="utf-8") as f:
                return pathspec.PathSpec.from_lines("gitwildmatch", f)
        except Exception:
            return None
    return None

def list_files():
    """列出workspace目录下的所有文件（遵循.gitignore）"""
    ws = _get_workspace()
    os.makedirs(ws, exist_ok=True)
    files = []
    spec = _get_ignore_spec()
    for root, dirs, filenames in os.walk(ws):
        # 跳过 .git 目录
        if ".git" in root:
            continue
        for filename in filenames:
            rel_path = os.path.relpath(os.path.join(root, filename), ws)
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
        return _err("not_found", f"Source file {src} does not exist")

    # Create dst parent directory
    dst_path.parent.mkdir(parents=True, exist_ok=True)

    # Move file
    try:
        shutil.move(str(src_path), str(dst_path))
        return {"success": f"File moved from {src} to {dst} successfully"}
    except Exception as e:
        return _err("internal", str(e))

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
        workspace = Path(_get_workspace())
    else:
        workspace = Path(workspace)
    
    # 路径安全检查
    abs_workspace = Path(_get_workspace()).resolve()
    abs_search_path = workspace.resolve()
    
    try:
        if not abs_search_path.is_relative_to(abs_workspace):
            return _err("permission", "Search path exceeds workspace directory")
    except ValueError:
        return _err("permission", "Search path exceeds workspace directory")
    
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
    parser, err = _load_ts_parser()
    if err:
        return err

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
            tree = _ts_parse_locked(parser, src_bytes)
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
        return _err("not_found", f"未找到符号 '{symbol_name}'")
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
            return _err("invalid_args", "无法从 patch 推断目标文件路径，请指定 file_path")

    resolved, err = _validate_path(file_path)
    if err:
        return err

    try:
        file_lines = resolved.read_text(encoding='utf-8').splitlines(keepends=True)
    except FileNotFoundError:
        return _err("not_found", f"文件 {file_path} 不存在")
    except Exception as e:
        return _err("internal", str(e))

    hunk_re = re.compile(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@')
    hunks = []
    current = None

    for line in lines:
        m = hunk_re.match(line)
        if m:
            old_start = int(m.group(1))
            if old_start < 1:
                return _err("invalid_args", f"补丁行号不合法: 行号 {old_start} < 1")
            if old_start > len(file_lines):
                return _err("invalid_args", f"补丁行号不合法: 起始行号 {old_start} > 文件总行数 {len(file_lines)}")
            if m.group(2) and old_start > int(m.group(2)):
                return _err("invalid_args", f"补丁行号不合法: start ({old_start}) > end ({int(m.group(2))})")

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
        return _err("invalid_args", "patch 中未找到有效的 hunk")

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
        _invalidate_ast_cache(resolved)
        return {"success": f"patch 应用成功: {file_path}"}
    except Exception as e:
        return _err("internal", str(e))


# ---------- #41 符号级编辑 ----------

# 进程级 parser 单例，懒加载；避免每次 list_symbols / get_symbol_definition 都重建
_TS_PARSER = None
# 文件级 AST 符号缓存：abs_path_str -> (mtime, [{name, type, line}])
# 仅 list_symbols 路径使用；replace_symbol 写入后调 _invalidate_ast_cache
_AST_CACHE: dict = {}
# P1 安全：tree-sitter Parser 不是 thread-safe；并发 subagent 启动时
# workspace_symbols 会撞 parser.parse。用一把锁串行 parse 调用。
# 单次 parse < 10ms，对 yansh 并发场景可接受。
_TS_PARSER_LOCK = threading.Lock()


def _load_ts_parser():
    """返回 (parser, error_dict)。parser 跨调用复用，init 加锁防多线程同时 init。"""
    global _TS_PARSER
    if _TS_PARSER is not None:
        return _TS_PARSER, None
    with _TS_PARSER_LOCK:
        # double-check：拿到锁后再确认
        if _TS_PARSER is not None:
            return _TS_PARSER, None
        try:
            import tree_sitter_python as tspython
            from tree_sitter import Language, Parser
            py_lang = Language(tspython.language())
            _TS_PARSER = Parser(py_lang)
            return _TS_PARSER, None
        except ImportError:
            return None, _err("internal", "tree-sitter 未安装，请运行: pip install tree-sitter tree-sitter-python")


def _ts_parse_locked(parser, src_bytes):
    """parser.parse 必须串行——tree-sitter Parser 单实例并发不安全。"""
    with _TS_PARSER_LOCK:
        return parser.parse(src_bytes)


def _invalidate_ast_cache(abs_path):
    """文件被写入后调用，清掉对应的缓存项（dict.pop CPython 原子）"""
    _AST_CACHE.pop(str(abs_path), None)


def _parse_symbols_cached(abs_path):
    """按 mtime 命中 _AST_CACHE；未命中则 parse 并写缓存。
    返回 (symbols_list, error_dict)；error_dict 为 None 表示成功。
    symbols_list 元素：{name, type, line}，仅函数和类。"""
    parser, err = _load_ts_parser()
    if err:
        return None, err
    try:
        mtime = abs_path.stat().st_mtime
    except FileNotFoundError as e:
        return None, _err("not_found", str(e))
    except Exception as e:
        return None, _err("internal", str(e))

    key = str(abs_path)
    cached = _AST_CACHE.get(key)
    if cached and cached[0] == mtime:
        return cached[1], None

    try:
        src_bytes = abs_path.read_bytes()
    except FileNotFoundError as e:
        return None, _err("not_found", str(e))
    except Exception as e:
        return None, _err("internal", str(e))

    tree = _ts_parse_locked(parser, src_bytes)
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
    _AST_CACHE[key] = (mtime, symbols)
    return symbols, None


def list_symbols(file_path):
    """列出文件中所有函数和类，返回 name/type/line 列表"""
    resolved, err = _validate_path(file_path)
    if err:
        return err
    symbols, err = _parse_symbols_cached(resolved)
    if err:
        return err
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
    except FileNotFoundError as e:
        return _err("not_found", str(e))
    except Exception as e:
        return _err("internal", str(e))

    tree = _ts_parse_locked(parser, src_bytes)

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
        return _err("not_found", f"未找到符号 '{symbol_name}'")

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
        _invalidate_ast_cache(resolved)
        return {
            "success": f"符号 '{symbol_name}' 替换成功",
            "file": file_path,
            "lines_replaced": end_line - start_line + 1,
        }
    except Exception as e:
        return _err("internal", str(e))

_WORKSPACE_SYMBOLS_IGNORE = {".git", ".yansh", "__pycache__", "node_modules", "venv", ".venv", ".pytest_cache"}


def _dir_symbol_count(dirpath: Path, exts: tuple) -> tuple:
    """递归统计某目录下匹配 exts 的文件数 + 符号总数。
    用于 workspace_symbols top 模式给子目录做摘要——只算计数不存符号清单。
    命中 _AST_CACHE 几乎零成本。"""
    py_files = 0
    total_symbols = 0
    for f in dirpath.rglob("*"):
        if not f.is_file():
            continue
        if any(part in _WORKSPACE_SYMBOLS_IGNORE for part in f.parts):
            continue
        if not f.name.endswith(exts):
            continue
        py_files += 1
        symbols, err = _parse_symbols_cached(f)
        if err:
            continue
        total_symbols += len(symbols)
    return py_files, total_symbols


def workspace_symbols(extensions=None, path=None, recursive=False):
    """扫描 workspace 内符号清单。
    - 默认（path=None, recursive=False）：返回顶层文件符号 + 子目录摘要（py_files / total_symbols）
    - path="sub/dir", recursive=False：返回该目录顶层文件符号 + 子目录摘要
    - recursive=True：递归扫整个子树（旧全量行为；大项目慎用）

    默认只扫 .py。命中 _AST_CACHE 几乎零成本。"""
    exts = tuple(extensions) if extensions else (".py",)
    ws_root = Path(_get_workspace())
    if not ws_root.exists():
        return {"mode": "top", "path": ".", "files": {}, "subdirs": {},
                "total_files": 0, "total_symbols": 0}

    # 解析 path 参数：相对 workspace；越界拦截
    if path:
        resolved, err = _validate_path(path)
        if err:
            return err
        if not resolved.exists():
            return _err("not_found", f"目录不存在：{path}")
        if not resolved.is_dir():
            return _err("invalid_args", f"path 不是目录：{path}")
        scan_root = resolved
        rel_base = path.rstrip("/").rstrip("\\")
    else:
        scan_root = ws_root
        rel_base = "."

    # recursive=True：旧 deep 行为
    if recursive:
        files_out: dict = {}
        total_symbols = 0
        for f in scan_root.rglob("*"):
            if not f.is_file():
                continue
            if any(part in _WORKSPACE_SYMBOLS_IGNORE for part in f.parts):
                continue
            if not f.name.endswith(exts):
                continue
            symbols, err = _parse_symbols_cached(f)
            if err:
                if "tree-sitter 未安装" in err.get("error", ""):
                    return err
                continue
            rel = str(f.relative_to(ws_root)).replace("\\", "/")
            files_out[rel] = symbols
            total_symbols += len(symbols)
        return {
            "mode": "deep",
            "path": rel_base,
            "files": files_out,
            "total_files": len(files_out),
            "total_symbols": total_symbols,
        }

    # 默认 top 模式：只列直接子项
    files_out = {}
    subdirs: dict = {}
    total_symbols = 0
    try:
        entries = list(scan_root.iterdir())
    except OSError as e:
        return _err("internal", f"读取目录失败：{e}")

    for entry in entries:
        if entry.name in _WORKSPACE_SYMBOLS_IGNORE:
            continue
        if entry.is_file():
            if not entry.name.endswith(exts):
                continue
            symbols, err = _parse_symbols_cached(entry)
            if err:
                if "tree-sitter 未安装" in err.get("error", ""):
                    return err
                continue
            rel = str(entry.relative_to(ws_root)).replace("\\", "/")
            files_out[rel] = symbols
            total_symbols += len(symbols)
        elif entry.is_dir():
            py_files, sub_total = _dir_symbol_count(entry, exts)
            # 跳过空子目录（无任何匹配文件）以减小 prompt 噪音
            if py_files == 0:
                continue
            subdirs[entry.name] = {"py_files": py_files, "total_symbols": sub_total}

    return {
        "mode": "top",
        "path": rel_base,
        "files": files_out,
        "subdirs": subdirs,
        "total_files": len(files_out),
        "total_symbols": total_symbols,
    }


_DIR_SUMMARY_KEY_FILES = (
    "README.md", "README.rst", "README", "README.txt",
    "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt",
    "Makefile", "Dockerfile", "docker-compose.yml",
    "Cargo.toml", "go.mod", "package.json", "tsconfig.json",
    "CLAUDE.md", "ROADMAP.md", ".agent_rules",
)


def directory_summary(path="."):
    """返回某目录的整体感知摘要：文件数、扩展名分布、关键文件、直接子目录、文件名采样。
    不递归——只看直接子项。用于 LLM 在大项目里快速了解某目录是干啥的。"""
    ws_root = Path(_get_workspace())
    if not ws_root.exists():
        return _err("not_found", f"workspace 不存在：{ws_root}")

    if path in (".", ""):
        target = ws_root
        rel_base = "."
    else:
        resolved, err = _validate_path(path)
        if err:
            return err
        if not resolved.exists():
            return _err("not_found", f"目录不存在：{path}")
        if not resolved.is_dir():
            return _err("invalid_args", f"path 不是目录：{path}")
        target = resolved
        rel_base = path.rstrip("/").rstrip("\\").replace("\\", "/")

    by_ext: dict = {}
    key_files = []
    subdirs = []
    files_sample = []
    file_count = 0
    subdir_count = 0

    try:
        entries = sorted(target.iterdir(), key=lambda e: e.name)
    except OSError as e:
        return _err("internal", f"读取目录失败：{e}")

    for entry in entries:
        if entry.name in _WORKSPACE_SYMBOLS_IGNORE:
            continue
        if entry.is_file():
            file_count += 1
            ext = entry.suffix.lower() or "<noext>"
            by_ext[ext] = by_ext.get(ext, 0) + 1
            if entry.name in _DIR_SUMMARY_KEY_FILES:
                key_files.append(entry.name)
            if len(files_sample) < 12:
                files_sample.append(entry.name)
        elif entry.is_dir():
            subdir_count += 1
            subdirs.append(entry.name + "/")

    if file_count > len(files_sample):
        files_sample.append(f"... 还有 {file_count - len(files_sample)} 个文件")

    return {
        "path": rel_base,
        "file_count": file_count,
        "subdir_count": subdir_count,
        "by_extension": dict(sorted(by_ext.items(), key=lambda kv: -kv[1])),
        "key_files": key_files,
        "subdirs": subdirs,
        "files_sample": files_sample,
    }


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
        # 网络/HTTP 类错误一律视为 transient，让 LLM 自行决定 retry
        msg = str(e).lower()
        if "timeout" in msg or "timed out" in msg:
            return _err("timeout", str(e))
        if "invalid url" in msg or "missing schema" in msg or "no host" in msg:
            return _err("invalid_args", str(e))
        return _err("transient", str(e))

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
        _invalidate_ast_cache(resolved)
        return {"success": f"文件 {filename} 追加成功"}
    except Exception as e:
        return _err("internal", str(e))


def glob_files(pattern, path="."):
    """在 workspace 内按 glob 模式匹配文件路径，遵循 .gitignore。
    pattern 同时匹配相对路径（如 'src/**/*.py'）和裸文件名（如 '*.py'）。"""
    import fnmatch
    resolved_root, err = _validate_path(path)
    if err:
        return err
    spec = _get_ignore_spec()
    abs_workspace = Path(_get_workspace()).resolve()
    matches = []
    for f in resolved_root.rglob("*"):
        if f.is_dir():
            continue
        if ".git" in f.parts:
            continue
        try:
            rel = os.path.relpath(f, abs_workspace).replace("\\", "/")
        except ValueError:
            continue
        if spec and spec.match_file(rel):
            continue
        if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(f.name, pattern):
            matches.append(rel)
    return {"matches": matches, "total": len(matches)}


def _git_run_ws(args: list, timeout: int = 10) -> tuple:
    """在 workspace 目录跑 git 命令，返回 (returncode, stdout, stderr)"""
    import subprocess
    try:
        r = subprocess.run(
            ["git"] + args,
            cwd=_get_workspace(),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout,
        )
        return r.returncode, r.stdout, r.stderr
    except FileNotFoundError:
        return -1, "", "git 未安装"
    except subprocess.TimeoutExpired:
        return -1, "", "git 命令超时"


def git_diff(path=None, staged=False):
    """在 workspace 跑 git diff，输出超过 20000 字符截断"""
    args = ["diff"]
    if staged:
        args.append("--cached")
    if path:
        resolved, err = _validate_path(path)
        if err:
            return err
        try:
            rel = str(resolved.relative_to(_WORKSPACE_ROOT)).replace("\\", "/")
        except ValueError:
            return _err("permission", "path 越界")
        args.append(rel)
    rc, stdout, stderr = _git_run_ws(args, timeout=15)
    if rc != 0 and not stdout:
        return _err("invalid_args", stderr.strip() or "git diff 失败（非 git 仓库？）")
    truncated = len(stdout) > 20000
    return {"diff": stdout[:20000], "truncated": truncated}


def git_log(limit=10):
    """git log --oneline -n <limit>"""
    rc, stdout, stderr = _git_run_ws(["log", "--oneline", f"-{int(limit)}"], timeout=10)
    if rc != 0:
        return _err("invalid_args", stderr.strip() or "git log 失败")
    return {"log": stdout}


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
    abs_workspace = Path(_get_workspace()).resolve()

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
                            rel_path = os.path.relpath(file_path, _get_workspace()).replace("\\", "/")
                            references.append(f"{rel_path}:{line_num}: {line.strip()}")
        except Exception:
            continue

    return {"references": references, "total": len(references)}