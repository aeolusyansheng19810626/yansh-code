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


_STATE_CMD_RE = re.compile(r'^\s*(py\b|python[0-9.]*\b|pytest\b)', re.IGNORECASE)
_STATE_FILE_LOCK = threading.Lock()
_STATE_SECTION_LIMIT = 20  # 每 section 最多保留 20 条命令记录


def _trim_section(content: str, section_header: str, limit: int) -> str:
    """把 section_header 下的条目裁剪到最多 limit 条（保留最新的，即靠近 header 的）。"""
    if section_header not in content:
        return content
    idx = content.index(section_header)
    after = content[idx + len(section_header):]
    next_sec = re.search(r'\n## ', after)
    sec_body = after[:next_sec.start()] if next_sec else after
    rest = after[next_sec.start():] if next_sec else ""
    lines = [l for l in sec_body.splitlines(keepends=True) if l.strip()]
    if len(lines) > limit:
        lines = lines[:limit]  # 保留最新（靠近 header）的 limit 条
    new_body = "".join(lines)
    if new_body and not new_body.startswith("\n"):
        new_body = "\n" + new_body
    return content[:idx] + section_header + new_body + rest


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
            existing = _trim_section(existing, "## 已验证命令（exit=0）", _STATE_SECTION_LIMIT)
            existing = _trim_section(existing, "## 失败命令（exit≠0）", _STATE_SECTION_LIMIT)
            _tmp = state_path.with_suffix(".md.tmp")
            _tmp.write_text(existing, encoding="utf-8")
            os.replace(str(_tmp), str(state_path))
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
