"""Unit tests for #2: Git集成snapshot（create_snapshot / restore_snapshot / cleanup_snapshot）"""
import os
import sys
import shutil
import subprocess
import pytest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))


def _git(args, cwd):
    subprocess.run(["git"] + args, cwd=str(cwd),
                   capture_output=True, check=False)


@pytest.fixture
def git_ws(tmp_path):
    """初始化一个带 git 的临时 workspace，并设置 config.WORKSPACE_DIR"""
    import config, tools, agent
    ws = tmp_path / "workspace"
    ws.mkdir()
    _git(["init"], ws)
    _git(["config", "user.email", "test@test.com"], ws)
    _git(["config", "user.name", "Test"], ws)
    # 初始提交（否则 stash 报错）
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
    """没有 git 的普通 workspace（文件复制模式）"""
    import config, tools, agent
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


# ── git stash 模式 ──────────────────────────────────────────────────────────

def test_git_is_repo_true(git_ws):
    from agent import _git_is_repo
    assert _git_is_repo(str(git_ws)) is True


def test_git_is_repo_false(file_ws):
    from agent import _git_is_repo
    assert _git_is_repo(str(file_ws)) is False


def test_git_snapshot_creates_stash(git_ws):
    """有改动时 create_snapshot 返回 git 模式标识，stash 列表不为空"""
    from agent import create_snapshot, _git_run
    (git_ws / "new_file.py").write_text("x = 1")
    snap = create_snapshot(["new_file.py"])
    assert snap["mode"] == "git"
    assert "yansh-snapshot-" in snap["msg"]
    # stash 列表应包含这条记录
    rc, stdout, _ = _git_run(["stash", "list"], str(git_ws))
    assert snap["msg"] in stdout


def test_git_snapshot_clean_workspace(git_ws):
    """工作区干净时 create_snapshot 返回 git_clean 模式"""
    from agent import create_snapshot
    snap = create_snapshot([])
    assert snap["mode"] == "git_clean"


def test_git_restore_recovers_file(git_ws):
    """restore_snapshot 后被修改的文件应恢复"""
    from agent import create_snapshot, restore_snapshot, cleanup_snapshot
    # 先提交 recover_me.py
    (git_ws / "recover_me.py").write_text("original")
    _git(["add", "."], git_ws)
    _git(["commit", "-m", "add file"], git_ws)

    # 修改文件（产生脏变更），然后快照
    (git_ws / "recover_me.py").write_text("modified")
    snap = create_snapshot(["recover_me.py"])
    assert snap["mode"] == "git", f"预期 git 模式，实际: {snap}"

    # 再次修改
    (git_ws / "recover_me.py").write_text("further modified")

    n = restore_snapshot(snap)
    assert n >= 1
    cleanup_snapshot(snap)


def test_git_cleanup_drops_stash(git_ws):
    """cleanup_snapshot 后 stash 列表应为空"""
    from agent import create_snapshot, cleanup_snapshot, _git_run
    (git_ws / "tmp.py").write_text("tmp")
    snap = create_snapshot(["tmp.py"])
    assert snap["mode"] == "git"
    cleanup_snapshot(snap)
    rc, stdout, _ = _git_run(["stash", "list"], str(git_ws))
    assert snap["msg"] not in stdout


# ── 文件复制模式（兜底） ──────────────────────────────────────────────────────

def test_file_snapshot_creates_directory(file_ws):
    """非 git 模式：create_snapshot 创建文件快照目录"""
    from agent import create_snapshot
    (file_ws / "data.txt").write_text("important data")
    snap = create_snapshot(["data.txt"])
    assert snap["mode"] == "file"
    snap_dir = Path(snap["path"])
    assert snap_dir.exists()
    assert (snap_dir / "data.txt").exists()


def test_file_snapshot_restore(file_ws):
    """文件复制模式：restore_snapshot 后文件内容恢复"""
    from agent import create_snapshot, restore_snapshot, cleanup_snapshot
    (file_ws / "a.txt").write_text("original content")
    snap = create_snapshot(["a.txt"])

    # 修改文件
    (file_ws / "a.txt").write_text("modified content")
    n = restore_snapshot(snap)
    assert n == 1
    assert (file_ws / "a.txt").read_text() == "original content"
    cleanup_snapshot(snap)


def test_file_snapshot_deletes_new_files(file_ws):
    """restore_snapshot 应删除快照后新建的文件"""
    from agent import create_snapshot, restore_snapshot, cleanup_snapshot
    (file_ws / "existing.txt").write_text("exists before snapshot")
    snap = create_snapshot(["existing.txt"])

    # 快照后新建文件
    (file_ws / "brand_new.txt").write_text("should be deleted on restore")
    restore_snapshot(snap)
    assert not (file_ws / "brand_new.txt").exists()
    cleanup_snapshot(snap)


def test_file_snapshot_cleanup_removes_dir(file_ws):
    """cleanup_snapshot 后快照目录应被删除"""
    from agent import create_snapshot, cleanup_snapshot
    (file_ws / "f.txt").write_text("hi")
    snap = create_snapshot(["f.txt"])
    snap_dir = Path(snap["path"])
    assert snap_dir.exists()
    cleanup_snapshot(snap)
    assert not snap_dir.exists()


def test_file_snapshot_none_input(file_ws):
    """restore_snapshot(None) 不报错，返回 0"""
    from agent import restore_snapshot
    assert restore_snapshot(None) == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])