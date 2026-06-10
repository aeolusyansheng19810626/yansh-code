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
    """写一轮 → task_complete(success=true)；gate 无测试命令 → P0-1：no_command → success=False。"""
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
    # P0-1：无测试命令 → gate_status=no_command → final_success=False（零外部复核不得放行）
    assert res["success"] is False
    assert res["task_complete_signal"]["gate_status"] == "no_command"
    assert res["task_complete_signal"]["agent_completed"] is True
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
    """agent 先 task_complete，但外部 gate 测试首轮失败 → 回灌再驱动 → 二轮通过。
    P0-1：全量兜底 coverage=full → gate_status=coverage_unknown → final_success=False。"""
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

    def mock_test(cmd, timeout_sec=None):
        r = test_results[min(test_calls["n"], len(test_results) - 1)]
        test_calls["n"] += 1
        return r

    monkeypatch.setattr(agent, "call_llm", mock_llm)
    monkeypatch.setattr(agent, "test", mock_test)

    res = agent.solo("实现并测试")
    # P0-1：_detect_python_test_cmd 不区分 scope，coverage=full → gate_status=coverage_unknown
    # coverage_unknown 不得视为成功（全量旧测试绿不能证明本次改动被覆盖）
    assert res["success"] is False
    assert res["task_complete_signal"]["gate_status"] == "coverage_unknown"
    assert res["task_complete_signal"]["agent_completed"] is True
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


# ========== P0 新增用例 ==========

# ── P0-1：gate 三态真值表 ──

def _make_targeted_test_setup(tmp_path, monkeypatch, agent_success, test_returncode):
    """公共 helper：设置 targeted scope（直接 mock _infer_test_scope 返回非空）。"""
    _setup_ws(tmp_path)
    monkeypatch.setattr(agent, "_dispatch_tool_calls", _stub_dispatch)
    # targeted_cmd 路径：scope 非空时 _detect_python_test_cmd 返回命令
    monkeypatch.setattr(agent, "_detect_python_test_cmd", lambda ws, scope=None: "pytest -q tests/test_foo.py" if scope else None)
    monkeypatch.setattr(agent, "_infer_test_scope", lambda *a, **k: ["tests/test_foo.py"])

    llm_seq = [_make_response("done", [("c1", "task_complete", {"success": agent_success, "summary": "s"})])]
    calls = {"n": 0}

    def mock_llm(msgs, **kw):
        r = llm_seq[min(calls["n"], len(llm_seq) - 1)]
        calls["n"] += 1
        return r

    def mock_test(cmd, timeout_sec=None):
        return {"returncode": test_returncode, "stdout": "ok" if test_returncode == 0 else "", "stderr": "FAILED" if test_returncode != 0 else ""}

    monkeypatch.setattr(agent, "call_llm", mock_llm)
    monkeypatch.setattr(agent, "test", mock_test)


def test_p01_agent_abandoned_gate_green_is_false(tmp_path, monkeypatch):
    """P0-1 原 bug ①：agent 放弃（success=false）但测试绿 → final_success 必须 False。"""
    _make_targeted_test_setup(tmp_path, monkeypatch, agent_success=False, test_returncode=0)
    res = agent.solo("任务")
    assert res["success"] is False, "agent 放弃时，gate 绿不得覆盖失败"
    assert res["task_complete_signal"]["agent_completed"] is False
    assert res["task_complete_signal"]["gate_status"] == "passed"


def test_p01_agent_completed_no_test_cmd_is_false(tmp_path, monkeypatch):
    """P0-1 原 bug ②：agent 自述成功但无测试命令 → final_success 必须 False（no_command）。"""
    _setup_ws(tmp_path)
    monkeypatch.setattr(agent, "_dispatch_tool_calls", _stub_dispatch)
    monkeypatch.setattr(agent, "_detect_python_test_cmd", lambda *a, **k: None)

    monkeypatch.setattr(agent, "call_llm", lambda msgs, **kw: _make_response(
        "done", [("c1", "task_complete", {"success": True, "summary": "完成"})]))

    res = agent.solo("任务")
    assert res["success"] is False, "无测试命令不得放行"
    assert res["task_complete_signal"]["gate_status"] == "no_command"
    assert res["task_complete_signal"]["agent_completed"] is True


