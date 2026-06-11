"""F3 + 修复2 补测：
- Fix B 三分支（全量绿/全量也collected-0/无更优全量命令）
- Fix C 两轮行为（第一次回灌 / 第二次转 no_command）
- Fix D（gate 轮耗尽 → _final_gate_verdict + _ever_completed 认可）
- 修复2（_final_gate_verdict targeted collected-0 → 全量回退仲裁）
"""
import os
import sys
import json as _json
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import agent
import config


# ─────────────────────────────────────────────────────────────────────────────
# 公共桩工具
# ─────────────────────────────────────────────────────────────────────────────

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


def _stub_dispatch_with_task_complete(tool_calls, **kw):
    """task_complete 工具调用返回 _task_complete sentinel，其余 noop。"""
    import json as _j
    results = []
    for tc in tool_calls:
        name = tc.function.name
        if name == "task_complete":
            args = _j.loads(tc.function.arguments)
            results.append({"result": {
                "_task_complete": True,
                "success": bool(args.get("success", False)),
                "summary": args.get("summary", ""),
            }})
        else:
            results.append({"result": {}})
    return results


def _ok(stdout="2 passed"):
    return {"returncode": 0, "stdout": stdout, "stderr": ""}


def _col0(stdout="collected 0 items"):
    return {"returncode": 5, "stdout": stdout, "stderr": ""}


def _red(stdout="1 failed"):
    return {"returncode": 1, "stdout": stdout, "stderr": ""}


def _setup_solo_env(tmp_path, monkeypatch):
    config.set_workspace_dir(str(tmp_path))
    import tools
    tools._reinit_paths()
    agent._reinit_paths()
    monkeypatch.setattr(agent, "_dispatch_tool_calls", _stub_dispatch_noop)
    monkeypatch.setattr(agent, "_infer_test_scope", lambda *a, **k: ["tests/test_x.py"])
    monkeypatch.setattr(agent, "_force_include_smoke", lambda sc, ws: sc)
    monkeypatch.setattr(agent, "_entry_modified", lambda m: False)
    monkeypatch.setattr(agent, "_smoke_exists", lambda ws: True)
    monkeypatch.setattr(agent, "_has_impl_files", lambda m: True)


# ─────────────────────────────────────────────────────────────────────────────
# Fix B 三分支（F3 假阴性回归 + 另外两条）
# ─────────────────────────────────────────────────────────────────────────────

def test_fixb_full_green_gives_passed(tmp_path, monkeypatch):
    """F3 核心回归：targeted collected-0，全量绿 → gate_status=passed（修前为 coverage_unknown → success=False）。"""
    _setup_solo_env(tmp_path, monkeypatch)
    # 使用能正确触发 task_complete sentinel 的 dispatch stub
    monkeypatch.setattr(agent, "_dispatch_tool_calls", _stub_dispatch_with_task_complete)

    targeted_cmd = "pytest -q tests/test_x.py"
    full_cmd = "pytest -q"

    def _test_stub(cmd, timeout_sec=None):
        if cmd == targeted_cmd:
            return _col0()   # targeted 命令 collected 0
        if cmd == full_cmd:
            return _ok()     # 全量绿
        return _col0()

    monkeypatch.setattr(agent, "_detect_python_test_cmd",
                        lambda ws, scope=None: (targeted_cmd if scope else full_cmd))
    monkeypatch.setattr(agent, "test", _test_stub)
    monkeypatch.setattr(agent, "call_llm",
                        lambda msgs, **kw: _make_response(
                            "done", [("c1", "task_complete", {"success": True, "summary": "ok"})]))

    res = agent.solo("任务")
    assert res["task_complete_signal"]["gate_status"] == "passed", (
        f"期望 passed，实际 {res['task_complete_signal']['gate_status']}"
    )
    assert res["success"] is True


