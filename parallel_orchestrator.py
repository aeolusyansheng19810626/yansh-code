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
        except Exception:
            continue
    if not parse_ok:
        result["status"] = "parse_error"

    # 自动 commit worktree 改动（排除 yansh 自身产物）
    _commit_worktree(result, name, prompt, wt_path)

    return result


def _commit_worktree(result: dict, name: str, prompt: str, wt_path: str) -> None:
    """在 wt_path 内自动 commit：排除 .yansh/ 和 .yansh_history.json。结果写回 result。"""
    # a. 先看有无改动
    rc_st, out_st, _ = _git(["status", "--porcelain"], cwd=wt_path)
    if rc_st != 0:
        result["commit_note"] = "git status 失败，跳过 commit"
        return

    # 过滤掉 yansh 产物，判断是否有实质改动
    real_changes = [
        line for line in out_st.splitlines()
        if line.strip()
        and not _is_yansh_artifact(line[3:].strip())
    ]
    if not real_changes:
        result["commit_note"] = "无改动"
        return

    # b. add 全部改动（无 pathspec，自动遵守 .gitignore 静默跳过 ignored），
    #    再把 yansh 落盘产物从暂存区撤出。两种情况都安全：
    #    用户 .gitignore 已忽略 → add -A 跳过、reset no-op；未忽略 → reset 撤下。
    #    （不用 `add -- . :(exclude)`：当 .yansh 被 gitignore 时显式 pathspec `.` 会报错退出）
    rc_add, _, err_add = _git(["add", "-A"], cwd=wt_path)
    if rc_add != 0:
        result["commit_note"] = f"git add 失败: {err_add}"
        return
    # 撤下 yansh 产物（未被 stage 时 no-op，不报错）
    _git(["reset", "-q", "--", ".yansh", ".yansh_history.json"], cwd=wt_path)

    # c. 判断暂存区是否真有内容
    rc_diff, _, _ = _git(["diff", "--cached", "--quiet"], cwd=wt_path)
    if rc_diff == 0:
        # 暂存区无内容（实际上没有可提交的变更）
        result["commit_note"] = "无改动"
        return

    # d. commit
    prompt_snippet = prompt[:200].replace("\n", " ")
    commit_msg = f"yansh并行任务: {name}\n\n{prompt_snippet}"
    rc_cm, _, err_cm = _git(["commit", "-m", commit_msg], cwd=wt_path)
    if rc_cm != 0:
        result["commit_note"] = f"commit 失败: {err_cm}"
        return

    # 取短 SHA
    rc_sha, sha, _ = _git(["rev-parse", "--short", "HEAD"], cwd=wt_path)
    result["committed"] = True
    result["commit_sha"] = sha if rc_sha == 0 else ""
    result["commit_note"] = "ok"


def _is_yansh_artifact(path: str) -> bool:
    """判断路径是否为 yansh 自身产物（不应进入 commit）。"""
    p = path.replace("\\", "/")
    return p == ".yansh_history.json" or p.startswith(".yansh/") or p == ".yansh"


