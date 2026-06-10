"""R19 轮次耗尽兜底：_final_gate_verdict 各分支 + solo() 耗尽时走兜底（不再无条件 failed）。"""
import os
import sys
import json as _json
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import agent


def _setup_verdict_mocks(monkeypatch, *, modified, scope, targeted_cmd, full_cmd,
                         returncode, entry=False, smoke=False):
    """配置 _final_gate_verdict 的全部依赖。"""
    monkeypatch.setattr(agent._task_log_mod, "snapshot_files_modified", lambda: modified)
    monkeypatch.setattr(agent, "_infer_test_scope", lambda *a, **k: scope)
    monkeypatch.setattr(agent, "_force_include_smoke", lambda sc, ws: sc)
    monkeypatch.setattr(agent, "_detect_python_test_cmd",
                        lambda ws, scope=None: (targeted_cmd if scope else full_cmd))
    monkeypatch.setattr(agent, "test", lambda cmd, timeout_sec=None: {"returncode": returncode, "stdout": "", "stderr": ""})
    monkeypatch.setattr(agent, "_entry_modified", lambda m: entry)
    monkeypatch.setattr(agent, "_smoke_exists", lambda ws: smoke)
    monkeypatch.setattr(agent, "_PROJECT_TYPE", "python", raising=False)


def test_verdict_targeted_green_no_entry_passed(monkeypatch):
    """targeted 绿 + 未改入口 → passed（救回假阴性）。"""
    _setup_verdict_mocks(monkeypatch, modified=["pkg/foo.py"], scope=["tests/test_foo.py"],
                         targeted_cmd="pytest -q tests/test_foo.py", full_cmd="pytest -q",
                         returncode=0, entry=False, smoke=False)
    status, _tr = agent._final_gate_verdict("/ws", 300)
    assert status == "passed"


def test_verdict_targeted_green_entry_no_smoke(monkeypatch):
    """targeted 绿 + 改入口且无 smoke → no_smoke（暴露缺陷，R19 即此情形）。"""
    _setup_verdict_mocks(monkeypatch, modified=["pkg/__main__.py"], scope=["tests/test_foo.py"],
                         targeted_cmd="pytest -q tests/test_foo.py", full_cmd="pytest -q",
                         returncode=0, entry=True, smoke=False)
    status, _tr = agent._final_gate_verdict("/ws", 300)
    assert status == "no_smoke"


def test_verdict_targeted_green_entry_with_smoke_passed(monkeypatch):
    """targeted 绿 + 改入口但有 smoke → passed。"""
    _setup_verdict_mocks(monkeypatch, modified=["pkg/__main__.py"], scope=["tests/test_foo.py"],
                         targeted_cmd="pytest -q tests/test_foo.py", full_cmd="pytest -q",
                         returncode=0, entry=True, smoke=True)
    status, _tr = agent._final_gate_verdict("/ws", 300)
    assert status == "passed"


def test_verdict_red_failed(monkeypatch):
    """测试红 → failed（与现状一致）。"""
    _setup_verdict_mocks(monkeypatch, modified=["pkg/foo.py"], scope=["tests/test_foo.py"],
                         targeted_cmd="pytest -q tests/test_foo.py", full_cmd="pytest -q",
                         returncode=1, entry=False, smoke=False)
    status, _tr = agent._final_gate_verdict("/ws", 300)
    assert status == "failed"


def test_verdict_no_command(monkeypatch):
    """无可用测试命令 → no_command。"""
    _setup_verdict_mocks(monkeypatch, modified=["pkg/foo.py"], scope=[],
                         targeted_cmd=None, full_cmd=None,
                         returncode=0, entry=False, smoke=False)
    status, _tr = agent._final_gate_verdict("/ws", 300)
    assert status == "no_command"


def test_verdict_full_green_coverage_unknown(monkeypatch):
    """scope 空 → 全量兜底绿 → coverage_unknown（不能证明本次改动被覆盖）。"""
    _setup_verdict_mocks(monkeypatch, modified=["pkg/foo.py"], scope=[],
                         targeted_cmd=None, full_cmd="pytest -q",
                         returncode=0, entry=False, smoke=False)
    status, _tr = agent._final_gate_verdict("/ws", 300)
    assert status == "coverage_unknown"


# ── collected-0 边界：pytest 退出码5/no tests → no_command，非 failed ──

