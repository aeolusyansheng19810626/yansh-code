"""P2 #9 子 Agent 单元测试。

覆盖：
  - dispatch_subagent sentinel 形态
  - READONLY 白名单包含 dispatch_subagent
  - role -> 工具集映射（explorer 只读 / general 全工具 / blocked={dispatch_subagent + plan sentinel}）
  - _run_subagent 正常流：返回 summary + 步数
  - 递归防护：_IN_SUBAGENT=True 时再调 dispatch_subagent → 失败
  - max_steps 上限 clamp
  - 沉默退出兜底
  - 父 agent 的 messages 不被污染（context 隔离）
  - _subagent_handler 输出形态
  - stats 累计
"""
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import agent
import tools
import state
from tools_schema import READONLY_TOOL_NAMES, TOOLS


# ---------- sentinel 形态 ----------

def test_dispatch_subagent_returns_sentinel():
    out = tools.dispatch_subagent("查 X 模块用法", role="explorer", max_steps=5)
    assert out["_subagent_dispatch"] is True
    assert out["task"] == "查 X 模块用法"
    assert out["role"] == "explorer"
    assert out["max_steps"] == 5


def test_dispatch_subagent_default_role_max_steps():
    out = tools.dispatch_subagent("活")
    assert out["role"] == "explorer"
    assert out["max_steps"] == 8


def test_dispatch_subagent_handles_falsy_max_steps():
    """max_steps=0 应被 fallback 到 8（避免 LLM 误传 0）"""
    out = tools.dispatch_subagent("X", max_steps=0)
    assert out["max_steps"] == 8


# ---------- READONLY 白名单 ----------

def test_readonly_whitelist_includes_dispatch_subagent():
    assert "dispatch_subagent" in READONLY_TOOL_NAMES


def test_dispatch_subagent_schema_present():
    names = {t["function"]["name"] for t in TOOLS}
    assert "dispatch_subagent" in names


def test_dispatch_subagent_schema_has_role_enum():
    schema = next(t for t in TOOLS if t["function"]["name"] == "dispatch_subagent")
    role_param = schema["function"]["parameters"]["properties"]["role"]
    assert set(role_param["enum"]) == {"explorer", "general", "auditor"}


# ---------- role -> 工具集 ----------

def test_subagent_explorer_excludes_write_tools():
    tools_subset = agent._subagent_tools_for_role("explorer")
    names = {t["function"]["name"] for t in tools_subset}
    # 写工具不在内
    assert "write_file" not in names
    assert "replace_in_file" not in names
    assert "execute_command" not in names
    assert "delete_file" not in names
    # 读工具在
    assert "read_file" in names
    assert "search_in_files" in names
    assert "task_complete" in names


def test_subagent_general_includes_write_tools():
    tools_subset = agent._subagent_tools_for_role("general")
    names = {t["function"]["name"] for t in tools_subset}
    assert "write_file" in names
    assert "execute_command" in names
    assert "read_file" in names


def test_subagent_blocks_dispatch_recursion_in_toolset():
    """所有 role 的工具集都不应包含 dispatch_subagent（物理隔离防递归）"""
    for role in ("explorer", "general", "auditor"):
        names = {t["function"]["name"] for t in agent._subagent_tools_for_role(role)}
        assert "dispatch_subagent" not in names, f"role={role} 工具集仍含 dispatch_subagent"


def test_subagent_blocks_plan_mode_tools():
    """子 agent 不应看到 plan mode 专用工具"""
    for role in ("explorer", "general", "auditor"):
        names = {t["function"]["name"] for t in agent._subagent_tools_for_role(role)}
        assert "update_plan_draft" not in names
        assert "exit_plan_mode_signal" not in names


def test_subagent_unknown_role_falls_back_to_explorer():
    tools_subset = agent._subagent_tools_for_role("nonsense")
    names = {t["function"]["name"] for t in tools_subset}
    assert "write_file" not in names
    assert "read_file" in names


# ---------- _run_subagent 正常流 ----------

