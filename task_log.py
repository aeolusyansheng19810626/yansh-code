"""任务日志：每次 _run() 期间记录 plan/工具调用/修改文件，结束时写盘到 .yansh/logs/

模块级状态由 record_* 函数维护；agent._dispatch_tool_call 通过 record_file_modified
和 record_tool_call 写入。get_last_task_log 供批处理 --json 输出。
"""
import json
import time as _time
from datetime import datetime
from pathlib import Path

from rich.console import Console
import config as _cfg_mod

console = Console()

_LOG_DIR = Path(_cfg_mod.WORKSPACE_DIR) / ".yansh" / "logs"

_current_task_log: dict = {}
_task_tool_calls: list = []
_task_files_modified: list = []
_last_task_log: dict = {}


def _reinit_paths():
    """--cwd 变更后由 agent._reinit_paths() 调用"""
    global _LOG_DIR
    _LOG_DIR = Path(_cfg_mod.WORKSPACE_DIR) / ".yansh" / "logs"


def init_task_log(requirement, mode):
    """重置当前任务日志（in-place 清空+更新，外部持有的 dict 引用仍有效）"""
    from config import get_config
    _task_tool_calls.clear()
    _task_files_modified.clear()
    _current_task_log.clear()
    _current_task_log.update({
        "timestamp": datetime.now().isoformat(),
        "requirement": requirement,
        "mode": mode,
        "model": get_config().get("model") or "unknown",
        "plan": [],
        "files_modified": [],
        "tool_calls": [],
        "test_command": "",
        "test_result": "unknown",
        "attempts": 0,
        "error": None,
        "duration_seconds": 0.0,
        "_start": _time.time(),
    })


def finish_task_log(success, attempts, test_result=None):
    if not _current_task_log:
        return
    _current_task_log["test_result"] = "pass" if success else "fail"
    _current_task_log["attempts"] = attempts
    _current_task_log["tool_calls"] = _task_tool_calls[:]
    _current_task_log["files_modified"] = list(dict.fromkeys(_task_files_modified))
    _current_task_log["duration_seconds"] = round(_time.time() - _current_task_log.pop("_start"), 2)
    if test_result and not success:
        err = test_result.get("stderr", "") or test_result.get("stdout", "")
        _current_task_log["error"] = err[:300]
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    (_LOG_DIR / f"{ts}.jsonl").write_text(
        json.dumps(_current_task_log, ensure_ascii=False), encoding="utf-8"
    )
    _last_task_log.clear()
    _last_task_log.update(_current_task_log)
    _current_task_log.clear()


def show_recent_logs():
    """打印最近 5 条日志摘要"""
    if not _LOG_DIR.exists():
        console.print("暂无日志", highlight=False)
        return
    logs = sorted(_LOG_DIR.glob("*.jsonl"), reverse=True)[:5]
    if not logs:
        console.print("暂无日志", highlight=False)
        return
    for f in logs:
        try:
            e = json.loads(f.read_text(encoding="utf-8"))
            ts  = e.get("timestamp", "")[:19]
            req = e.get("requirement", "")[:60]
            res = "✓" if e.get("test_result") == "pass" else "✗"
            dur = e.get("duration_seconds", 0)
            att = e.get("attempts", 0)
            console.print(f"{ts} | {res} | {dur}s | {att}次 | {req}", highlight=False)
        except Exception:
            continue


def get_last_task_log() -> dict:
    return dict(_last_task_log)


def get_current_log() -> dict:
    """供 _run() / _auto_generate_tests 等填充 plan / test_command 字段"""
    return _current_task_log


def record_file_modified(filename: str):
    if filename:
        _task_files_modified.append(filename)


def record_tool_call(name: str, safe_args: dict):
    _task_tool_calls.append({"name": name, "args": safe_args})
