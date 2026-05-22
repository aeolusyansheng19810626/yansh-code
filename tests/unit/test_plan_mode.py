"""P2 #7 Plan Mode 方案 C 单元测试。

覆盖：
  - 状态机：enter / cancel / approve
  - 工具：update_plan_draft / exit_plan_mode_signal sentinel 形态
  - 工具白名单：READONLY_TOOL_NAMES 包含两个新工具，不含写工具
  - plan_chat 循环：探索 → update_plan_draft → exit_plan_mode_signal → 返回
  - sentinel 路径：草稿被写进 _PLAN_DRAFT；exit signal 终止本轮
"""
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import agent
import tools
import state
from tools_schema import READONLY_TOOL_NAMES, TOOLS


# ---------- 工具 sentinel 形态 ----------

def test_update_plan_draft_returns_sentinel():
    out = tools.update_plan_draft("## 目标\n做 X")
    assert out["_plan_draft_update"] is True
    assert out["content"] == "## 目标\n做 X"


def test_update_plan_draft_handles_none():
    out = tools.update_plan_draft(None)
    assert out["_plan_draft_update"] is True
    assert out["content"] == ""


def test_exit_plan_mode_signal_returns_sentinel():
    out = tools.exit_plan_mode_signal("草稿到位")
    assert out["_exit_plan_mode_signal"] is True
    assert out["reason"] == "草稿到位"


def test_exit_plan_mode_signal_default_reason():
    out = tools.exit_plan_mode_signal()
    assert out["_exit_plan_mode_signal"] is True
    assert out["reason"] == ""


# ---------- READONLY 白名单：新工具进，写工具不进 ----------

def test_readonly_whitelist_includes_plan_tools():
    assert "update_plan_draft" in READONLY_TOOL_NAMES
    assert "exit_plan_mode_signal" in READONLY_TOOL_NAMES


def test_readonly_whitelist_excludes_write_tools():
    """plan_mode 下 LLM 看到的工具应该不含写/执行类"""
    write_tools = {"write_file", "replace_in_file", "replace_symbol",
                   "apply_patch", "delete_file", "move_file", "append_to_file",
                   "execute_command"}
    assert write_tools.isdisjoint(READONLY_TOOL_NAMES)


def test_filter_tools_for_plan_mode():
    """_filter_tools 用 plan_mode 白名单过滤后，不应漏写工具"""
    plan_tools = agent._filter_tools(READONLY_TOOL_NAMES)
    names = {t["function"]["name"] for t in plan_tools}
    assert "update_plan_draft" in names
    assert "exit_plan_mode_signal" in names
    assert "write_file" not in names
    assert "replace_in_file" not in names
    assert "execute_command" not in names


# ---------- 状态机 ----------

def test_enter_plan_mode_sets_flag(tmp_path):
    with state.scoped_session(tmp_path):
        assert agent.is_plan_mode() is False
        agent.enter_plan_mode()
        assert agent.is_plan_mode() is True
        assert agent._PLAN_HISTORY == []
        assert agent._PLAN_DRAFT == ""


def test_cancel_plan_mode_clears_all(tmp_path):
    with state.scoped_session(tmp_path):
        agent.enter_plan_mode()
        agent._PLAN_DRAFT = "## 目标\n要做的事"
        agent._PLAN_HISTORY = [{"role": "user", "content": "x"}]
        agent.cancel_plan_mode()
        assert agent.is_plan_mode() is False
        assert agent._PLAN_DRAFT == ""
        assert agent._PLAN_HISTORY == []


def test_approve_plan_returns_enriched_requirement(tmp_path):
    """/approve 应把 plan 草稿拼进 requirement，并清理 plan 状态"""
    with state.scoped_session(tmp_path):
        agent.enter_plan_mode()
        agent._PLAN_HISTORY = [
            {"role": "user", "content": "重构 fix() 错误恢复"},
            {"role": "assistant", "content": "..."},
        ]
        agent._PLAN_DRAFT = "## 目标\n重构 fix\n## 步骤\n1. xxx"
        enriched = agent.approve_plan()
        assert "重构 fix() 错误恢复" in enriched
        assert "已批准的实施方案" in enriched
        assert "## 步骤" in enriched
        # plan 状态被清空
        assert agent.is_plan_mode() is False
        assert agent._PLAN_DRAFT == ""
        assert agent._PLAN_HISTORY == []


def test_approve_plan_empty_draft_returns_empty(tmp_path):
    with state.scoped_session(tmp_path):
        agent.enter_plan_mode()
        # 没有草稿
        result = agent.approve_plan()
        assert result == ""


# ---------- state.Session 镜像新字段 ----------

