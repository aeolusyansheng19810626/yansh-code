"""快照单元测试（方案 A：纯文件复制，不动用户 git 状态）"""
import os
import sys
import subprocess
import pytest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))


def _git(args, cwd):
    subprocess.run(["git"] + args, cwd=str(cwd),
                   capture_output=True, check=False)


@pytest.fixture
def git_ws(tmp_path):
    """初始化一个带 git 的临时 workspace（用于验证我们不会污染用户的 git 状态）"""
    import config, tools, agent, snapshot
    ws = tmp_path / "workspace"
    ws.mkdir()
    _git(["init"], ws)
    _git(["config", "user.email", "test@test.com"], ws)
    _git(["config", "user.name", "Test"], ws)
    (ws / "init.txt").write_text("init")
    _git(["add", "."], ws)
    _git(["commit", "-m", "init"], ws)

    original_ws = config.WORKSPACE_DIR
    config.set_workspace_dir(str(ws))
    tools._reinit_paths()
    agent._reinit_paths()

    yield ws

    config.set_workspace_dir(original_ws)
    tools._reinit_paths()
    agent._reinit_paths()


@pytest.fixture
def file_ws(tmp_path):
    """没有 git 的普通 workspace"""
    import config, tools, agent, snapshot
    ws = tmp_path / "plain_ws"
    ws.mkdir()
    original_ws = config.WORKSPACE_DIR
    config.set_workspace_dir(str(ws))
    tools._reinit_paths()
    agent._reinit_paths()
    yield ws
    config.set_workspace_dir(original_ws)
    tools._reinit_paths()
    agent._reinit_paths()


# ── 方案 A：永远走 file 模式，不动 git ────────────────────────────────────────

def test_snapshot_always_file_mode_in_git_repo(git_ws):
    """即使 workspace 在 git 仓库内，也走 file 模式（不再用 git stash）"""
    from agent import create_snapshot
    (git_ws / "new_file.py").write_text("x = 1")
    snap = create_snapshot(["new_file.py"])
    assert snap["mode"] == "file"


def test_snapshot_does_not_touch_user_git_state(git_ws):
    """create_snapshot 不能污染用户的工作树和 stash 列表"""
    from agent import create_snapshot, cleanup_snapshot
    # 用户手动改了一个未提交的文件
    (git_ws / "user_wip.py").write_text("user manual edit")
    # 拍快照
    snap = create_snapshot(["user_wip.py"])
    cleanup_snapshot(snap)

    # 用户的工作树状态不变：文件还在、内容没变
    assert (git_ws / "user_wip.py").read_text() == "user manual edit"
    # stash 列表应该是空（我们不再用 git stash）
    r = subprocess.run(["git", "stash", "list"], cwd=str(git_ws),
                       capture_output=True, text=True)
    assert r.stdout.strip() == ""


def test_snapshot_clean_workspace_still_records(file_ws):
    """工作区干净（file_list 中无文件存在）时仍创建快照目录，meta.files 为空"""
    from agent import create_snapshot
    snap = create_snapshot([])
    assert snap["mode"] == "file"
    assert Path(snap["path"]).exists()


def test_backup_file_if_needed_incremental(file_ws):
    """LLM 写入前的增量备份：未在 baseline 中的文件首次写入时被备份"""
    from agent import create_snapshot, _backup_file_if_needed, restore_snapshot, cleanup_snapshot
    (file_ws / "later.py").write_text("baseline content")
    # 创建快照时不指定 later.py（它不在 file_list）
    snap = create_snapshot([])

    # 模拟 LLM 即将改 later.py，触发增量备份
    _backup_file_if_needed(snap, "later.py")
    # 然后真的改
    (file_ws / "later.py").write_text("modified by agent")

    n = restore_snapshot(snap)
    assert n == 1
    assert (file_ws / "later.py").read_text() == "baseline content"
    cleanup_snapshot(snap)


def test_gc_old_snapshots(file_ws):
    """_gc_old_snapshots 保留最近 N 个，删掉更老的"""
    from snapshot import _SNAPSHOT_DIR, _gc_old_snapshots
    _SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    # 制造 5 个排序在前的旧目录 + 5 个排序在后的新目录
    for i in range(5):
        (_SNAPSHOT_DIR / f"20200101-00000{i}").mkdir()
    for i in range(5):
        (_SNAPSHOT_DIR / f"20300101-00000{i}").mkdir()
    _gc_old_snapshots(keep=5)
    remaining = sorted(d.name for d in _SNAPSHOT_DIR.iterdir())
    assert all(name.startswith("2030") for name in remaining)
    assert len(remaining) == 5


# ── 文件复制模式（与方案 A 对应的全部行为） ──────────────────────────────────

def test_file_snapshot_creates_directory(file_ws):
    from agent import create_snapshot
    (file_ws / "data.txt").write_text("important data")
    snap = create_snapshot(["data.txt"])
    assert snap["mode"] == "file"
    snap_dir = Path(snap["path"])
    assert snap_dir.exists()
    assert (snap_dir / "data.txt").exists()


def test_file_snapshot_restore(file_ws):
    from agent import create_snapshot, restore_snapshot, cleanup_snapshot
    (file_ws / "a.txt").write_text("original content")
    snap = create_snapshot(["a.txt"])

    (file_ws / "a.txt").write_text("modified content")
    n = restore_snapshot(snap)
    assert n == 1
    assert (file_ws / "a.txt").read_text() == "original content"
    cleanup_snapshot(snap)


def test_file_snapshot_deletes_new_files(file_ws):
    from agent import create_snapshot, restore_snapshot, cleanup_snapshot
    (file_ws / "existing.txt").write_text("exists before snapshot")
    snap = create_snapshot(["existing.txt"])

    (file_ws / "brand_new.txt").write_text("should be deleted on restore")
    restore_snapshot(snap)
    assert not (file_ws / "brand_new.txt").exists()
    cleanup_snapshot(snap)


def test_file_snapshot_cleanup_removes_dir(file_ws):
    from agent import create_snapshot, cleanup_snapshot
    (file_ws / "f.txt").write_text("hi")
    snap = create_snapshot(["f.txt"])
    snap_dir = Path(snap["path"])
    assert snap_dir.exists()
    cleanup_snapshot(snap)
    assert not snap_dir.exists()


def test_file_snapshot_none_input(file_ws):
    from agent import restore_snapshot
    assert restore_snapshot(None) == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
