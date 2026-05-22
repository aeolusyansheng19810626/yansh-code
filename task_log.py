"""任务日志：每次 _run() 期间记录 plan/工具调用/修改文件，结束时写盘到 .yansh/logs/

模块级状态由 record_* 函数维护；agent._dispatch_tool_call 通过 record_file_modified
和 record_tool_call 写入。get_last_task_log 供批处理 --json 输出。

P2 并发：所有读写都走 _log_lock。CPython GIL 下 list.append/clear 单条原子，
但 finish_task_log 的 dict.fromkeys(_task_files_modified) 迭代期间如果有
并发 append（multi-subagent），结果未定。加锁让多 subagent 写日志互不交错。
"""
import json
import threading
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
_log_lock = threading.Lock()
# AB 测试 / 成本统计：init 时记 baseline，finish 时算 delta 即本任务的 token 用量
_token_baseline: dict = {"input": 0, "output": 0, "by_model": {}}


def _reinit_paths():
    """--cwd 变更后由 agent._reinit_paths() 调用"""
    global _LOG_DIR
    _LOG_DIR = Path(_cfg_mod.WORKSPACE_DIR) / ".yansh" / "logs"


def init_task_log(requirement, mode):
    """重置当前任务日志（in-place 清空+更新，外部持有的 dict 引用仍有效）"""
    from config import get_config
    # 记 token baseline——finish 时算 delta 得到本任务的 token 实耗
    try:
        import llm_client as _lc
        global _token_baseline
        _token_baseline = _lc.get_session_token_breakdown()
    except Exception:
        _token_baseline = {"input": 0, "output": 0, "by_model": {}}
    with _log_lock:
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


def finish_task_log(success, attempts, test_result=None, task_complete_signal=None):
    """落盘任务日志。
    task_complete_signal: LLM 主动声明的 {early_exit, success, summary}。
      传 None 表示这次没收到主动信号（沉默退出 / 老路径），日志里不写该字段。
    """
    with _log_lock:
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
        if task_complete_signal:
            _current_task_log["task_complete_signal"] = {
                "early_exit": bool(task_complete_signal.get("early_exit", False)),
                "success": bool(task_complete_signal.get("success", False)),
                "summary": str(task_complete_signal.get("summary", ""))[:500],
            }
        # token 实耗 = 当前累计 - baseline（多任务场景隔离）
        try:
            import llm_client as _lc
            cur = _lc.get_session_token_breakdown()
            _current_task_log["tokens"] = {
                "input": cur["input"] - _token_baseline.get("input", 0),
                "output": cur["output"] - _token_baseline.get("output", 0),
                "by_model": {
                    m: {
                        "input": b["input"] - _token_baseline.get("by_model", {}).get(m, {}).get("input", 0),
                        "output": b["output"] - _token_baseline.get("by_model", {}).get(m, {}).get("output", 0),
                    }
                    for m, b in cur["by_model"].items()
                },
            }
        except Exception:
            pass
        # 在锁内构建好 payload，但实际写盘 IO 释放锁后做
        snapshot = dict(_current_task_log)
        _last_task_log.clear()
        _last_task_log.update(_current_task_log)
        _current_task_log.clear()
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    (_LOG_DIR / f"{ts}.jsonl").write_text(
        json.dumps(snapshot, ensure_ascii=False), encoding="utf-8"
    )


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
            # task_complete_signal：标注 LLM 是否主动声明，让历史能看出"自然收尾 vs 兜底退出"
            sig = e.get("task_complete_signal")
            sig_tag = ""
            if sig:
                sig_tag = " | TC:" + ("ok" if sig.get("success") else "give-up")
            console.print(f"{ts} | {res} | {dur}s | {att}次{sig_tag} | {req}", highlight=False)
        except Exception:
            continue


def get_last_task_log() -> dict:
    return dict(_last_task_log)


def get_current_log() -> dict:
    """供 _run() / _auto_generate_tests 等填充 plan / test_command 字段"""
    return _current_task_log


def record_file_modified(filename: str):
    if filename:
        with _log_lock:
            _task_files_modified.append(filename)


def record_tool_call(name: str, safe_args: dict):
    with _log_lock:
        _task_tool_calls.append({"name": name, "args": safe_args})


def snapshot_files_modified() -> list:
    """返回 _task_files_modified 的快照副本（线程安全）"""
    with _log_lock:
        return list(_task_files_modified)


def snapshot_tool_calls() -> list:
    """返回 _task_tool_calls 的快照副本（线程安全）"""
    with _log_lock:
        return list(_task_tool_calls)
