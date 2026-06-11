"""实验1：solo_test_enforcement 三态（off/role/gate）—— 逼 agent 留下正规测试。"""
import os
import sys
import json as _json
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import agent
import config
import tools


def _make_response(content="", tool_calls=None):
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
                "function": {"name": _n, "arguments": _json.dumps(_a)}}
            tcs.append(tc)
        msg.tool_calls = tcs
    else:
        msg.tool_calls = None
    resp = MagicMock()
    resp.choices = [MagicMock(message=msg)]
    return resp


def _stub_dispatch(tool_calls, **kw):
    outs = []
    for tc in tool_calls:
        if tc.function.name == "task_complete":
            args = _json.loads(tc.function.arguments)
            outs.append({"result": {"_task_complete": True, "success": args.get("success"),
                                     "summary": args.get("summary", "")}})
        else:
            outs.append({"result": {}})
    return outs


def _setup_ws(tmp_path):
    config.set_workspace_dir(str(tmp_path))
    tools._reinit_paths()
    agent._reinit_paths()


# ── _has_impl_files helper ──

def test_has_impl_files():
    assert agent._has_impl_files(["miniql/executor.py"]) is True
    assert agent._has_impl_files(["pkg/__main__.py", "tests/test_a.py"]) is True
    assert agent._has_impl_files(["tests/test_x.py"]) is False
    assert agent._has_impl_files(["_test_foo.py"]) is False
    assert agent._has_impl_files(["data/emp.csv", "PROMPT.md"]) is False
    assert agent._has_impl_files(["tests/conftest.py"]) is False
    assert agent._has_impl_files([]) is False


# ── 解法A：role 注入 ──

def _run_solo_capture_system(tmp_path, monkeypatch, enforcement):
    _setup_ws(tmp_path)
    monkeypatch.setitem(config._effective_config, "solo_test_enforcement", enforcement)
    monkeypatch.setattr(agent, "_dispatch_tool_calls", _stub_dispatch)
    monkeypatch.setattr(agent, "_infer_test_scope", lambda *a, **k: [])
    monkeypatch.setattr(agent, "_detect_python_test_cmd", lambda ws, scope=None: "pytest")
    # gate 测试直接绿（rc=0 且非 collected-0）快速收尾
    monkeypatch.setattr(agent, "test", lambda cmd, timeout_sec=None: {"returncode": 0, "stdout": "1 passed", "stderr": ""})
    captured = {}

    def mock_llm(msgs, **kw):
        if "system" not in captured:
            captured["system"] = msgs[0].get("content", "")
        return _make_response("done", [("c1", "task_complete", {"success": True, "summary": "ok"})])

    monkeypatch.setattr(agent, "call_llm", mock_llm)
    agent.solo("任务")
    return captured.get("system", "")


def test_role_enforcement_injects_test_first(tmp_path, monkeypatch):
    sys_prompt = _run_solo_capture_system(tmp_path, monkeypatch, "role")
    assert "测试优先" in sys_prompt


def test_off_enforcement_no_test_first(tmp_path, monkeypatch):
    sys_prompt = _run_solo_capture_system(tmp_path, monkeypatch, "off")
    assert "测试优先" not in sys_prompt


# ── 解法B：gate 硬判定 ──

def _run_gate_b(tmp_path, monkeypatch, enforcement, modified, test_seq):
    _setup_ws(tmp_path)
    monkeypatch.setitem(config._effective_config, "solo_test_enforcement", enforcement)
    monkeypatch.setattr(agent, "_dispatch_tool_calls", _stub_dispatch)
    monkeypatch.setattr(agent._task_log_mod, "snapshot_files_modified", lambda: modified)
    monkeypatch.setattr(agent, "_infer_test_scope", lambda *a, **k: [])
    monkeypatch.setattr(agent, "_force_include_smoke", lambda sc, ws: sc)
    monkeypatch.setattr(agent, "_detect_python_test_cmd", lambda ws, scope=None: "pytest")

    tcalls = {"n": 0}

    def mock_test(cmd, timeout_sec=None):
        r = test_seq[min(tcalls["n"], len(test_seq) - 1)]
        tcalls["n"] += 1
        return r

    monkeypatch.setattr(agent, "test", mock_test)
    monkeypatch.setattr(agent, "call_llm", lambda msgs, **kw: _make_response(
        "done", [("c1", "task_complete", {"success": True, "summary": "ok"})]))

    res = agent.solo("任务")
    return res, tcalls["n"]


def test_gate_enforcement_refeeds_when_impl_no_tests(tmp_path, monkeypatch):
    """gate 模式：有实现 + collected-0 → 回灌强制补，agent 补后第二次跑测试通过（test 调 2 次）。"""
    res, n = _run_gate_b(tmp_path, monkeypatch, "gate", ["miniql/executor.py"],
                         [{"returncode": 5, "stdout": "collected 0 items", "stderr": ""},
                          {"returncode": 0, "stdout": "1 passed", "stderr": ""}])
    assert n == 2, f"gate 模式应回灌后重跑，test 应被调 2 次，实际 {n}"
    assert res["task_complete_signal"]["gate_status"] != "no_command"