def run_parallel(tasks: list, workers: int, base_cwd: str) -> int:
    """并行编排入口，返回 0=全成功，1=有失败。"""
    # 前置校验：是否 git 仓库
    rc, _, err = _git(["rev-parse", "--is-inside-work-tree"], cwd=base_cwd)
    if rc != 0:
        console.print(f"workspace 不是 git 仓库，无法并行隔离：{err}", style="red", highlight=False)
        return 1

    # 取 HEAD ref
    rc2, base_ref, err2 = _git(["rev-parse", "HEAD"], cwd=base_cwd)
    if rc2 != 0:
        console.print(f"无法获取 HEAD：{err2}", style="red", highlight=False)
        return 1

    # 校验 tasks
    if not tasks:
        console.print("任务列表为空", style="red", highlight=False)
        return 1
    seen_names: set = set()
    for i, t in enumerate(tasks):
        name = t.get("name", "")
        prompt = t.get("prompt", "")
        if not name or not prompt:
            console.print(f"task[{i}] name/prompt 不能为空", style="red", highlight=False)
            return 1
        if not _NAME_RE.match(name):
            console.print(
                f"task[{i}] name={name!r} 含非法字符（只允许 A-Za-z0-9._-）",
                style="red",
                highlight=False,
            )
            return 1
        if name in seen_names:
            console.print(f"task name 重复：{name!r}", style="red", highlight=False)
            return 1
        seen_names.add(name)

    # 建 worktree
    valid_tasks = []
    skipped = []
    for t in tasks:
        name = t["name"]
        wt_path = str(Path(base_cwd) / ".yansh" / "worktrees" / name)
        branch = f"yansh/{name}"
        rc3, out3, err3 = _git(
            ["worktree", "add", wt_path, "-b", branch, base_ref],
            cwd=base_cwd,
        )
        if rc3 != 0:
            console.print(
                f"worktree setup 失败 [{name}]：{err3}（已跳过）",
                style="yellow",
                highlight=False,
            )
            skipped.append({
                "name": name,
                "branch": branch,
                "worktree": wt_path,
                "exit_code": None,
                "success": False,
                "files_modified": None,
                "cost_usd": None,
                "elapsed_sec": None,
                "status": "setup_failed",
            })
        else:
            valid_tasks.append(t)

    results = list(skipped)

    # 并行跑有效 tasks
    if valid_tasks:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(_run_task, t, base_cwd): t["name"]
                for t in valid_tasks
            }
            for fut in concurrent.futures.as_completed(futures):
                try:
                    res = fut.result()
                except Exception as e:
                    name = futures[fut]
                    res = {
                        "name": name,
                        "branch": f"yansh/{name}",
                        "worktree": str(Path(base_cwd) / ".yansh" / "worktrees" / name),
                        "exit_code": -1,
                        "success": False,
                        "files_modified": None,
                        "cost_usd": None,
                        "elapsed_sec": None,
                        "status": f"future_error: {e}",
                    }
                results.append(res)

    # 汇总表（固定宽度，files 列只显示数量）
    console.print("\n[bold cyan]=== 并行任务汇总 ===[/bold cyan]", highlight=False)
    header = (
        f"{'name':<18} {'branch':<24} {'ok':<6} {'commit':<8} "
        f"{'#files':>6} {'cost_usd':>9} {'status':<16}"
    )
    sep = "-" * len(header)
    console.print(header, highlight=False)
    console.print(sep, highlight=False)
    for r in results:
        cost_str = f"{r['cost_usd']:.4f}" if r["cost_usd"] is not None else "-"
        _fm = r["files_modified"]
        if _fm is None:
            files_str = "-"
        elif isinstance(_fm, int):
            files_str = str(_fm)
        else:
            files_str = str(len(_fm))
        committed_flag = r.get("committed", False)
        commit_str = "Y" if committed_flag else "N"
        name_col = r["name"][:18]
        branch_col = r["branch"][:24]
        status_col = r["status"][:16]
        line = (
            f"{name_col:<18} {branch_col:<24} "
            f"{str(r['success']):<6} {commit_str:<8} "
            f"{files_str:>6} {cost_str:>9} {status_col:<16}"
        )
        console.print(line, highlight=False)

    # 手动合并指引
    console.print("\n[bold]手动合并指引（在主仓库执行）：[/bold]", highlight=False)
    can_merge = [r for r in results if r.get("committed", False)]
    no_commit = [r for r in results if not r.get("committed", False)]
    if can_merge:
        console.print("# 有 commit 可 merge：", highlight=False)
        for r in can_merge:
            sha_hint = f"  # {r['commit_sha']}" if r.get("commit_sha") else ""
            console.print(f"  git merge {r['branch']}{sha_hint}", highlight=False)
    if no_commit:
        console.print("# 无 commit（不需 merge）：", highlight=False)
        for r in no_commit:
            note = r.get("commit_note", "")
            status = r.get("status", "")
            if note == "无改动":
                hint = "无改动，可直接删除"
            elif note.startswith(("commit 失败", "git add 失败", "git status 失败")):
                hint = f"commit失败，需人工查看 worktree: {note}"
            elif status == "timeout":
                hint = "任务超时，worktree 可能有未提交改动，需人工查看"
            elif status.startswith(("spawn_error", "future_error", "parse_error")):
                hint = f"未正常完成（{status}），需人工查看 worktree"
            elif status == "setup_failed":
                hint = "worktree setup 失败，无需处理"
            else:
                hint = note or status or "未知"
            console.print(f"  # {r['branch']} — {hint}", highlight=False)
    console.print("\n# 清理 worktree 和分支：", highlight=False)
    for r in results:
        console.print(f"  git worktree remove {r['worktree']}", highlight=False)
        console.print(f"  git branch -D {r['branch']}", highlight=False)

    # 返回码
    any_fail = any(
        not r["success"] or r["status"] not in ("done",)
        for r in results
    )
    return 1 if any_fail else 0
