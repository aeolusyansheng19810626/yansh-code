# 补充10: 并行编排——每子任务独立 worktree + 独立进程(决定 threading.Lock 是否跨进程 bug)
# parallel_orchestrator.py 1-110
"""worktree 并行编排：为每个子任务建 git worktree，各起一个 yansh 进程并行跑。

run_parallel(tasks, workers, base_cwd) -> int
  tasks   : list[dict]，每项 {"name": str, "prompt": str, "mode": str(可选)}
  workers : 最大并发子进程数
  base_cwd: 主仓库 workspace 绝对路径（git 仓库根）
  返回：0=全部成功，1=有任意失败/超时/setup失败
"""
import concurrent.futures
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import procutil
from console_shared import console

TOOL_HOME = os.path.dirname(os.path.abspath(__file__))
_TASK_TIMEOUT = 1800  # 单任务最大等待秒数
_NAME_RE = re.compile(r'^[A-Za-z0-9._-]+$')


def _git(args: list, cwd: str, timeout: int = 30):
    """执行 git 命令，返回 (rc, out, err)。失败兜底返回 (-1, "", msg)。"""
    try:
        r = subprocess.run(
            ["git"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except FileNotFoundError as e:
        return -1, "", f"git not found: {e}"
    except subprocess.TimeoutExpired as e:
        return -1, "", f"git timeout: {e}"


def _run_task(task: dict, base_cwd: str) -> dict:
    """worker 函数：在已建好的 worktree 内起 yansh 进程，返回结果 dict。"""
    name = task["name"]
    prompt = task["prompt"]
    mode = task.get("mode", "code")
    wt_path = str(Path(base_cwd) / ".yansh" / "worktrees" / name)
    branch = f"yansh/{name}"

    result = {
        "name": name,
        "branch": branch,
        "worktree": wt_path,
        "exit_code": None,
        "success": False,
        "files_modified": None,
        "cost_usd": None,
        "elapsed_sec": None,
        "status": "done",
        "committed": False,
        "commit_sha": "",
        "commit_note": "",
    }

    cmd = [
        sys.executable, "-m", "main",
        "--cwd", wt_path,
        "--mode", mode,
        "--json", prompt,
    ]
    try:
        proc = procutil.spawn_with_pgroup(
            cmd,
            cwd=TOOL_HOME,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        try:
            out, err = proc.communicate(timeout=_TASK_TIMEOUT)
            result["exit_code"] = proc.returncode
        except subprocess.TimeoutExpired:
            procutil.kill_tree(proc)
            out, err = proc.communicate()
            result["exit_code"] = proc.returncode
            result["status"] = "timeout"
            return result
    except Exception as e:
        result["exit_code"] = -1
        result["status"] = f"spawn_error: {e}"
        return result

    # 解析 JSON 输出（取最后一行 JSON）
    parse_ok = False
    for line in reversed(out.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            log = json.loads(line)
            result["success"] = bool(log.get("success", False))
            result["files_modified"] = log.get("files_modified")
            result["cost_usd"] = log.get("cost_usd")
            result["elapsed_sec"] = log.get("elapsed_sec")
            parse_ok = True
            break