def test_fixb_full_also_col0_stays_no_command(tmp_path, monkeypatch):
    """Fix B：全量也 collected-0 → 不回退，保持原 test_result 落 no_command 分支。"""
    _setup_solo_env(tmp_path, monkeypatch)
    monkeypatch.setattr(agent, "_dispatch_tool_calls", _stub_dispatch_with_task_complete)

    targeted_cmd = "pytest -q tests/test_x.py"
    full_cmd = "pytest -q"

    monkeypatch.setattr(agent, "_detect_python_test_cmd",
                        lambda ws, scope=None: (targeted_cmd if scope else full_cmd))
    monkeypatch.setattr(agent, "test",
                        lambda cmd, timeout_sec=None: _col0())  # 无论全量/targeted 都 collected 0
    monkeypatch.setattr(agent, "call_llm",
                        lambda msgs, **kw: _make_response(
                            "done", [("c1", "task_complete", {"success": True, "summary": "ok"})]))

    res = agent.solo("任务")
    assert res["task_complete_signal"]["gate_status"] == "no_command"


def test_fixb_no_better_full_cmd_no_fallback(tmp_path, monkeypatch):
    """Fix B：_full_cmd == test_cmd（无更优全量命令）→ 不触发回退，直接落 collected-0 分支。"""
    _setup_solo_env(tmp_path, monkeypatch)
    monkeypatch.setattr(agent, "_dispatch_tool_calls", _stub_dispatch_with_task_complete)

    same_cmd = "pytest -q tests/test_x.py"

    monkeypatch.setattr(agent, "_detect_python_test_cmd",
                        lambda ws, scope=None: same_cmd)   # scope=None 也返回同一命令
    monkeypatch.setattr(agent, "test",
                        lambda cmd, timeout_sec=None: _col0())
    monkeypatch.setattr(agent, "call_llm",
                        lambda msgs, **kw: _make_response(
                            "done", [("c1", "task_complete", {"success": True, "summary": "ok"})]))

    res = agent.solo("任务")
    assert res["task_complete_signal"]["gate_status"] == "no_command"


# ─────────────────────────────────────────────────────────────────────────────
# Fix C：collected-0 一次性回灌标志
# ─────────────────────────────────────────────────────────────────────────────

def test_fixc_first_col0_demands_then_second_no_command(tmp_path, monkeypatch):
    """Fix C：第一次 collected-0（enforce=gate + impl 文件）→ 设 _collected0_demanded 回灌；
    第二次 collected-0 → 转 no_command（不再重复回灌，避免 8 轮空转）。"""
    _setup_solo_env(tmp_path, monkeypatch)
    monkeypatch.setattr(agent, "_dispatch_tool_calls", _stub_dispatch_with_task_complete)

    # 强制 _enforce == "gate"（solo 默认就是 gate，但显式确保）
    monkeypatch.setattr(agent, "_SOLO_GATE_MAX_ROUNDS", 8, raising=False)
    # 确保 _has_impl_files 能检测到实现文件（stub modified 包含非测试文件）
    monkeypatch.setattr(agent._task_log_mod, "snapshot_files_modified", lambda: ["pkg/impl.py"])
    # no full fallback（无更优全量命令，让 col0 走进 collected-0 分支）
    targeted_cmd = "pytest -q tests/test_x.py"
    monkeypatch.setattr(agent, "_detect_python_test_cmd",
                        lambda ws, scope=None: targeted_cmd)
    monkeypatch.setattr(agent, "test",
                        lambda cmd, timeout_sec=None: _col0())

    demand_count = [0]
    drive_count = [0]

    def _llm_stub(msgs, **kw):
        drive_count[0] += 1
        # 检查最后一条 user 消息是否是 collected-0 回灌（Fix E 文案包含「collected 0」）
        last_user = next(
            (m["content"] for m in reversed(msgs) if m.get("role") == "user" and isinstance(m.get("content"), str)),
            ""
        )
        if "collected 0" in last_user and drive_count[0] > 1:
            # 这是回灌后的第一轮 drive（demand 已经记录）
            demand_count[0] += 1
        # drive_count >= 3：第二次 gate 检测到 no_command，期间宣告完成触发最终判定
        if drive_count[0] >= 3:
            return _make_response("done", [("c1", "task_complete", {"success": True, "summary": "ok"})])
        return _make_response("working", [("c1", "read_file", {"filename": "x.py"})])

    monkeypatch.setattr(agent, "call_llm", _llm_stub)

    res = agent.solo("任务")
    # 最终 gate_status 应为 no_command（第二次 col0 转 no_command，不再回灌）
    assert res["task_complete_signal"]["gate_status"] == "no_command"


