"""跨平台子进程管理（P2 重构：mcp_client / hooks 共用）

抽出共同的两件事：
  1. spawn_with_pgroup(cmd, ...) -> Popen
     起子进程时把它放到独立进程组——POSIX 用 start_new_session=True，
     Windows 用 CREATE_NEW_PROCESS_GROUP。这样后面 kill 时能整组清掉。
  2. kill_tree(proc) -> None
     杀整棵进程树（父 + 所有后代）。psutil 优先（最可靠跨平台），
     fallback 平台原生（taskkill /F /T / killpg SIGKILL）。

为什么 psutil 优先？
  - Windows Popen → Popen 链不构成 Job Object，taskkill /T /PID 父pid
    找不到孙进程
  - psutil.Process.children(recursive=True) 走 OS 父子关系链——能真正
    "看见全树"，是跨平台一致最可靠的方案
  - psutil 没装时 fallback 到平台原生（覆盖 90% 简单情况）

历史：mcp_client.MCPServer._kill_tree 第一版是私有的，shutdown 时用；
hooks._run_one_hook 早期 timeout 路径也复制了一份 taskkill/killpg。两边
逻辑会漂移——抽到这里统一。
"""
from __future__ import annotations

import os
import subprocess
import sys as _sys
from typing import Optional, Sequence


def spawn_with_pgroup(cmd: Sequence[str], **popen_kwargs) -> subprocess.Popen:
    """起子进程并放进独立进程组（便于后续 kill_tree 清理孙进程）。

    cmd: 命令 + 参数列表（同 subprocess.Popen 第一参数）
    popen_kwargs: 透传给 Popen——不要传 creationflags / start_new_session，
                  本函数会按平台自动加。
    """
    if _sys.platform == "win32":
        existing = popen_kwargs.get("creationflags", 0)
        popen_kwargs["creationflags"] = existing | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True
    return subprocess.Popen(cmd, **popen_kwargs)


def kill_tree(proc: subprocess.Popen, timeout: float = 2.0) -> None:
    """杀整棵进程树（父 + 所有后代）。失败也尽量清理（不抛异常）。

    必须趁父进程还活着调用——父退后 psutil 没法枚举到孤儿孙。

    timeout: 等所有 victims 实际退出的最大秒数（psutil.wait_procs）。
    """
    if proc is None:
        return
    pid = proc.pid

    # 路径 1：psutil（最可靠）
    try:
        import psutil as _psutil
        try:
            root = _psutil.Process(pid)
            victims = root.children(recursive=True) + [root]
            for p in victims:
                try:
                    p.kill()
                except _psutil.NoSuchProcess:
                    pass
                except Exception:
                    pass
            try:
                _psutil.wait_procs(victims, timeout=timeout)
            except Exception:
                pass
            return
        except _psutil.NoSuchProcess:
            # 父已死——孤儿孙没法通过父枚举找到
            return
    except ImportError:
        pass

    # 路径 2：平台原生（无 psutil 时兜底）
    try:
        if _sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True, timeout=3,
            )
        else:
            import signal as _signal
            os.killpg(os.getpgid(pid), _signal.SIGKILL)
    except Exception:
        # 兜底——至少杀直接子进程
        try:
            proc.kill()
        except Exception:
            pass
