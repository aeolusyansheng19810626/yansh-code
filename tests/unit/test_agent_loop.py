"""P0 #3 第二波：fix() / code() task_complete 信号传递的单测。

验证 fix() 和 code() 的返回值能正确把 LLM 的 task_complete 主动声明
传给外层 run()，不让"任务完成/放弃"信号被吞掉。
"""
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import agent


def _make_response(content="", tool_calls=None):
    """构造一个像 LLM SDK 返回的 response 对象。
    tool_calls: list of (call_id, name, args_dict)
    """
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
            # model_dump 是 Pydantic 风格 SDK 才有；agent.fix() 会调它
            tc.model_dump = lambda _cid=cid, _n=name, _a=args: {
                "id": _cid,
                "type": "function",
                "function": {"name": _n, "arguments": _json.dumps(_a)},
            }
            tcs.append(tc)
        msg.tool_calls = tcs
    else:
        msg.tool_calls = None
    resp = MagicMock()
    resp.choices = [MagicMock(message=msg)]
    return resp


def test_fix_returns_early_exit_on_task_complete_success(tmp_path):
    """LLM 第一轮调 task_complete(success=true) → fix() 返回 early_exit=True success=True"""
    import config, tools
    config.set_workspace_dir(str(tmp_path))
    tools._reinit_paths()

    orig = agent.call_llm
    try:
        agent.call_llm = lambda msgs, **kw: _make_response(
            tool_calls=[("c1", "task_complete",
                         {"success": True, "summary": "已修复 list_files；其余是 pre-existing"})]
        )
        result = agent.fix(
            {"returncode": 1, "stderr": "FAILED test_x", "stdout": ""},
            {"files": [], "test_command": ""},
        )
    finally:
        agent.call_llm = orig

    assert result == {
        "early_exit": True,
        "success": True,
        "summary": "已修复 list_files；其余是 pre-existing",
    }


def test_fix_returns_early_exit_on_task_complete_failure(tmp_path):
    """LLM 调 task_complete(success=false) → fix() 返回 early_exit=True success=False"""
    import config, tools
    config.set_workspace_dir(str(tmp_path))
    tools._reinit_paths()

    orig = agent.call_llm
    try:
        agent.call_llm = lambda msgs, **kw: _make_response(
            tool_calls=[("c2", "task_complete",
                         {"success": False, "summary": "测试自相矛盾，无解"})]
        )
        result = agent.fix(
            {"returncode": 1, "stderr": "...", "stdout": ""},
            {"files": [], "test_command": ""},
        )
    finally:
        agent.call_llm = orig

    assert result["early_exit"] is True
    assert result["success"] is False
    assert "矛盾" in result["summary"]


def test_fix_returns_no_early_exit_on_silent_then_silent(tmp_path):
    """LLM 两轮都不调工具（沉默 + 兜底再沉默）→ fix() 返回 early_exit=False"""
    import config, tools
    config.set_workspace_dir(str(tmp_path))
    tools._reinit_paths()

    call_count = {"n": 0}

    def mock_llm(msgs, **kw):
        call_count["n"] += 1
        return _make_response(content="（沉默，不调工具）", tool_calls=None)

    orig = agent.call_llm
    try:
        agent.call_llm = mock_llm
        result = agent.fix(
            {"returncode": 1, "stderr": "...", "stdout": ""},
            {"files": [], "test_command": ""},
        )
    finally:
        agent.call_llm = orig

    # 第一次沉默触发兜底追问，第二次仍沉默才真退出
    assert call_count["n"] == 2
    assert result == {"early_exit": False, "success": False, "summary": ""}


def test_code_returns_signal_on_coder_giveup(tmp_path):
    """Coder 调 task_complete(success=false) → code() 立即 return early_exit signal"""
    import config, tools
    config.set_workspace_dir(str(tmp_path))
    tools._reinit_paths()

    orig = agent.call_llm
    try:
        agent.call_llm = lambda msgs, **kw: _make_response(
            tool_calls=[("c3", "task_complete",
                         {"success": False, "summary": "需求自相矛盾，无法实现"})]
        )
        # plan 里没文件——code() 不会进 inner loop，所以构造一个最小 plan
        # 但是 code() 第一轮强制 write_file 会优先于 task_complete，
        # 所以让 plan 里给一个"已存在"文件（先创建出来），inner loop tc="auto"
        (tmp_path / "calc.py").write_text("def add(a,b): return a+b\n", encoding="utf-8")
        result = agent.code(
            {"files": [{"filename": "calc.py", "intent": "矛盾任务"}], "test_command": ""},
            mode="code",
            requirement="给我做个不可能的事",
        )
    finally:
        agent.call_llm = orig

    assert result is not None
    assert result["early_exit"] is True
    assert result["success"] is False
    assert "矛盾" in result["summary"]


def test_code_returns_signal_on_coder_success_done(tmp_path):
    """Coder 调 task_complete(success=true) → code() 完成时返回 early_exit signal"""
    import config, tools
    config.set_workspace_dir(str(tmp_path))
    tools._reinit_paths()

    (tmp_path / "calc.py").write_text("def add(a,b): return a+b\n", encoding="utf-8")

    orig = agent.call_llm
    try:
        agent.call_llm = lambda msgs, **kw: _make_response(
            tool_calls=[("c4", "task_complete",
                         {"success": True, "summary": "已完成 multiply"})]
        )
        result = agent.code(
            {"files": [{"filename": "calc.py", "intent": "加 multiply"}], "test_command": ""},
            mode="code",
            requirement="加 multiply",
        )
    finally:
        agent.call_llm = orig

    assert result is not None
    assert result["early_exit"] is True
    assert result["success"] is True


def test_code_returns_none_when_no_task_complete(tmp_path):
    """Coder 不调 task_complete（沉默退出 inner loop）→ code() 返回 None"""
    import config, tools
    config.set_workspace_dir(str(tmp_path))
    tools._reinit_paths()

    (tmp_path / "calc.py").write_text("def add(a,b): return a+b\n", encoding="utf-8")

    orig = agent.call_llm
    try:
        # 第一轮调 read_file 一下，第二轮不调任何工具
        call_count = {"n": 0}
        def mock_llm(msgs, **kw):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return _make_response(
                    tool_calls=[("c5", "read_file", {"filename": "calc.py"})]
                )
            return _make_response(content="看完了，没改动", tool_calls=None)

        agent.call_llm = mock_llm
        result = agent.code(
            {"files": [{"filename": "calc.py", "intent": "看一下"}], "test_command": ""},
            mode="code",
            requirement="审视",
        )
    finally:
        agent.call_llm = orig

    assert result is None


def test_report_includes_task_complete_signal_when_provided():
    sig = {"early_exit": True, "success": True, "summary": "完成"}
    out = agent.report(True, {"returncode": 0}, task_complete_signal=sig)
    assert out["task_complete_signal"] == sig
    assert out["success"] is True


def test_report_omits_task_complete_signal_when_none():
    out = agent.report(False, {"returncode": 1})
    assert "task_complete_signal" not in out
    assert out["success"] is False


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