# ─────────────────────────────────────────────────────────────────────────────
# Fix D：gate_round 达上限 → while-else → _final_gate_verdict + _ever_completed
# ─────────────────────────────────────────────────────────────────────────────

def test_fixd_gate_exhausted_calls_verdict(tmp_path, monkeypatch):
    """Fix D：gate_round 达 _SOLO_GATE_MAX_ROUNDS → while-else → 调 _final_gate_verdict，
    兜底测试绿（targeted，无入口）且此前曾宣告完成 → success=True。"""
    _setup_solo_env(tmp_path, monkeypatch)
    monkeypatch.setattr(agent, "_dispatch_tool_calls", _stub_dispatch_with_task_complete)

    targeted_cmd = "pytest -q tests/test_x.py"
    full_cmd = "pytest -q"

    # 每次 gate 跑都红，且错误内容不同（避免「同一失败集合连续两轮无新写」熔断提前退出）
    call_idx = [0]
    def _test_stub(cmd, timeout_sec=None):
        i = call_idx[0]
        call_idx[0] += 1
        # 每轮给不同的 test-id，避免 err_hash 相同导致提前熔断
        return {"returncode": 1, "stdout": f"FAILED tests/test_x.py::test_case_{i}", "stderr": ""}

    monkeypatch.setattr(agent, "_SOLO_GATE_MAX_ROUNDS", 2, raising=False)
    monkeypatch.setattr(agent, "_detect_python_test_cmd",
                        lambda ws, scope=None: (targeted_cmd if scope else full_cmd))
    monkeypatch.setattr(agent, "test", _test_stub)

    # verdict 直接桩：targeted 绿
    monkeypatch.setattr(agent, "_final_gate_verdict",
                        lambda ws, to: ("passed", _ok()))

    # agent 第一轮 drive 宣告完成（让 _ever_completed=True），gate 回灌后返回 fixing（无写）
    drive_count = [0]
    def _llm_stub(msgs, **kw):
        drive_count[0] += 1
        if drive_count[0] == 1:
            return _make_response("done", [("c1", "task_complete", {"success": True, "summary": "ok"})])
        # 回灌后：返回工具调用（read_file），产生进展避免沉默退出，但不 task_complete
        return _make_response("fixing", [("cx", "read_file", {"filename": "x.py"})])
    monkeypatch.setattr(agent, "call_llm", _llm_stub)

    res = agent.solo("任务")
    assert res["task_complete_signal"]["gate_status"] == "passed", (
        f"期望 passed，实际 {res['task_complete_signal']['gate_status']}"
    )
    assert res["success"] is True


# ─────────────────────────────────────────────────────────────────────────────
# 修复2：_final_gate_verdict targeted collected-0 → 全量回退仲裁
# ─────────────────────────────────────────────────────────────────────────────

