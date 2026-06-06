"""snapshot.py 回滚缺陷修复单测：验证只删 meta["created"] 里的文件，不误删外部文件。"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import pytest
from pathlib import Path


@pytest.fixture
def ws_env(tmp_path):
    """建临时 workspace，设置 config + reinit snapshot 路径。"""
    import config
    import snapshot

    ws = tmp_path / "workspace"
    ws.mkdir()
    config.set_workspace_dir(str(ws))
    snapshot._reinit_paths()
    yield ws
    # 还原（其他测试不受影响）
    config.set_workspace_dir("workspace")
    snapshot._reinit_paths()


def test_restore_only_deletes_created_not_external(ws_env):
    """
    场景：
    - a.py 是 baseline 已存在文件
    - new.py 是任务新建（通过 _backup_file_if_needed 记入 created）
    - external.py 是外部进程新建（未经 backup），回滚后必须保留
    """
    import snapshot

    ws = ws_env

    # 建 a.py
    a_py = ws / "a.py"
    a_py.write_text("original", encoding="utf-8")

    # 建 snapshot（baseline 含 a.py）
    snap_info = snapshot.create_snapshot(["a.py"])

    # 改 a.py 内容
    a_py.write_text("modified", encoding="utf-8")

    # 模拟将新建 new.py：_backup_file_if_needed 此时 src 不存在，记入 created
    snapshot._backup_file_if_needed(snap_info, "new.py")

    # 验证 meta["created"] 已写入
    import json
    snap_dir = Path(snap_info["path"])
    meta = json.loads((snap_dir / "meta.json").read_text(encoding="utf-8"))
    assert "new.py" in meta.get("created", []), "new.py 应被记入 meta['created']"

    # 真的写出 new.py
    (ws / "new.py").write_text("new file content", encoding="utf-8")

    # external.py 是外部进程写的，未经 _backup_file_if_needed
    (ws / "external.py").write_text("external content", encoding="utf-8")

    # 回滚
    restored = snapshot.restore_snapshot(snap_info)

    # 断言
    assert restored >= 1, "应该还原了至少 1 个文件"
    assert a_py.read_text(encoding="utf-8") == "original", "a.py 应还原到 original"
    assert not (ws / "new.py").exists(), "new.py 是任务新建，回滚后应删除"
    assert (ws / "external.py").exists(), "external.py 是外部文件，回滚后必须保留"


def test_backup_existing_file_recorded_in_files(ws_env):
    """_backup_file_if_needed 对已存在文件，记入 meta['files']，不记入 meta['created']。"""
    import snapshot
    import json

    ws = ws_env
    b_py = ws / "b.py"
    b_py.write_text("b content", encoding="utf-8")

    snap_info = snapshot.create_snapshot([])  # 空 baseline

    snapshot._backup_file_if_needed(snap_info, "b.py")

    snap_dir = Path(snap_info["path"])
    meta = json.loads((snap_dir / "meta.json").read_text(encoding="utf-8"))
    assert "b.py" in meta.get("files", [])
    assert "b.py" not in meta.get("created", [])


def test_created_field_dedup(ws_env):
    """重复调用 _backup_file_if_needed 对同一新建文件，meta['created'] 不重复记。"""
    import snapshot
    import json

    ws = ws_env
    snap_info = snapshot.create_snapshot([])

    snapshot._backup_file_if_needed(snap_info, "dup.py")
    snapshot._backup_file_if_needed(snap_info, "dup.py")

    snap_dir = Path(snap_info["path"])
    meta = json.loads((snap_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta.get("created", []).count("dup.py") == 1, "dup.py 不能重复出现"