def test_p01_agent_completed_targeted_green_is_true(tmp_path, monkeypatch):
    """P0-1：agent 完成 + targeted 测试绿 → final_success True（正常路径）。"""
    _make_targeted_test_setup(tmp_path, monkeypatch, agent_success=True, test_returncode=0)
    res = agent.solo("任务")
    assert res["success"] is True
    assert res["task_complete_signal"]["gate_status"] == "passed"
    assert res["task_complete_signal"]["agent_completed"] is True


def test_p01_coverage_unknown_is_false(tmp_path, monkeypatch):
    """P0-1：scope 落空 → full 兜底绿 → coverage_unknown → final_success False。"""
    _setup_ws(tmp_path)
    monkeypatch.setattr(agent, "_dispatch_tool_calls", _stub_dispatch)
    # scope 空（_infer_test_scope 返回 []）→ targeted_cmd=None → 全量兜底
    monkeypatch.setattr(agent, "_detect_python_test_cmd", lambda ws, scope=None: "pytest -q" if scope is None or not scope else None)
    monkeypatch.setattr(agent, "_infer_test_scope", lambda *a, **k: [])

    monkeypatch.setattr(agent, "call_llm", lambda msgs, **kw: _make_response(
        "done", [("c1", "task_complete", {"success": True, "summary": "完成"})]))
    monkeypatch.setattr(agent, "test", lambda cmd, timeout_sec=None: {"returncode": 0, "stdout": "1 passed", "stderr": ""})

    res = agent.solo("任务")
    assert res["success"] is False
    assert res["task_complete_signal"]["gate_status"] == "coverage_unknown"


# ── P0-2：判定分类 ──

def test_p02_classify_timeout():
    """_classify_test_failure：error_kind=timeout → kind=timeout。"""
    kind, hint = agent._classify_test_failure({"error_kind": "timeout", "returncode": -1, "stdout": "", "stderr": ""})
    assert kind == "timeout"
    assert "超时" in hint


def test_p02_classify_rc2():
    """_classify_test_failure：rc=2 → uncollectable。"""
    kind, hint = agent._classify_test_failure({"returncode": 2, "stdout": "", "stderr": ""})
    assert kind == "uncollectable"


def test_p02_classify_import_error():
    """_classify_test_failure：ImportError 在输出中 → uncollectable。"""
    kind, hint = agent._classify_test_failure({"returncode": 1, "stdout": "ImportError: no module", "stderr": ""})
    assert kind == "uncollectable"


def test_p02_classify_assertion():
    """_classify_test_failure：rc=1 无特殊标记 → assertion。"""
    kind, hint = agent._classify_test_failure({"returncode": 1, "stdout": "FAILED tests/x.py - AssertionError", "stderr": ""})
    assert kind == "assertion"
    assert "断言" in hint


# ── P0-3：compact 锚点 ──

def test_p03_compact_plan_anchor_injected(tmp_path, monkeypatch):
    """compact 时 plan_anchor 非空 → new_msgs 含 '[开场规划锚点' 系统消息。"""
    import agent as _agent
    # 构造足够多的 messages 触发 compact（绕过 token 阈值，直接调 _compact_messages）
    plan_text = "规划：实现 foo + bar，接口 foo(x)->int，bar(y)->str"
    head = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "task"},
    ]
    pairs_flat = []
    for i in range(5):
        pairs_flat.append({"role": "assistant", "content": f"步骤 {i}", "tool_calls": None})
        pairs_flat.append({"role": "user", "content": f"结果 {i}"})
    msgs = head + pairs_flat

    # mock _summarize_old_history 避免真实 LLM 调用
    monkeypatch.setattr(_agent, "_summarize_old_history", lambda text: "历史摘要")

    new_msgs = _agent._compact_messages(msgs, keep_recent_pairs=2, plan_anchor=plan_text)
    roles_contents = [(m["role"], m["content"]) for m in new_msgs]
    anchor_msgs = [c for r, c in roles_contents if r == "system" and c.startswith("[开场规划锚点")]
    assert len(anchor_msgs) == 1, f"应有 1 条锚点 system 消息，实际：{roles_contents}"
    assert plan_text in anchor_msgs[0]


