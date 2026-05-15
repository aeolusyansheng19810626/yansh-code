"""HIL（Human-in-Loop）人工介入：diff 显示、编辑器调起、确认询问

模块级状态 _HIL_AUTO_ACCEPT 由 agent._run() 在每次任务开始时通过
reset_auto_accept() 重置；用户在某次确认中选 'a' 后置位，本轮后续 HIL
确认全部自动接受。
"""
import os
import sys
import difflib
from pathlib import Path

from rich.console import Console

console = Console()

_HIL_AUTO_ACCEPT = False


def reset_auto_accept():
    """每次新任务开始前调用，清掉上一轮的 'a 全部接受' 状态"""
    global _HIL_AUTO_ACCEPT
    _HIL_AUTO_ACCEPT = False


def show_diff(filename: str, old_str: str, new_str: str):
    """打印 unified diff（不截断），用于非 HIL 模式下的预览"""
    old_lines = old_str.splitlines(keepends=True)
    new_lines = new_str.splitlines(keepends=True)
    diff = list(difflib.unified_diff(
        old_lines, new_lines,
        fromfile=f"a/{filename}", tofile=f"b/{filename}", lineterm=""
    ))
    if not diff:
        return
    console.print(f"\n--- diff: {filename} ---", highlight=False)
    for line in diff:
        if line.startswith("---") or line.startswith("+++"):
            console.print(line, style="bold", highlight=False)
        elif line.startswith("-"):
            console.print(line, style="red", highlight=False)
        elif line.startswith("+"):
            console.print(line, style="green", highlight=False)
        elif line.startswith("@@"):
            console.print(line, style="cyan", highlight=False)
        else:
            console.print(line, highlight=False)


def _detect_editor():
    """返回可用编辑器命令列表。Windows 优先 VS Code，其次 notepad；Unix 读 $VISUAL/$EDITOR，否则 vi。"""
    import shutil
    if sys.platform == "win32":
        if shutil.which("code"):
            return ["code", "--wait"]
        return ["notepad"]
    for var in ("VISUAL", "EDITOR"):
        val = os.environ.get(var)
        if val:
            return val.split()
    return ["vi"]


def _build_diff_lines(filename, old_content, new_content, is_new_file=False):
    """生成 unified diff 行列表。超过 50 行时截断（头30 + 尾10）"""
    old_lines = [] if is_new_file else old_content.splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)
    from_file = "新建文件" if is_new_file else f"a/{filename}"
    diff = list(difflib.unified_diff(
        old_lines, new_lines,
        fromfile=from_file, tofile=f"b/{filename}", lineterm=""
    ))
    if len(diff) > 50:
        omitted = len(diff) - 40
        diff = diff[:30] + [f"...已截断，共 {len(diff)} 行变更，省略 {omitted} 行..."] + diff[-10:]
    return diff


def _print_diff_colored(diff_lines):
    for line in diff_lines:
        if line.startswith("---") or line.startswith("+++"):
            console.print(line, style="bold", highlight=False)
        elif line.startswith("-"):
            console.print(line, style="red", highlight=False)
        elif line.startswith("+"):
            console.print(line, style="green", highlight=False)
        elif line.startswith("@@"):
            console.print(line, style="cyan", highlight=False)
        elif line.startswith("..."):
            console.print(line, style="yellow", highlight=False)
        else:
            console.print(line, highlight=False)


def hil_confirm(filename, old_content, new_content, is_new_file=False):
    """展示 diff 并询问用户处理方式。
    返回 (accept: bool, final_content: str)
    选 'a' 时设置 _HIL_AUTO_ACCEPT = True，后续不再询问。"""
    global _HIL_AUTO_ACCEPT
    if _HIL_AUTO_ACCEPT:
        return True, new_content

    diff_lines = _build_diff_lines(filename, old_content, new_content, is_new_file)
    if not diff_lines:
        return True, new_content

    label = "新建文件" if is_new_file else "修改文件"
    console.print(f"\n[HIL] {label}: {filename}", highlight=False)
    _print_diff_colored(diff_lines)
    console.print("\n[y] 接受  [n] 拒绝  [e] 编辑后接受  [a] 全部接受（本轮）  ?", highlight=False)

    # 延迟 import 避免和 agent.py 循环依赖
    from agent import _prompt

    try:
        answer = _prompt("").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = "n"

    if answer == "a":
        _HIL_AUTO_ACCEPT = True
        return True, new_content
    if answer == "e":
        import tempfile
        suffix = Path(filename).suffix or ".txt"
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=suffix, delete=False, encoding="utf-8"
        ) as tf:
            tf.write(new_content)
            tmp_path = tf.name
        try:
            import subprocess as _sp
            _sp.call(_detect_editor() + [tmp_path])
            edited = Path(tmp_path).read_text(encoding="utf-8")
        except Exception as e:
            console.print(f"[HIL] 编辑器错误: {e}", style="red", highlight=False)
            edited = new_content
        finally:
            Path(tmp_path).unlink(missing_ok=True)
        return True, edited
    if answer == "n":
        console.print(f"[HIL] 已跳过: {filename}", highlight=False)
        return False, new_content
    return True, new_content
