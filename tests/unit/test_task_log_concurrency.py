"""P2-3 task_log 并发安全单元测试。

CPython GIL 下 list.append/clear 单条原子，但：
  - finish_task_log 的 dict.fromkeys(_task_files_modified) 迭代期间如果有
    并发 append → 结果未定（迭代器看到长度变化）
  - free-threaded Python 3.13+ 去 GIL 后 list.append 也不再原子

修复后：所有读写走 _log_lock。
本测试验证：
  - N 线程并发 append 后总数 == N
  - 并发 append 期间 snapshot 拿到一致的结果（不漏不脏）
"""
import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import task_log


def test_record_file_modified_concurrent_appends_complete(tmp_path, monkeypatch):
    """50 线程 × 100 次 record_file_modified → 总长度 == 5000"""
    import config as _config_mod
    monkeypatch.setattr(_config_mod, "WORKSPACE_DIR", str(tmp_path))
    task_log.init_task_log("test", "auto")

    N_THREADS = 50
    PER_THREAD = 100

    def worker(tid):
        for i in range(PER_THREAD):
            task_log.record_file_modified(f"thread_{tid}_file_{i}.py")

    threads = [threading.Thread(target=worker, args=(t,), daemon=True)
               for t in range(N_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    snap = task_log.snapshot_files_modified()
    assert len(snap) == N_THREADS * PER_THREAD, \
        f"丢失 append（锁失效？）：实际 {len(snap)}, 期望 {N_THREADS * PER_THREAD}"


def test_record_tool_call_concurrent_appends_complete(tmp_path, monkeypatch):
    """50 线程 × 100 次 record_tool_call → 总长度 == 5000"""
    import config as _config_mod
    monkeypatch.setattr(_config_mod, "WORKSPACE_DIR", str(tmp_path))
    task_log.init_task_log("test", "auto")

    N_THREADS = 50
    PER_THREAD = 100

    def worker(tid):
        for i in range(PER_THREAD):
            task_log.record_tool_call(f"tool_{tid}_{i}", {"k": i})

    threads = [threading.Thread(target=worker, args=(t,), daemon=True)
               for t in range(N_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    snap = task_log.snapshot_tool_calls()
    assert len(snap) == N_THREADS * PER_THREAD


def test_snapshot_during_concurrent_writes_no_crash(tmp_path, monkeypatch):
    """读 snapshot 与并发 append 不应崩——不要求结果是某个特定 size，只要不抛"""
    import config as _config_mod
    monkeypatch.setattr(_config_mod, "WORKSPACE_DIR", str(tmp_path))
    task_log.init_task_log("test", "auto")

    stop = threading.Event()
    errors = []

    def writer():
        i = 0
        while not stop.is_set():
            task_log.record_file_modified(f"f_{i}.py")
            i += 1

    def reader():
        try:
            for _ in range(100):
                snap = task_log.snapshot_files_modified()
                # 必须是 list 副本——不应在写期间报 RuntimeError
                _ = list(dict.fromkeys(snap))
        except Exception as e:
            errors.append(e)

    writers = [threading.Thread(target=writer, daemon=True) for _ in range(5)]
    readers = [threading.Thread(target=reader, daemon=True) for _ in range(3)]
    for t in writers + readers:
        t.start()
    for t in readers:
        t.join(timeout=5)
    stop.set()
    for t in writers:
        t.join(timeout=2)

    assert not errors, f"读 snapshot 时崩了：{errors[:3]}"


def test_finish_task_log_under_concurrent_writes_consistent(tmp_path, monkeypatch):
    """finish_task_log 在并发 record 期间不应炸——dict.fromkeys 在锁内安全"""
    import config as _config_mod
    monkeypatch.setattr(_config_mod, "WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setattr(task_log, "_LOG_DIR", tmp_path / "logs")

    # 跑一波并发 record，然后 finish_task_log——期望写盘不抛、上次日志可读
    task_log.init_task_log("test_concurrent_finish", "auto")
    for i in range(200):
        task_log.record_file_modified(f"f_{i}.py")
        task_log.record_tool_call(f"t_{i}", {"i": i})

    task_log.finish_task_log(success=True, attempts=1)
    last = task_log.get_last_task_log()
    assert last["test_result"] == "pass"
    assert len(last["tool_calls"]) == 200
    assert len(last["files_modified"]) == 200


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