def test_p03_compact_anchor_survives_multiple_compacts(tmp_path, monkeypatch):
    """多次 compact 后锚点仍在（不丢失）。"""
    import agent as _agent
    plan_text = "规划：接口 A→B→C"
    monkeypatch.setattr(_agent, "_summarize_old_history", lambda text: "摘要")

    def build_msgs(head, n_pairs):
        msgs = list(head)
        for i in range(n_pairs):
            msgs.append({"role": "assistant", "content": f"步 {i}", "tool_calls": None})
            msgs.append({"role": "user", "content": f"结果 {i}"})
        return msgs

    head = [{"role": "system", "content": "sys"}, {"role": "user", "content": "task"}]
    msgs = build_msgs(head, 6)

    # 第一次 compact
    msgs1 = _agent._compact_messages(msgs, keep_recent_pairs=2, plan_anchor=plan_text)
    anchor1 = [m for m in msgs1 if m["role"] == "system" and "[开场规划锚点" in m["content"]]
    assert anchor1, "第一次 compact 后锚点应存在"

    # 扩充 msgs1，模拟继续运行后再次触发 compact
    msgs2_input = list(msgs1)
    for i in range(4):
        msgs2_input.append({"role": "assistant", "content": f"新步 {i}", "tool_calls": None})
        msgs2_input.append({"role": "user", "content": f"新结果 {i}"})

    msgs2 = _agent._compact_messages(msgs2_input, keep_recent_pairs=2, plan_anchor=plan_text)
    anchor2 = [m for m in msgs2 if m["role"] == "system" and "[开场规划锚点" in m["content"]]
    assert anchor2, "第二次 compact 后锚点应仍然存在（不丢失）"


# ── P0-4：smoke 强并入 ──

def test_p04_force_include_smoke_adds_when_exists(tmp_path):
    """tests/test_smoke.py 存在且 scope 未含 → _force_include_smoke 加进去。"""
    smoke_dir = tmp_path / "tests"
    smoke_dir.mkdir()
    (smoke_dir / "test_smoke.py").write_text("# smoke")

    import config
    config.set_workspace_dir(str(tmp_path))
    import agent as _agent
    _agent._reinit_paths()

    scope = ["tests/test_foo.py"]
    result = _agent._force_include_smoke(scope, tmp_path)
    assert "tests/test_smoke.py" in result
    assert "tests/test_foo.py" in result


def test_p04_force_include_smoke_no_add_when_absent(tmp_path):
    """tests/test_smoke.py 不存在 → _force_include_smoke 不加。"""
    import config
    config.set_workspace_dir(str(tmp_path))
    import agent as _agent
    _agent._reinit_paths()

    scope = ["tests/test_foo.py"]
    result = _agent._force_include_smoke(scope, tmp_path)
    assert "tests/test_smoke.py" not in result


def test_p04_force_include_smoke_no_duplicate(tmp_path):
    """tests/test_smoke.py 已在 scope → 不重复加入。"""
    smoke_dir = tmp_path / "tests"
    smoke_dir.mkdir()
    (smoke_dir / "test_smoke.py").write_text("# smoke")

    import config
    config.set_workspace_dir(str(tmp_path))
    import agent as _agent
    _agent._reinit_paths()

    scope = ["tests/test_smoke.py", "tests/test_foo.py"]
    result = _agent._force_include_smoke(scope, tmp_path)
    assert result.count("tests/test_smoke.py") == 1


# ── P0-5：回灌内容 ──

def test_p05_build_gate_feedback_both_channels():
    """_build_gate_feedback：stdout 和 stderr 都有内容时两段都出现，不二选一。"""
    tr = {"returncode": 1, "stdout": "FAILED tests/x.py - boom", "stderr": "Traceback: error"}
    feedback = agent._build_gate_feedback("pytest -q", tr, "assertion", "断言失败，正常定位修复。")
    assert "STDOUT" in feedback
    assert "STDERR" in feedback
    assert "FAILED tests/x.py" in feedback
    assert "Traceback" in feedback


def test_p05_build_gate_feedback_only_stdout():
    """_build_gate_feedback：只有 stdout 时不出现 STDERR 段。"""
    tr = {"returncode": 1, "stdout": "FAILED tests/x.py", "stderr": ""}
    feedback = agent._build_gate_feedback("pytest -q", tr, "assertion", "hint")
    assert "STDOUT" in feedback
    assert "STDERR" not in feedback


def test_p05_clip_preserves_head_and_tail():
    """_clip：超长字符串保头尾，省略中间。"""
    s = "A" * 2000 + "MIDDLE" * 100 + "B" * 2000
    result = agent._clip(s, head=100, tail=100)
    assert result.startswith("A" * 100)
    assert result.endswith("B" * 100)
    assert "omitted" in result