def _mk_resp(content="", tool_calls=None):
    msg = MagicMock()
    msg.content = content
    if tool_calls:
        tcs = []
        for cid, name, args in tool_calls:
            tc = MagicMock()
            tc.id = cid
            tc.function.name = name
            import json as _json
            tc.function.arguments = _json.dumps(args)
            tc.model_dump = lambda _cid=cid, _n=name, _a=args: {
                "id": _cid, "type": "function",
                "function": {"name": _n, "arguments": _json.dumps(_a)},
            }
            tcs.append(tc)
        msg.tool_calls = tcs
    else:
        msg.tool_calls = None
    resp = MagicMock()
    resp.choices = [MagicMock(message=msg)]
    return resp


def test_run_subagent_returns_summary_on_task_complete(tmp_path):
    """子 agent 调 task_complete 后返回 summary"""
    with state.scoped_session(tmp_path):
        (tmp_path / "main.py").write_text("def f(): pass\n", encoding="utf-8")

        orig = agent.call_llm
        try:
            agent.call_llm = lambda msgs, **kw: _mk_resp(
                content="找到了",
                tool_calls=[
                    ("c1", "task_complete",
                     {"success": True, "summary": "main.py:1 是入口函数 f"}),
                ],
            )
            res = agent._run_subagent("找入口函数", role="explorer", max_steps=4)
        finally:
            agent.call_llm = orig

        assert res["success"] is True
        assert res["summary"] == "main.py:1 是入口函数 f"
        assert res["steps"] == 1
        assert res["role"] == "explorer"


def test_run_subagent_max_steps_clamped_to_hard_cap(tmp_path):
    """max_steps=999 应被 clamp 到 _SUBAGENT_HARD_CAP（16）"""
    with state.scoped_session(tmp_path):
        (tmp_path / "f.py").write_text("x=1\n", encoding="utf-8")

        call_count = {"n": 0}

        def fake_llm(msgs, **kw):
            call_count["n"] += 1
            return _mk_resp(content=f"step {call_count['n']}")  # 永不调工具，沉默退出

        orig = agent.call_llm
        try:
            agent.call_llm = fake_llm
            res = agent._run_subagent("永远跑不完的任务", max_steps=999)
        finally:
            agent.call_llm = orig

        # 沉默退出会有兜底追问 1 次再 break，所以 ≤ 2 次调用就退；不会跑到 16
        assert res["steps"] <= 16
        assert call_count["n"] <= 16


def test_run_subagent_silent_exit_uses_content_as_summary(tmp_path):
    """LLM 一直不调工具，content 当 summary 兜底"""
    with state.scoped_session(tmp_path):
        (tmp_path / "f.py").write_text("x=1\n", encoding="utf-8")

        call_count = {"n": 0}

        def fake_llm(msgs, **kw):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return _mk_resp(content="")   # 第一轮沉默——触发兜底追问
            return _mk_resp(content="我看完了，没什么要报告的")  # 第二轮有 content 但没工具

        orig = agent.call_llm
        try:
            agent.call_llm = fake_llm
            res = agent._run_subagent("任务", role="explorer", max_steps=8)
        finally:
            agent.call_llm = orig

        assert "没什么要报告的" in res["summary"]


def test_run_subagent_max_steps_min_clamped_to_1(tmp_path):
    """max_steps=0 / 负数应被 clamp 到 1"""
    with state.scoped_session(tmp_path):
        (tmp_path / "f.py").write_text("x=1\n", encoding="utf-8")

        orig = agent.call_llm
        try:
            agent.call_llm = lambda msgs, **kw: _mk_resp(content="hi")
            res = agent._run_subagent("X", max_steps=-5)
        finally:
            agent.call_llm = orig

        assert res["steps"] >= 1


# ---------- 递归防护 ----------

def test_run_subagent_recursion_blocked(tmp_path):
    """_IN_SUBAGENT=True 时再调 _run_subagent → 立即返回失败"""
    with state.scoped_session(tmp_path):
        agent._IN_SUBAGENT = True
        try:
            res = agent._run_subagent("再派一个", role="explorer")
        finally:
            agent._IN_SUBAGENT = False
        assert res["success"] is False
        assert "递归" in res["summary"]
        assert res["steps"] == 0


# ---------- handler 形态 ----------

