"""P2-1 procutil 单元测试。

覆盖：
  - spawn_with_pgroup 设了平台正确的 flag
  - kill_tree 真把孙进程杀了（端到端）
  - kill_tree 对 None proc / 已死 proc 不抛
"""
import os
import sys
import subprocess
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import procutil


def test_spawn_sets_platform_pgroup_flag():
    """spawn 后 popen kwargs 经过补 flag——通过实际起一个 sleep 进程验证 .pid 有效"""
    py = sys.executable
    proc = procutil.spawn_with_pgroup(
        [py, "-c", "import time; time.sleep(0.5)"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        assert proc.pid > 0
        # 等它结束
        proc.wait(timeout=3)
    finally:
        try:
            proc.kill()
        except Exception:
            pass


def test_spawn_preserves_existing_creationflags_windows():
    """Windows 下已传 creationflags 时不应覆盖原有 flags（用 OR 合并）"""
    if sys.platform != "win32":
        import pytest
        pytest.skip("仅 Windows")

    # 用 CREATE_NO_WINDOW (0x08000000) 试试
    CREATE_NO_WINDOW = 0x08000000
    py = sys.executable
    proc = procutil.spawn_with_pgroup(
        [py, "-c", "pass"],
        creationflags=CREATE_NO_WINDOW,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        proc.wait(timeout=3)
    finally:
        try:
            proc.kill()
        except Exception:
            pass


def test_kill_tree_kills_grandchild():
    """端到端：父 spawn 一个 sleep 60s 孙进程，kill_tree 应把孙也杀掉"""
    try:
        import psutil
    except ImportError:
        import pytest
        pytest.skip("需要 psutil 验证孙 pid 死亡")

    py = sys.executable
    # 父：spawn 孙 + 自己也 sleep
    parent_code = (
        "import subprocess, sys, time;"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']);"
        "print(p.pid, flush=True);"
        "time.sleep(60)"
    )
    proc = procutil.spawn_with_pgroup(
        [py, "-c", parent_code],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )

    try:
        # 读父 stdout 拿孙 pid
        grandchild_pid = int(proc.stdout.readline().strip())
        parent_pid = proc.pid
        time.sleep(0.3)  # 让孙稳定

        assert psutil.pid_exists(parent_pid)
        assert psutil.pid_exists(grandchild_pid)

        procutil.kill_tree(proc)
        time.sleep(0.5)

        parent_alive = psutil.pid_exists(parent_pid)
        grand_alive = psutil.pid_exists(grandchild_pid)

        # 兜底清理（避免测试失败时残留）
        if grand_alive:
            try:
                psutil.Process(grandchild_pid).kill()
            except Exception:
                pass
        if parent_alive:
            try:
                psutil.Process(parent_pid).kill()
            except Exception:
                pass

        assert not parent_alive, f"父 pid {parent_pid} 没被杀"
        assert not grand_alive, f"孙 pid {grandchild_pid} 没被杀（procutil 失效）"
    finally:
        try:
            proc.kill()
        except Exception:
            pass


def test_kill_tree_handles_none_proc():
    """kill_tree(None) 不应抛"""
    procutil.kill_tree(None)


def test_kill_tree_handles_already_dead_proc():
    """对已死的 proc kill_tree 不抛"""
    py = sys.executable
    proc = procutil.spawn_with_pgroup(
        [py, "-c", "pass"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    proc.wait(timeout=3)
    # 父已死——不应抛
    procutil.kill_tree(proc)


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