def test_no_tests_collected_helper():
    assert agent._no_tests_collected({"returncode": 5, "stdout": "", "stderr": ""}) is True
    assert agent._no_tests_collected({"returncode": 1, "stdout": "collected 0 items\n", "stderr": ""}) is True
    assert agent._no_tests_collected({"returncode": 0, "stdout": "no tests ran in 0.1s", "stderr": ""}) is True
    assert agent._no_tests_collected({"returncode": 1, "stdout": "1 failed", "stderr": ""}) is False
    assert agent._no_tests_collected({"returncode": 0, "stdout": "2 passed", "stderr": ""}) is False
    assert agent._no_tests_collected(None) is False


def test_verdict_collected_zero_is_no_command(monkeypatch):
    """兜底裁定：全量 pytest collected 0（rc=5）→ no_command，而非 failed（R20 即此情形）。"""
    _setup_verdict_mocks(monkeypatch, modified=["pkg/__main__.py"], scope=[],
                         targeted_cmd=None, full_cmd="pytest",
                         returncode=5, entry=True, smoke=False)
    status, _tr = agent._final_gate_verdict("/ws", 300)
    assert status == "no_command"


def test_normal_gate_collected_zero_is_no_command(tmp_path, monkeypatch):
    """正常 gate 循环：agent 完成后跑测试 collected 0 → no_command break，不回灌。"""
    import config
    config.set_workspace_dir(str(tmp_path))
    import tools
    tools._reinit_paths()
    agent._reinit_paths()

    monkeypatch.setattr(agent, "_dispatch_tool_calls", _stub_dispatch_noop)
    monkeypatch.setattr(agent, "_infer_test_scope", lambda *a, **k: [])
    monkeypatch.setattr(agent, "_force_include_smoke", lambda sc, ws: sc)
    monkeypatch.setattr(agent, "_detect_python_test_cmd", lambda ws, scope=None: "pytest")
    monkeypatch.setattr(agent, "test",
                        lambda cmd, timeout_sec=None: {"returncode": 5, "stdout": "collected 0 items", "stderr": ""})
    monkeypatch.setattr(agent, "call_llm", lambda msgs, **kw: _make_response(
        "done", [("c1", "task_complete", {"success": True, "summary": "ok"})]))

    res = agent.solo("任务")
    assert res["task_complete_signal"]["gate_status"] == "no_command"


# ── 集成：solo() 轮次耗尽时走兜底，不再无条件 failed ──

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


def _stub_dispatch_noop(tool_calls, **kw):
    return [{"result": {}} for _ in tool_calls]


def test_solo_exhausted_runs_verdict_not_failed(tmp_path, monkeypatch):
    """soft_limit=1：初始 drive 一轮就耗尽（agent 不 task_complete），gate 走兜底；
    兜底测试绿（targeted 无入口）→ gate_status=passed，而非旧的无条件 failed。"""
    import config
    config.set_workspace_dir(str(tmp_path))
    import tools
    tools._reinit_paths()
    agent._reinit_paths()

    monkeypatch.setattr(agent, "_SOLO_SOFT_LIMIT", 1)
    monkeypatch.setattr(agent, "_dispatch_tool_calls", _stub_dispatch_noop)
    monkeypatch.setattr(agent, "_infer_test_scope", lambda *a, **k: ["tests/test_foo.py"])
    monkeypatch.setattr(agent, "_force_include_smoke", lambda sc, ws: sc)
    monkeypatch.setattr(agent, "_detect_python_test_cmd",
                        lambda ws, scope=None: "pytest -q tests/test_foo.py" if scope else None)
    monkeypatch.setattr(agent, "test", lambda cmd, timeout_sec=None: {"returncode": 0, "stdout": "", "stderr": ""})
    monkeypatch.setattr(agent, "_entry_modified", lambda m: False)
    monkeypatch.setattr(agent, "_smoke_exists", lambda ws: False)

    # agent 一轮只读文件，不 task_complete → 撞 soft_limit=1
    monkeypatch.setattr(agent, "call_llm",
                        lambda msgs, **kw: _make_response("分析中", [("c1", "read_file", {"filename": "x.py"})]))

    res = agent.solo("任务")
    # 兜底生效：测试绿 → passed（而非旧逻辑的无条件 failed）
    assert res["task_complete_signal"]["gate_status"] == "passed"