# ========== P1 新增用例 ==========

# ── P1-7：_infer_test_scope 支持 *_test.py ──

def test_p17_infer_scope_finds_foo_test_py(tmp_path):
    """P1-7：tests/ 下有 foo_test.py（*_test.py 命名）→ 源文件 foo.py 能命中。"""
    import config
    config.set_workspace_dir(str(tmp_path))
    import agent as _agent
    _agent._reinit_paths()

    # 构造 tests/foo_test.py
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "foo_test.py").write_text("# foo_test")

    plan_files = [{"filename": "foo.py"}]
    scope = _agent._infer_test_scope(plan_files)
    assert any("foo_test.py" in p for p in scope), f"未找到 foo_test.py，scope={scope}"


def test_p17_infer_scope_test_and_xtest_both_found(tmp_path):
    """P1-7：同时存在 test_foo.py 和 foo_test.py → 两个都命中，去重正确。"""
    import config
    config.set_workspace_dir(str(tmp_path))
    import agent as _agent
    _agent._reinit_paths()

    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_foo.py").write_text("# test_foo")
    (tests_dir / "foo_test.py").write_text("# foo_test")

    plan_files = [{"filename": "foo.py"}]
    scope = _agent._infer_test_scope(plan_files)
    assert any("test_foo.py" in p for p in scope), f"未找到 test_foo.py，scope={scope}"
    assert any("foo_test.py" in p for p in scope), f"未找到 foo_test.py，scope={scope}"
    assert len(scope) == len(set(scope)), "scope 含重复项"


# ── P1-8：move_file 成功后 record_file_modified ──

def test_p18_move_file_records_src_and_dst(tmp_path, monkeypatch):
    """P1-8：move_file 成功 → record_file_modified 对 src 和 dst 各调一次。"""
    _setup_ws(tmp_path)
    from unittest.mock import patch, MagicMock

    recorded = []

    with patch("agent.move_file") as mock_move, \
         patch("agent._task_log_mod") as mock_log, \
         patch("agent._backup_file_if_needed"):
        mock_move.return_value = {"success": True}
        mock_log.record_file_modified.side_effect = lambda f: recorded.append(f)

        # 构造一个最小 tool_call
        tc = MagicMock()
        tc.id = "c1"
        tc.function.name = "move_file"
        tc.function.arguments = _json.dumps({"src": "old.py", "dst": "new.py"})

        # 调用内部分发（args 已解析，snap=None，allow_confirm=False）
        parsed_args = {"src": "old.py", "dst": "new.py"}
        agent._dispatch_tool_call_inner(tc, parsed_args, snap=None, allow_confirm=False)

    assert "old.py" in recorded, f"src 未记录，recorded={recorded}"
    assert "new.py" in recorded, f"dst 未记录，recorded={recorded}"


# ── P1-9：gate drive_limit 限制 ──

def test_p19_gate_drive_soft_limit_bounded(tmp_path, monkeypatch):
    """P1-9：gate 回灌时 _solo_drive soft_limit 被限制在 total_rounds + 15 以内。"""
    _setup_ws(tmp_path)

    drive_soft_limits = []

    def capture_drive(messages, tools, compact_state, *, soft_limit, **kw):
        drive_soft_limits.append(soft_limit)
        # 模拟 drive：不执行任何轮，直接返回（total_rounds 不变）
        return {"early_exit": False, "success": False, "summary": ""}

    monkeypatch.setattr(agent, "_solo_drive", capture_drive)
    monkeypatch.setattr(agent, "_dispatch_tool_calls", _stub_dispatch)

    # 第一次 drive（初始）：被调时 total_rounds=0，soft_limit=_SOLO_SOFT_LIMIT
    # gate：测试第一次失败→回灌→第二次通过
    test_results = [
        {"returncode": 1, "stderr": "FAILED", "stdout": ""},
        {"returncode": 0, "stderr": "", "stdout": "1 passed"},
    ]
    test_calls = {"n": 0}

    def mock_test(cmd, timeout_sec=None):
        r = test_results[min(test_calls["n"], len(test_results) - 1)]
        test_calls["n"] += 1
        return r

    monkeypatch.setattr(agent, "call_llm", lambda msgs, **kw: _make_response(
        "done", [("c1", "task_complete", {"success": True, "summary": "ok"})]))
    monkeypatch.setattr(agent, "test", mock_test)
    monkeypatch.setattr(agent, "_detect_python_test_cmd", lambda ws, scope=None: "pytest -q")

    agent.solo("任务")

    # drive_soft_limits[0] 是初始 drive（=_SOLO_SOFT_LIMIT）
    # drive_soft_limits[1] 是 gate 回灌 drive，应 <= total_rounds_at_that_point + 15
    assert len(drive_soft_limits) >= 2, f"应有至少 2 次 drive 调用，实际={drive_soft_limits}"
    gate_drive_limit = drive_soft_limits[1]
    assert gate_drive_limit <= agent._SOLO_SOFT_LIMIT, "gate drive limit 不得超过全局上限"
    # 初始 drive 的 soft_limit 减去 gate drive 的 soft_limit 应 >= _SOLO_SOFT_LIMIT - 15
    assert drive_soft_limits[0] - gate_drive_limit >= agent._SOLO_SOFT_LIMIT - agent._SOLO_GATE_DRIVE_LIMIT - 5, \
        f"gate drive_limit 未被限制，drive_soft_limits={drive_soft_limits}"