def _setup_verdict_mocks(monkeypatch, *, modified, scope, targeted_cmd, full_cmd,
                         targeted_result, full_result, entry=False, smoke=False):
    monkeypatch.setattr(agent._task_log_mod, "snapshot_files_modified", lambda: modified)
    monkeypatch.setattr(agent, "_infer_test_scope", lambda *a, **k: scope)
    monkeypatch.setattr(agent, "_force_include_smoke", lambda sc, ws: sc)

    def _detect(ws, scope=None):
        if scope:
            return targeted_cmd
        return full_cmd
    monkeypatch.setattr(agent, "_detect_python_test_cmd", _detect)

    def _test(cmd, timeout_sec=None):
        if cmd == targeted_cmd:
            return targeted_result
        if cmd == full_cmd:
            return full_result
        return _col0()
    monkeypatch.setattr(agent, "test", _test)
    monkeypatch.setattr(agent, "_entry_modified", lambda m: entry)
    monkeypatch.setattr(agent, "_smoke_exists", lambda ws: smoke)
    monkeypatch.setattr(agent, "_PROJECT_TYPE", "python", raising=False)


def test_verdict_targeted_col0_full_green_coverage_unknown(monkeypatch):
    """修复2：兜底裁定 targeted collected-0，全量绿 → 改用全量结果 → coverage_unknown（非 no_command）。"""
    _setup_verdict_mocks(
        monkeypatch,
        modified=["pkg/foo.py"],
        scope=["tests/test_foo.py"],
        targeted_cmd="pytest -q tests/test_foo.py",
        full_cmd="pytest -q",
        targeted_result=_col0(),
        full_result=_ok(),
        entry=False, smoke=False,
    )
    status, _tr = agent._final_gate_verdict("/ws", 300)
    assert status == "coverage_unknown", f"期望 coverage_unknown，实际 {status}"


def test_verdict_targeted_col0_full_red_failed(monkeypatch):
    """修复2：兜底裁定 targeted collected-0，全量能收集但红 → failed。"""
    _setup_verdict_mocks(
        monkeypatch,
        modified=["pkg/foo.py"],
        scope=["tests/test_foo.py"],
        targeted_cmd="pytest -q tests/test_foo.py",
        full_cmd="pytest -q",
        targeted_result=_col0(),
        full_result=_red(),
        entry=False, smoke=False,
    )
    status, _tr = agent._final_gate_verdict("/ws", 300)
    assert status == "failed", f"期望 failed，实际 {status}"


def test_verdict_targeted_col0_full_also_col0_no_command(monkeypatch):
    """修复2：兜底裁定 targeted collected-0，全量也 collected-0 → 保持 no_command（无正规测试）。"""
    _setup_verdict_mocks(
        monkeypatch,
        modified=["pkg/foo.py"],
        scope=["tests/test_foo.py"],
        targeted_cmd="pytest -q tests/test_foo.py",
        full_cmd="pytest -q",
        targeted_result=_col0(),
        full_result=_col0(),
        entry=False, smoke=False,
    )
    status, _tr = agent._final_gate_verdict("/ws", 300)
    assert status == "no_command", f"期望 no_command，实际 {status}"


def test_verdict_targeted_col0_no_full_cmd_no_command(monkeypatch):
    """修复2：targeted collected-0，无全量命令（_full_cmd is None）→ no_command（不崩溃）。"""
    _setup_verdict_mocks(
        monkeypatch,
        modified=["pkg/foo.py"],
        scope=["tests/test_foo.py"],
        targeted_cmd="pytest -q tests/test_foo.py",
        full_cmd=None,
        targeted_result=_col0(),
        full_result=_col0(),
        entry=False, smoke=False,
    )
    status, _tr = agent._final_gate_verdict("/ws", 300)
    assert status == "no_command"


def test_verdict_targeted_col0_same_full_cmd_no_command(monkeypatch):
    """修复2：targeted collected-0，_full_cmd == targeted_cmd（无更优） → no_command，不触发全量回退。"""
    same_cmd = "pytest -q tests/test_foo.py"
    _setup_verdict_mocks(
        monkeypatch,
        modified=["pkg/foo.py"],
        scope=["tests/test_foo.py"],
        targeted_cmd=same_cmd,
        full_cmd=same_cmd,
        targeted_result=_col0(),
        full_result=_ok(),
        entry=False, smoke=False,
    )
    status, _tr = agent._final_gate_verdict("/ws", 300)
    assert status == "no_command"
