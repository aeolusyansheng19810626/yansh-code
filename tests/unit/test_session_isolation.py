"""P4-3 Session 镜像 _ACTIVE_* / _SUBAGENT_STATS 单测。

之前 _ACTIVE_SKILLS_PROMPT / _ACTIVE_MEMORY_INDEX / _SUBAGENT_STATS 没进
state.Session 镜像——单测互相污染：test_A 加载的 memory 索引会被 test_B
看到，test_A 跑 subagent 的 stats 累计到 test_B。

P4-3 修复后：scoped_session 进入时拍快照（含 _ACTIVE_*），退出时恢复。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import agent
import state


def test_scoped_session_isolates_active_skills_prompt(tmp_path):
    """scoped_session 内修改 _ACTIVE_SKILLS_PROMPT，退出后应恢复"""
    agent._ACTIVE_SKILLS_PROMPT = "原始-skill-prompt"
    try:
        with state.scoped_session(tmp_path):
            # 进入 scoped 后是空字符串
            assert agent._ACTIVE_SKILLS_PROMPT == ""
            # 在 scope 内污染
            agent._ACTIVE_SKILLS_PROMPT = "污染数据"
        # 退出 scope 后恢复到原值
        assert agent._ACTIVE_SKILLS_PROMPT == "原始-skill-prompt"
    finally:
        agent._ACTIVE_SKILLS_PROMPT = ""


def test_scoped_session_isolates_active_memory_index(tmp_path):
    """同上，验证 _ACTIVE_MEMORY_INDEX"""
    agent._ACTIVE_MEMORY_INDEX = "原始-memory-index"
    try:
        with state.scoped_session(tmp_path):
            assert agent._ACTIVE_MEMORY_INDEX == ""
            agent._ACTIVE_MEMORY_INDEX = "test 期间的索引"
        assert agent._ACTIVE_MEMORY_INDEX == "原始-memory-index"
    finally:
        agent._ACTIVE_MEMORY_INDEX = ""


def test_scoped_session_isolates_subagent_stats(tmp_path):
    """_SUBAGENT_STATS 累计型——必须 scope 内归零、退出恢复"""
    agent._SUBAGENT_STATS["calls"] = 99
    agent._SUBAGENT_STATS["last_task"] = "outer-task"
    try:
        with state.scoped_session(tmp_path):
            # scope 内归零
            assert agent._SUBAGENT_STATS["calls"] == 0
            assert agent._SUBAGENT_STATS["last_task"] == ""
            # scope 内累计
            agent._SUBAGENT_STATS["calls"] = 5
        # 退出后恢复
        assert agent._SUBAGENT_STATS["calls"] == 99
        assert agent._SUBAGENT_STATS["last_task"] == "outer-task"
    finally:
        agent._SUBAGENT_STATS["calls"] = 0
        agent._SUBAGENT_STATS["last_task"] = ""


def test_session_pull_captures_active_state(tmp_path):
    """state.Session().pull() 应当把 _ACTIVE_* 当前值都抓进 dataclass"""
    agent._ACTIVE_SKILLS_PROMPT = "snapshot-skill"
    agent._ACTIVE_MEMORY_INDEX = "snapshot-mem"
    agent._SUBAGENT_STATS["calls"] = 7
    try:
        s = state.Session().pull()
        assert s.active_skills_prompt == "snapshot-skill"
        assert s.active_memory_index == "snapshot-mem"
        assert s.subagent_stats["calls"] == 7
    finally:
        agent._ACTIVE_SKILLS_PROMPT = ""
        agent._ACTIVE_MEMORY_INDEX = ""
        agent._SUBAGENT_STATS["calls"] = 0


def test_session_push_writes_active_state_back(tmp_path):
    """state.Session.push() 应当把 dataclass 字段写回模块"""
    s = state.Session(
        active_skills_prompt="pushed-skill",
        active_memory_index="pushed-mem",
        subagent_stats={"calls": 11, "last_task": "x"},
    )
    s.push()
    try:
        assert agent._ACTIVE_SKILLS_PROMPT == "pushed-skill"
        assert agent._ACTIVE_MEMORY_INDEX == "pushed-mem"
        assert agent._SUBAGENT_STATS["calls"] == 11
        assert agent._SUBAGENT_STATS["last_task"] == "x"
    finally:
        agent._ACTIVE_SKILLS_PROMPT = ""
        agent._ACTIVE_MEMORY_INDEX = ""
        agent._SUBAGENT_STATS["calls"] = 0
        agent._SUBAGENT_STATS["last_task"] = ""


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