# ── P1-10：同错收敛检测 ──

def test_p110_same_error_convergence_stops_gate(tmp_path, monkeypatch):
    """P1-10：连续两轮 test_cmd / err / modified 完全相同 → gate 提前退出 gate_status=failed。"""
    _setup_ws(tmp_path)

    # drive 不修改任何文件，也不 task_complete
    drive_call = {"n": 0}

    def capture_drive(messages, tools, compact_state, *, soft_limit, **kw):
        drive_call["n"] += 1
        return {"early_exit": False, "success": False, "summary": ""}

    monkeypatch.setattr(agent, "_solo_drive", capture_drive)
    monkeypatch.setattr(agent, "_dispatch_tool_calls", _stub_dispatch)
    monkeypatch.setattr(agent, "call_llm", lambda msgs, **kw: _make_response(
        "done", [("c1", "task_complete", {"success": True, "summary": "ok"})]))

    # 每次测试都返回相同的失败
    monkeypatch.setattr(agent, "test", lambda cmd, timeout_sec=None: {
        "returncode": 1, "stderr": "same error", "stdout": ""
    })
    monkeypatch.setattr(agent, "_detect_python_test_cmd", lambda ws, scope=None: "pytest -q")

    res = agent.solo("任务")
    assert res["task_complete_signal"]["gate_status"] == "failed"
    # 第一轮无 _prev，第二轮检测到相同 → 停止，gate_round 应很小
    assert drive_call["n"] <= 3, f"同错应快速收敛，但 drive 调用了 {drive_call['n']} 次"


# ── P1-11：gate 顶部先检查轮次 ──

def test_p111_gate_skips_test_when_rounds_exhausted(tmp_path, monkeypatch):
    """P1-11：total_rounds >= soft_limit 时，gate 不再跑测试（test 未被调用）。"""
    _setup_ws(tmp_path)

    test_called = {"n": 0}

    def counting_test(cmd, timeout_sec=None):
        test_called["n"] += 1
        return {"returncode": 0, "stdout": "ok", "stderr": ""}

    # drive 耗尽所有轮次（修改 no_progress_state["total_rounds"] 到上限）
    def exhausting_drive(messages, tools, compact_state, *, soft_limit, no_progress_state, **kw):
        no_progress_state["total_rounds"] = agent._SOLO_SOFT_LIMIT  # 直接耗尽
        return {"early_exit": True, "success": True, "summary": "ok"}

    monkeypatch.setattr(agent, "_solo_drive", exhausting_drive)
    monkeypatch.setattr(agent, "_dispatch_tool_calls", _stub_dispatch)
    monkeypatch.setattr(agent, "call_llm", lambda msgs, **kw: _make_response(
        "done", [("c1", "task_complete", {"success": True, "summary": "ok"})]))
    monkeypatch.setattr(agent, "test", counting_test)
    monkeypatch.setattr(agent, "_detect_python_test_cmd", lambda ws, scope=None: "pytest -q")

    res = agent.solo("任务")
    assert test_called["n"] == 0, f"轮次耗尽后不应跑测试，但 test 被调了 {test_called['n']} 次"
    assert res["task_complete_signal"]["gate_status"] == "failed"