def test_session_pulls_plan_state(tmp_path):
    with state.scoped_session(tmp_path):
        agent.enter_plan_mode()
        agent._PLAN_DRAFT = "草稿内容"
        agent._PLAN_HISTORY = [{"role": "user", "content": "x"}]
        sess = state.Session().pull()
        assert sess.plan_mode is True
        assert sess.plan_draft == "草稿内容"
        assert len(sess.plan_history) == 1


def test_session_reset_clears_plan_state(tmp_path):
    agent._PLAN_MODE = True
    agent._PLAN_DRAFT = "应被清"
    agent._PLAN_HISTORY = [{"role": "user", "content": "y"}]
    state.Session().reset(workspace_dir=str(tmp_path))
    assert agent._PLAN_MODE is False
    assert agent._PLAN_DRAFT == ""
    assert agent._PLAN_HISTORY == []


def test_scoped_session_restores_plan_state(tmp_path):
    agent._PLAN_MODE = True
    agent._PLAN_DRAFT = "OUTER"
    with state.scoped_session(tmp_path):
        assert agent._PLAN_MODE is False
        assert agent._PLAN_DRAFT == ""
        agent._PLAN_MODE = True
        agent._PLAN_DRAFT = "INNER"
    assert agent._PLAN_MODE is True
    assert agent._PLAN_DRAFT == "OUTER"


# ---------- plan_chat 多轮循环 ----------

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


def test_plan_chat_captures_draft_and_exits_on_signal(tmp_path):
    """LLM 调 update_plan_draft 后调 exit_plan_mode_signal → plan_chat 返回，草稿入 _PLAN_DRAFT"""
    with state.scoped_session(tmp_path):
        agent.enter_plan_mode()
        # 给 workspace 放一个文件让 workspace_symbols 不报错
        (tmp_path / "calc.py").write_text("def add(a, b): return a + b\n", encoding="utf-8")

        orig = agent.call_llm
        try:
            agent.call_llm = lambda msgs, **kw: _mk_resp(
                content="读完了 calc.py，方案如下：",
                tool_calls=[
                    ("p1", "update_plan_draft",
                     {"content": "## 目标\n加 multiply\n## 步骤\n1. 改 calc.py"}),
                    ("p2", "exit_plan_mode_signal", {"reason": "请审阅"}),
                ],
            )
            text = agent.plan_chat("加个 multiply 函数")
        finally:
            agent.call_llm = orig

        assert "方案如下" in text
        assert "## 目标" in agent._PLAN_DRAFT
        assert "multiply" in agent._PLAN_DRAFT
        # 历史里应记录用户输入 + assistant 文本
        roles = [m["role"] for m in agent._PLAN_HISTORY]
        assert roles == ["user", "assistant"]


def test_plan_chat_silent_then_signal(tmp_path):
    """LLM 第一轮沉默 → 兜底追问 → 第二轮调 exit signal"""
    with state.scoped_session(tmp_path):
        agent.enter_plan_mode()
        (tmp_path / "x.py").write_text("def f(): pass\n", encoding="utf-8")

        call_count = {"n": 0}
        def mock_llm(msgs, **kw):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return _mk_resp(content="嗯，好的。", tool_calls=None)
            return _mk_resp(
                content="本轮结束",
                tool_calls=[("p1", "exit_plan_mode_signal", {})],
            )

        orig = agent.call_llm
        try:
            agent.call_llm = mock_llm
            text = agent.plan_chat("看一下")
        finally:
            agent.call_llm = orig

        assert call_count["n"] == 2
        assert "本轮结束" in text


def test_plan_chat_draft_visible_in_next_round_system_prompt(tmp_path):
    """第二轮调 plan_chat 时，system_prompt 应含上一次的草稿"""
    with state.scoped_session(tmp_path):
        agent.enter_plan_mode()
        (tmp_path / "x.py").write_text("def f(): pass\n", encoding="utf-8")

        # 第一轮：写草稿
        orig = agent.call_llm
        try:
            agent.call_llm = lambda msgs, **kw: _mk_resp(
                content="OK",
                tool_calls=[
                    ("p1", "update_plan_draft", {"content": "## v1 草稿"}),
                    ("p2", "exit_plan_mode_signal", {}),
                ],
            )
            agent.plan_chat("方案 v1")
        finally:
            agent.call_llm = orig

        assert "v1 草稿" in agent._PLAN_DRAFT

        # 第二轮：拦截 messages 看 system 是否含草稿
        seen = {"sys": ""}
        def capture_llm(msgs, **kw):
            seen["sys"] = msgs[0]["content"]
            return _mk_resp(
                content="读到了",
                tool_calls=[("p3", "exit_plan_mode_signal", {})],
            )
        try:
            agent.call_llm = capture_llm
            agent.plan_chat("继续看一下")
        finally:
            agent.call_llm = orig

        assert "v1 草稿" in seen["sys"]
        assert "当前 plan 草稿" in seen["sys"]


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
