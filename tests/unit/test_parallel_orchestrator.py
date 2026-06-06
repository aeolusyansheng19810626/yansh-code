"""parallel_orchestrator.py 单测：mock _git 和子进程，测校验逻辑与汇总返回码。"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import pytest
import parallel_orchestrator as po


# ---------- helpers ----------

def _make_git_success(base_ref="abc1234"):
    """返回一个 mock _git，is-inside-work-tree 和 rev-parse HEAD 都成功。"""
    call_count = {"n": 0}

    def mock_git(args, cwd, timeout=30):
        call_count["n"] += 1
        if "is-inside-work-tree" in args:
            return (0, "true", "")
        if "rev-parse" in args and "HEAD" in args:
            return (0, base_ref, "")
        if "worktree" in args and "add" in args:
            return (0, "", "")
        return (0, "", "")

    return mock_git, call_count


# ---------- tests ----------

def test_non_git_workspace_returns_1(monkeypatch, tmp_path):
    """非 git 仓库时 run_parallel 应返回 1。"""
    def mock_git(args, cwd, timeout=30):
        if "is-inside-work-tree" in args:
            return (128, "", "not a git repository")
        return (0, "", "")

    monkeypatch.setattr(po, "_git", mock_git)
    tasks = [{"name": "task1", "prompt": "do something"}]
    rc = po.run_parallel(tasks, workers=1, base_cwd=str(tmp_path))
    assert rc == 1


def test_invalid_name_returns_1(monkeypatch, tmp_path):
    """task name 含非法字符时应返回 1。"""
    mock_git, _ = _make_git_success()
    monkeypatch.setattr(po, "_git", mock_git)
    tasks = [{"name": "bad name!", "prompt": "do something"}]
    rc = po.run_parallel(tasks, workers=1, base_cwd=str(tmp_path))
    assert rc == 1


def test_duplicate_name_returns_1(monkeypatch, tmp_path):
    """task name 重复时应返回 1。"""
    mock_git, _ = _make_git_success()
    monkeypatch.setattr(po, "_git", mock_git)
    tasks = [
        {"name": "alpha", "prompt": "p1"},
        {"name": "alpha", "prompt": "p2"},
    ]
    rc = po.run_parallel(tasks, workers=1, base_cwd=str(tmp_path))
    assert rc == 1


def test_empty_name_returns_1(monkeypatch, tmp_path):
    """空 name 时应返回 1。"""
    mock_git, _ = _make_git_success()
    monkeypatch.setattr(po, "_git", mock_git)
    tasks = [{"name": "", "prompt": "do something"}]
    rc = po.run_parallel(tasks, workers=1, base_cwd=str(tmp_path))
    assert rc == 1


def test_empty_prompt_returns_1(monkeypatch, tmp_path):
    """空 prompt 时应返回 1。"""
    mock_git, _ = _make_git_success()
    monkeypatch.setattr(po, "_git", mock_git)
    tasks = [{"name": "task1", "prompt": ""}]
    rc = po.run_parallel(tasks, workers=1, base_cwd=str(tmp_path))
    assert rc == 1


def test_empty_tasks_returns_1(monkeypatch, tmp_path):
    """空任务列表应返回 1。"""
    mock_git, _ = _make_git_success()
    monkeypatch.setattr(po, "_git", mock_git)
    rc = po.run_parallel([], workers=1, base_cwd=str(tmp_path))
    assert rc == 1


def test_worktree_setup_failure_skipped_returns_1(monkeypatch, tmp_path):
    """worktree add 失败的任务被标记 setup_failed，整体返回 1。"""
    def mock_git(args, cwd, timeout=30):
        if "is-inside-work-tree" in args:
            return (0, "true", "")
        if "rev-parse" in args and "HEAD" in args:
            return (0, "abc123", "")
        if "worktree" in args and "add" in args:
            return (128, "", "branch already exists")
        return (0, "", "")

    monkeypatch.setattr(po, "_git", mock_git)
    # 所有 worktree add 都失败 → valid_tasks 为空 → 汇总只有 skipped
    tasks = [{"name": "task1", "prompt": "do something"}]
    rc = po.run_parallel(tasks, workers=1, base_cwd=str(tmp_path))
    assert rc == 1


def _full_result(name, tmp_path, *, success=True, committed=True, commit_sha="abc1234",
                  commit_note="ok", files_modified=2, cost_usd=0.01,
                  elapsed_sec=5.0, status="done", exit_code=0):
    """构造完整结果 dict（含新字段）。"""
    return {
        "name": name,
        "branch": f"yansh/{name}",
        "worktree": str(tmp_path / ".yansh" / "worktrees" / name),
        "exit_code": exit_code,
        "success": success,
        "files_modified": files_modified,
        "cost_usd": cost_usd,
        "elapsed_sec": elapsed_sec,
        "status": status,
        "committed": committed,
        "commit_sha": commit_sha,
        "commit_note": commit_note,
    }


def test_all_success_returns_0(monkeypatch, tmp_path):
    """
    所有 task worktree 建成功，子进程返回 success=True 的 JSON，整体返回 0。
    mock _run_task 直接返回成功结果（含 committed 字段），绕开真实子进程 spawn。
    """
    mock_git, _ = _make_git_success()
    monkeypatch.setattr(po, "_git", mock_git)

    def mock_run_task(task, base_cwd):
        return _full_result(task["name"], tmp_path, committed=True)

    monkeypatch.setattr(po, "_run_task", mock_run_task)

    tasks = [
        {"name": "feat-a", "prompt": "add feature A"},
        {"name": "feat-b", "prompt": "add feature B"},
    ]
    rc = po.run_parallel(tasks, workers=2, base_cwd=str(tmp_path))
    assert rc == 0


def test_one_failure_returns_1(monkeypatch, tmp_path):
    """一个成功一个失败，整体返回 1。"""
    mock_git, _ = _make_git_success()
    monkeypatch.setattr(po, "_git", mock_git)

    def mock_run_task(task, base_cwd):
        if task["name"] == "ok-task":
            return _full_result("ok-task", tmp_path, committed=True)
        return _full_result("fail-task", tmp_path, success=False,
                            committed=False, commit_note="无改动",
                            files_modified=0, cost_usd=0.005,
                            elapsed_sec=2, exit_code=1)

    monkeypatch.setattr(po, "_run_task", mock_run_task)

    tasks = [
        {"name": "ok-task", "prompt": "ok"},
        {"name": "fail-task", "prompt": "fail"},
    ]
    rc = po.run_parallel(tasks, workers=2, base_cwd=str(tmp_path))
    assert rc == 1


def test_commit_worktree_with_changes(monkeypatch, tmp_path):
    """_commit_worktree：有改动时应 committed=True，记录 commit_sha。"""
    call_log = []

    def mock_git(args, cwd, timeout=30):
        call_log.append(args[0] if args else "")
        first = args[0] if args else ""
        if first == "status":
            return (0, "M  calc.py", "")
        if first == "add":
            return (0, "", "")
        if first == "diff":
            # --cached --quiet: rc=1 表示有暂存改动
            return (1, "", "")
        if first == "commit":
            return (0, "", "")
        if first == "rev-parse":
            return (0, "deadbee", "")
        return (0, "", "")

    monkeypatch.setattr(po, "_git", mock_git)

    result = {"committed": False, "commit_sha": "", "commit_note": ""}
    po._commit_worktree(result, "feat-x", "add calc function", str(tmp_path))

    assert result["committed"] is True
    assert result["commit_sha"] == "deadbee"
    assert result["commit_note"] == "ok"
    assert "status" in call_log
    assert "add" in call_log
    assert "commit" in call_log


def test_commit_worktree_no_changes(monkeypatch, tmp_path):
    """_commit_worktree：无改动时 committed=False，commit_note='无改动'。"""
    def mock_git(args, cwd, timeout=30):
        if args[0] == "status":
            return (0, "", "")   # 空 = 无改动
        return (0, "", "")

    monkeypatch.setattr(po, "_git", mock_git)

    result = {"committed": False, "commit_sha": "", "commit_note": ""}
    po._commit_worktree(result, "doc-only", "update readme", str(tmp_path))

    assert result["committed"] is False
    assert result["commit_note"] == "无改动"


def test_commit_worktree_only_yansh_artifacts(monkeypatch, tmp_path):
    """_commit_worktree：status 只有 yansh 产物时，视为无改动跳过 commit。"""
    def mock_git(args, cwd, timeout=30):
        if args[0] == "status":
            # 只有 yansh 产物
            return (0, "M  .yansh_history.json\nM  .yansh/foo.json", "")
        if args[0] == "add":
            return (0, "", "")
        if args[0] == "diff":
            # 暂存区空（pathspec exclude 已排除产物）
            return (0, "", "")
        return (0, "", "")

    monkeypatch.setattr(po, "_git", mock_git)

    result = {"committed": False, "commit_sha": "", "commit_note": ""}
    po._commit_worktree(result, "task1", "do something", str(tmp_path))

    assert result["committed"] is False
    assert result["commit_note"] == "无改动"


def test_commit_worktree_commit_failure(monkeypatch, tmp_path):
    """_commit_worktree：commit 命令失败时记录原因，不抛异常。"""
    def mock_git(args, cwd, timeout=30):
        first = args[0] if args else ""
        if first == "status":
            return (0, "M  app.py", "")
        if first == "add":
            return (0, "", "")
        if first == "diff":
            return (1, "", "")
        if first == "commit":
            return (128, "", "Author identity unknown")
        return (0, "", "")

    monkeypatch.setattr(po, "_git", mock_git)

    result = {"committed": False, "commit_sha": "", "commit_note": ""}
    po._commit_worktree(result, "task2", "fix bug", str(tmp_path))

    assert result["committed"] is False
    assert "Author identity unknown" in result["commit_note"]


def test_is_yansh_artifact():
    """_is_yansh_artifact 正确识别 yansh 产物路径。"""
    assert po._is_yansh_artifact(".yansh_history.json") is True
    assert po._is_yansh_artifact(".yansh/worktrees/x") is True
    assert po._is_yansh_artifact(".yansh") is True
    assert po._is_yansh_artifact("src/app.py") is False
    assert po._is_yansh_artifact(".yanshrc") is False   # 不是产物
