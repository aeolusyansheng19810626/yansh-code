"""Unit tests for #10: --cwd 支持（set_workspace_dir + _reinit_paths）"""
import os
import sys
import pytest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))


@pytest.fixture(autouse=True)
def restore_workspace(tmp_path):
    """每个测试后恢复原始 WORKSPACE_DIR"""
    import config
    original = config.WORKSPACE_DIR
    yield tmp_path
    # 恢复
    config.set_workspace_dir(original)
    import tools as _t
    import agent as _a
    _t._reinit_paths()
    _a._reinit_paths()


def test_set_workspace_dir_updates_config(tmp_path):
    """set_workspace_dir 后 config.WORKSPACE_DIR 应变为新路径"""
    import config
    new_ws = str(tmp_path / "myproject")
    config.set_workspace_dir(new_ws)
    assert config.WORKSPACE_DIR == new_ws


def test_tools_reinit_updates_workspace_root(tmp_path):
    """tools._reinit_paths() 后 _get_workspace() 返回新路径"""
    import config, tools
    new_ws = str(tmp_path / "proj")
    config.set_workspace_dir(new_ws)
    tools._reinit_paths()
    assert tools._get_workspace() == new_ws


def test_tools_workspace_root_updated(tmp_path):
    """tools._WORKSPACE_ROOT 在 _reinit_paths() 后指向新目录"""
    import config, tools
    new_ws = str(tmp_path / "newws")
    config.set_workspace_dir(new_ws)
    tools._reinit_paths()
    assert tools._WORKSPACE_ROOT == Path(new_ws).resolve()


def test_agent_reinit_updates_yansh_dir(tmp_path):
    """agent._reinit_paths() 后 _YANSH_DIR 等路径变量更新"""
    import config, agent, snapshot
    new_ws = str(tmp_path / "ws2")
    config.set_workspace_dir(new_ws)
    agent._reinit_paths()
    assert agent._YANSH_DIR == Path(new_ws) / ".yansh"
    assert snapshot._SNAPSHOT_DIR == Path(new_ws) / ".yansh" / "snapshots"
    assert agent._LOG_DIR == Path(new_ws) / ".yansh" / "logs"
    assert agent._REPLAY_DIR == Path(new_ws) / ".yansh" / "replay"
    assert agent._HISTORY_FILE == Path(new_ws) / ".yansh_history.json"


def test_write_file_uses_new_workspace(tmp_path):
    """set_workspace_dir + _reinit_paths 后 write_file 写到新目录"""
    import config, tools
    new_ws = str(tmp_path / "newdir")
    os.makedirs(new_ws, exist_ok=True)
    config.set_workspace_dir(new_ws)
    tools._reinit_paths()

    result = tools.write_file("hello.txt", "cwd test")
    assert "success" in result
    assert (Path(new_ws) / "hello.txt").exists()
    assert (Path(new_ws) / "hello.txt").read_text() == "cwd test"


def test_config_file_path_updated(tmp_path):
    """set_workspace_dir 后 config._CONFIG_FILE 指向新路径"""
    import config
    new_ws = str(tmp_path / "proj")
    config.set_workspace_dir(new_ws)
    assert config._CONFIG_FILE == Path(new_ws) / ".yansh" / "config.json"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])