def test_off_enforcement_collected0_is_no_command(tmp_path, monkeypatch):
    """off 模式：同样 collected-0 → 直接 no_command（test 只调 1 次，不回灌）。"""
    res, n = _run_gate_b(tmp_path, monkeypatch, "off", ["miniql/executor.py"],
                         [{"returncode": 5, "stdout": "collected 0 items", "stderr": ""}])
    assert n == 1, f"off 模式不回灌，test 应只调 1 次，实际 {n}"
    assert res["task_complete_signal"]["gate_status"] == "no_command"


def test_gate_enforcement_no_impl_no_refeed(tmp_path, monkeypatch):
    """gate 模式但没改实现（只动数据/文档）+ collected-0 → 不回灌，仍 no_command。"""
    res, n = _run_gate_b(tmp_path, monkeypatch, "gate", ["data/emp.csv", "README.md"],
                         [{"returncode": 5, "stdout": "collected 0 items", "stderr": ""}])
    assert n == 1
    assert res["task_complete_signal"]["gate_status"] == "no_command"


# ── 解法B v2：主动 smoke 硬卡 ──

def test_gate_v2_smoke_hardstop_on_entry_no_smoke(tmp_path, monkeypatch):
    """gate v2：改了 __main__.py 但无 tests/test_smoke.py → 测试运行前先回灌补 smoke；
    补完（smoke 第二轮存在）后放行 passed。test 第一轮不被调（前置拦截）。"""
    _setup_ws(tmp_path)
    monkeypatch.setitem(config._effective_config, "solo_test_enforcement", "gate")
    monkeypatch.setattr(agent, "_dispatch_tool_calls", _stub_dispatch)
    monkeypatch.setattr(agent._task_log_mod, "snapshot_files_modified",
                        lambda: ["miniql/__main__.py", "miniql/executor.py"])
    monkeypatch.setattr(agent, "_infer_test_scope", lambda *a, **k: ["tests/test_x.py"])
    monkeypatch.setattr(agent, "_force_include_smoke", lambda sc, ws: sc)
    monkeypatch.setattr(agent, "_detect_python_test_cmd", lambda ws, scope=None: "pytest")
    # smoke：第一次查不存在（触发硬卡）→ drive 后存在（放行）
    smoke = {"n": 0}

    def fake_smoke(ws):
        smoke["n"] += 1
        return smoke["n"] > 1

    monkeypatch.setattr(agent, "_smoke_exists", fake_smoke)
    tcalls = {"n": 0}
    monkeypatch.setattr(agent, "test", lambda cmd, timeout_sec=None: (
        tcalls.__setitem__("n", tcalls["n"] + 1) or {"returncode": 0, "stdout": "1 passed", "stderr": ""}))
    monkeypatch.setattr(agent, "call_llm", lambda msgs, **kw: _make_response(
        "done", [("c1", "task_complete", {"success": True, "summary": "ok"})]))
    res = agent.solo("任务")
    assert tcalls["n"] == 1, f"前置硬卡应拦掉第一轮测试，test 只在补 smoke 后调 1 次，实际 {tcalls['n']}"
    assert res["task_complete_signal"]["gate_status"] == "passed"


def test_gate_v2_no_smoke_if_never_added(tmp_path, monkeypatch):
    """gate v2：改入口 + 单测通过但 agent 死活不补 smoke → 最终 no_smoke 不放行（success=False）。"""
    _setup_ws(tmp_path)
    monkeypatch.setitem(config._effective_config, "solo_test_enforcement", "gate")
    monkeypatch.setattr(agent, "_dispatch_tool_calls", _stub_dispatch)
    monkeypatch.setattr(agent._task_log_mod, "snapshot_files_modified",
                        lambda: ["miniql/__main__.py", "miniql/executor.py"])
    monkeypatch.setattr(agent, "_infer_test_scope", lambda *a, **k: ["tests/test_x.py"])
    monkeypatch.setattr(agent, "_force_include_smoke", lambda sc, ws: sc)
    monkeypatch.setattr(agent, "_detect_python_test_cmd", lambda ws, scope=None: "pytest")
    monkeypatch.setattr(agent, "_smoke_exists", lambda ws: False)  # 始终没补
    monkeypatch.setattr(agent, "test", lambda cmd, timeout_sec=None: {
        "returncode": 0, "stdout": "1 passed", "stderr": ""})
    monkeypatch.setattr(agent, "call_llm", lambda msgs, **kw: _make_response(
        "done", [("c1", "task_complete", {"success": True, "summary": "ok"})]))
    res = agent.solo("任务")
    assert res["task_complete_signal"]["gate_status"] == "no_smoke"
    assert res["success"] is False


def test_off_mode_no_v2_smoke_hardstop(tmp_path, monkeypatch):
    """off 模式：改了入口 + collected-0 也不触发 v2 硬卡 → 直接 no_command。"""
    res, n = _run_gate_b(tmp_path, monkeypatch, "off", ["miniql/__main__.py"],
                         [{"returncode": 5, "stdout": "collected 0 items", "stderr": ""}])
    assert n == 1
    assert res["task_complete_signal"]["gate_status"] == "no_command"