def test_subagent_handler_returns_dict_for_llm(tmp_path):
    with state.scoped_session(tmp_path):
        (tmp_path / "f.py").write_text("x=1\n", encoding="utf-8")
        orig = agent.call_llm
        try:
            agent.call_llm = lambda msgs, **kw: _mk_resp(
                tool_calls=[("c1", "task_complete",
                             {"success": True, "summary": "OK"})],
            )
            out = agent._subagent_handler(task="X", role="explorer", max_steps=4)
        finally:
            agent.call_llm = orig

        assert out["success"] is True
        assert out["summary"] == "OK"
        assert out["steps"] >= 1
        assert out["role"] == "explorer"


def test_subagent_handler_missing_task_returns_error(tmp_path):
    with state.scoped_session(tmp_path):
        out = agent._subagent_handler(task="", role="explorer")
        assert "error" in out


# ---------- stats 累计 ----------

def test_subagent_stats_updated(tmp_path):
    with state.scoped_session(tmp_path):
        # 重置 stats
        agent._SUBAGENT_STATS["calls"] = 0
        agent._SUBAGENT_STATS["total_steps"] = 0
        (tmp_path / "f.py").write_text("x=1\n", encoding="utf-8")

        orig = agent.call_llm
        try:
            agent.call_llm = lambda msgs, **kw: _mk_resp(
                tool_calls=[("c1", "task_complete",
                             {"success": True, "summary": "done"})],
            )
            agent._run_subagent("a", role="explorer", max_steps=4)
            agent._run_subagent("b", role="explorer", max_steps=4)
        finally:
            agent.call_llm = orig

        stats = agent.get_subagent_stats()
        assert stats["calls"] == 2
        assert stats["total_steps"] >= 2
        assert stats["last_task"] == "b"
        assert stats["last_summary"] == "done"
        assert stats["last_success"] is True
        assert stats["last_role"] == "explorer"


# ---------- context 隔离：父 agent 不被污染 ----------

def test_subagent_messages_isolated_from_parent(tmp_path):
    """子 agent 跑完，父 agent 的 conversation_history 不应被写入子 agent 的中间消息"""
    with state.scoped_session(tmp_path):
        (tmp_path / "f.py").write_text("x=1\n", encoding="utf-8")
        before = list(agent.conversation_history)

        orig = agent.call_llm
        try:
            agent.call_llm = lambda msgs, **kw: _mk_resp(
                content="子 agent 看到的中间步骤",
                tool_calls=[("c1", "task_complete",
                             {"success": True, "summary": "短 summary"})],
            )
            agent._run_subagent("X", role="explorer", max_steps=4)
        finally:
            agent.call_llm = orig

        after = list(agent.conversation_history)
        assert before == after, "子 agent 不应改父 agent 的 conversation_history"


# ---------- _IN_SUBAGENT 进出对称 ----------

def test_in_subagent_flag_resets_after_run(tmp_path):
    with state.scoped_session(tmp_path):
        (tmp_path / "f.py").write_text("x=1\n", encoding="utf-8")
        assert agent._IN_SUBAGENT is False
        orig = agent.call_llm
        try:
            agent.call_llm = lambda msgs, **kw: _mk_resp(
                tool_calls=[("c1", "task_complete",
                             {"success": True, "summary": "OK"})],
            )
            agent._run_subagent("X", role="explorer", max_steps=4)
        finally:
            agent.call_llm = orig
        assert agent._IN_SUBAGENT is False, "跑完应当重置"


def test_in_subagent_flag_resets_on_exception(tmp_path):
    """LLM 抛错时 _IN_SUBAGENT 也要 reset（finally 块）"""
    with state.scoped_session(tmp_path):
        (tmp_path / "f.py").write_text("x=1\n", encoding="utf-8")

        def boom(*a, **kw):
            raise RuntimeError("LLM 挂了")

        orig = agent.call_llm
        try:
            agent.call_llm = boom
            try:
                agent._run_subagent("X", role="explorer", max_steps=4)
            except RuntimeError:
                pass
        finally:
            agent.call_llm = orig
        assert agent._IN_SUBAGENT is False


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
