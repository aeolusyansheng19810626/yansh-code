"""solo mode 单测：主驱动循环 / no_progress 熔断 / 外部 test gate 回灌 / compact 接入 / 工具集。

solo() 是单一连续 context 端到端 agent，与逐文件 code() 并存。
这里用 mock call_llm + stub _dispatch_tool_calls 隔离循环控制流，不触发真实文件写/测试。
"""
import os
import sys
import json as _json
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import agent


def _make_response(content="", tool_calls=None):
    """构造像 LLM SDK 返回的 response。tool_calls: list of (call_id, name, args_dict)。"""
    msg = MagicMock()
    msg.content = content
    if tool_calls:
        tcs = []
        for cid, name, args in tool_calls:
            tc = MagicMock()
            tc.id = cid
            tc.function.name = name
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


def _stub_dispatch(tool_calls, **kw):
    """隔离 dispatch：不跑真实工具。task_complete → 返回 sentinel；其余 → 空 result。"""
    outs = []
    for tc in tool_calls:
        if tc.function.name == "task_complete":
            args = _json.loads(tc.function.arguments)
            outs.append({"result": {
                "_task_complete": True,
                "success": args.get("success"),
                "summary": args.get("summary", ""),
            }})
        else:
            outs.append({"result": {}})
    return outs


def _setup_ws(tmp_path):
    import config, tools
    config.set_workspace_dir(str(tmp_path))
    tools._reinit_paths()
    agent._reinit_paths()


def test_solo_basic_flow_write_then_complete(tmp_path, monkeypatch):
    """写一轮 → task_complete(success=true)；gate 无测试命令跳过 → solo 成功。"""
    _setup_ws(tmp_path)
    seq = [
        _make_response("规划：写 foo.py", [("c1", "write_file", {"filename": "foo.py", "content": "x=1"})]),
        _make_response("完成", [("c2", "task_complete", {"success": True, "summary": "已实现 foo"})]),
    ]
    calls = {"n": 0}

    def mock_llm(msgs, **kw):
        r = seq[calls["n"]]
        calls["n"] += 1
        return r

    monkeypatch.setattr(agent, "call_llm", mock_llm)
    monkeypatch.setattr(agent, "_dispatch_tool_calls", _stub_dispatch)
    monkeypatch.setattr(agent, "_detect_python_test_cmd", lambda *a, **k: None)

    res = agent.solo("实现 foo")
    assert res["success"] is True
    assert res["task_complete_signal"]["early_exit"] is True
    assert calls["n"] == 2


def test_solo_no_progress_circuit_break(tmp_path, monkeypatch):
    """持续只读不写 → 连续 2*CAP 轮无写编辑熔断，success=False。"""
    _setup_ws(tmp_path)
    monkeypatch.setattr(agent, "_dispatch_tool_calls", _stub_dispatch)
    monkeypatch.setattr(agent, "_detect_python_test_cmd", lambda *a, **k: None)

    calls = {"n": 0}

    def mock_llm(msgs, **kw):
        calls["n"] += 1
        return _make_response("继续看代码", [("c", "read_file", {"filename": "foo.py"})])

    monkeypatch.setattr(agent, "call_llm", mock_llm)

    res = agent.solo("某任务")
    assert res["success"] is False
    # 第 2*CAP 轮触发熔断
    assert calls["n"] == 2 * agent._SOLO_NO_PROGRESS_CAP
    assert "熔断" in res["task_complete_signal"]["summary"]


def test_solo_gate_reinjection_drives_fix(tmp_path, monkeypatch):
    """agent 先 task_complete，但外部 gate 测试首轮失败 → 回灌再驱动 → 二轮通过。"""
    _setup_ws(tmp_path)
    monkeypatch.setattr(agent, "_dispatch_tool_calls", _stub_dispatch)
    monkeypatch.setattr(agent, "_detect_python_test_cmd", lambda *a, **k: "pytest -q")

    # 两次 drive 各自 task_complete(success=true)
    llm_seq = [
        _make_response("done1", [("c1", "task_complete", {"success": True, "summary": "v1"})]),
        _make_response("fixed", [("c2", "task_complete", {"success": True, "summary": "v2"})]),
    ]
    calls = {"n": 0}

    def mock_llm(msgs, **kw):
        r = llm_seq[min(calls["n"], len(llm_seq) - 1)]
        calls["n"] += 1
        return r

    # gate：第一次 test 失败，第二次通过
    test_results = [
        {"returncode": 1, "stderr": "FAILED test_x - boom", "stdout": ""},
        {"returncode": 0, "stderr": "", "stdout": "1 passed"},
    ]
    test_calls = {"n": 0}

    def mock_test(cmd):
        r = test_results[min(test_calls["n"], len(test_results) - 1)]
        test_calls["n"] += 1
        return r

    monkeypatch.setattr(agent, "call_llm", mock_llm)
    monkeypatch.setattr(agent, "test", mock_test)

    res = agent.solo("实现并测试")
    assert res["success"] is True
    assert test_calls["n"] == 2   # 跑了两次测试（失败→回灌→通过）
    assert calls["n"] == 2        # 两次 drive 各 task_complete 一次


def test_solo_compact_invoked(tmp_path, monkeypatch):
    """主 loop 每轮调 _maybe_compact_messages（auto-compact 接入）。"""
    _setup_ws(tmp_path)
    monkeypatch.setattr(agent, "_dispatch_tool_calls", _stub_dispatch)
    monkeypatch.setattr(agent, "_detect_python_test_cmd", lambda *a, **k: None)

    compact_calls = {"n": 0}
    orig = agent._maybe_compact_messages

    def counting_compact(msgs, state, label="auto-compact"):
        compact_calls["n"] += 1
        return msgs

    monkeypatch.setattr(agent, "_maybe_compact_messages", counting_compact)
    monkeypatch.setattr(agent, "call_llm", lambda msgs, **kw: _make_response(
        "done", [("c", "task_complete", {"success": True, "summary": "ok"})]))

    agent.solo("任务")
    assert compact_calls["n"] >= 1


def test_solo_tools_writable_and_no_planmode():
    """solo 工具集含写工具/执行/子 agent，排除 plan-mode 专用工具。"""
    names = {t["function"]["name"] for t in agent._solo_tools()}
    assert "write_file" in names
    assert "execute_command" in names
    assert "dispatch_subagent" in names
    assert "update_plan_draft" not in names
    assert "exit_plan_mode_signal" not in names
