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


def test_code_no_warning_when_task_complete_in_final_round(tmp_path):
    """[P1 #2] task_complete(success=True) 与 attempts_left=0 同轮触发时不打 '已用尽' 警告"""
    import config, tools
    import task_log as _task_log_mod
    config.set_workspace_dir(str(tmp_path))
    tools._reinit_paths()
    (tmp_path / "calc.py").write_text("def add(a,b): return a+b\n", encoding="utf-8")

    # 把每文件预算压到 2 轮，便于触发"最后一轮"边界
    orig_cfg = agent._cfg
    agent._cfg = lambda k: 2 if k == "coder_rounds_per_file" else orig_cfg(k)

    # 重置 task_log warnings
    _task_log_mod._current_task_log.clear()
    _task_log_mod._current_task_log.update({"warnings": []})

    call_count = {"n": 0}
    def mock_llm(msgs, **kw):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return _make_response(tool_calls=[("c1", "read_file", {"filename": "calc.py"})])
        # 第 2 轮（最后一轮，attempts_left 减到 0 那一次）调 task_complete success=True
        return _make_response(tool_calls=[("c2", "task_complete",
                                            {"success": True, "summary": "看完了，无需改动"})])

    orig_llm = agent.call_llm
    try:
        agent.call_llm = mock_llm
        result = agent.code(
            {"files": [{"filename": "calc.py", "intent": "审视"}], "test_command": ""},
            mode="code",
            requirement="看一下",
        )
    finally:
        agent.call_llm = orig_llm
        agent._cfg = orig_cfg

    # task_complete success=True → coder_signal 上送
    assert result is not None
    assert result["success"] is True
    # 关键断言：_task_log 里不能有"已用尽"警告
    warnings = _task_log_mod._current_task_log.get("warnings", [])
    assert not any("已用尽" in w for w in warnings), \
        f"task_complete 同轮触发时不应警告，但警告记录为: {warnings}"


# ============= P1 #5: plan 写文档前必须 explorer =============

def test_plan_needs_exploration_chinese():
    """中文强信号关键词命中"""
    assert agent._plan_needs_exploration("写一个改造方案文档，含改动范围和兼容性分析")
    assert agent._plan_needs_exploration("分析 task_complete 的影响范围和调用关系")
    assert agent._plan_needs_exploration("给出具体行号")


def test_plan_needs_exploration_english():
    """英文强信号关键词命中"""
    assert agent._plan_needs_exploration("Write a doc describing the impact analysis and affected files")
    assert agent._plan_needs_exploration("List specific lines that need changes")
    assert agent._plan_needs_exploration("Document the existing implementation")


def test_plan_needs_exploration_negative():
    """普通需求不命中"""
    assert not agent._plan_needs_exploration("加一个 multiply 函数")
    assert not agent._plan_needs_exploration("修复 test_memory.py 的失败")
    assert not agent._plan_needs_exploration("Add a new endpoint /api/v2")
    assert not agent._plan_needs_exploration("")
    assert not agent._plan_needs_exploration(None)


def test_plan_needs_exploration_avoids_overbroad_words():
    """[reviewer 建议] 普通 feature 需求里常见的'兼容性'/'现有实现'/'compatibility'/'code details'
    单字短语不应触发 explorer dispatch"""
    assert not agent._plan_needs_exploration("加 X 函数注意兼容性")
    assert not agent._plan_needs_exploration("在现有实现上加一个开关")
    assert not agent._plan_needs_exploration("Add error handling considering compatibility")
    assert not agent._plan_needs_exploration("Document code details for the API")


def test_plan_dispatches_explorer_when_keywords_match(tmp_path, monkeypatch):
    """[P1 #5] requirement 含关键词 → plan() 派 explorer subagent，summary 注入 system_prompt"""
    import config, tools
    config.set_workspace_dir(str(tmp_path))
    tools._reinit_paths()
    (tmp_path / "x.py").write_text("def foo(): pass\n", encoding="utf-8")

    explorer_called = {"n": 0}
    def mock_run_subagent(task, role="explorer", max_steps=8):
        explorer_called["n"] += 1
        explorer_called["task"] = task
        explorer_called["role"] = role
        return {"success": True,
                "summary": "EXPLORER_REPORT_MARKER: 找到 x.py:1 def foo()",
                "steps": 2, "role": role}

    captured_msgs = {}
    def mock_call_with_json_retry(label, msgs, parser, **kw):
        captured_msgs["msgs"] = msgs
        return {"files": [{"filename": "doc.md", "description": "x", "expected_edits": 1}],
                "test_command": ""}

    monkeypatch.setattr(agent, "_run_subagent", mock_run_subagent)
    monkeypatch.setattr(agent, "_call_with_json_retry", mock_call_with_json_retry)

    result = agent.plan("写一个改造方案文档，含改动范围和具体行号引用")

    assert explorer_called["n"] == 1
    assert explorer_called["role"] == "explorer"
    sys_prompt = captured_msgs["msgs"][0]["content"]
    assert "EXPLORER_REPORT_MARKER" in sys_prompt
    assert "Code exploration results" in sys_prompt
    assert result is not None
    # [P1 #5 reviewer 必改] exploration 必须持久化到 plan_result，coder 阶段才能读
    assert "_exploration" in result
    assert "EXPLORER_REPORT_MARKER" in result["_exploration"]


def test_code_propagates_exploration_to_coder_sys_prompt(tmp_path):
    """[P1 #5 reviewer 必改] plan._exploration 必须拼到 coder system_prompt
    否则 coder 写文档时仍凭训练知识猜（task #3 1 错 2 偏的根因）"""
    import config, tools
    config.set_workspace_dir(str(tmp_path))
    tools._reinit_paths()
    (tmp_path / "x.py").write_text("def foo(): pass\n", encoding="utf-8")

    captured_sys_prompts = []
    def mock_llm(msgs, **kw):
        captured_sys_prompts.append(next(m["content"] for m in msgs if m["role"] == "system"))
        return _make_response(tool_calls=[("c1", "task_complete",
                                            {"success": True, "summary": "done"})])

    orig = agent.call_llm
    try:
        agent.call_llm = mock_llm
        agent.code(
            {"files": [{"filename": "x.py", "intent": "tweak"}],
             "test_command": "",
             "_exploration": "\n\n# Code exploration\nx.py:1 def foo()\n"},
            mode="code", requirement="...",
        )
    finally:
        agent.call_llm = orig

    assert any("Code exploration" in sp for sp in captured_sys_prompts)
    assert any("x.py:1 def foo()" in sp for sp in captured_sys_prompts)


def test_plan_skips_explorer_when_no_keywords(tmp_path, monkeypatch):
    """[P1 #5] requirement 不含关键词 → plan() 走原路径，不派 explorer"""
    import config, tools
    config.set_workspace_dir(str(tmp_path))
    tools._reinit_paths()

    explorer_called = {"n": 0}
    def mock_run_subagent(task, role="explorer", max_steps=8):
        explorer_called["n"] += 1
        return {"success": True, "summary": "x", "steps": 0, "role": role}

    captured_msgs = {}
    def mock_call_with_json_retry(label, msgs, parser, **kw):
        captured_msgs["msgs"] = msgs
        return {"files": [{"filename": "add.py", "description": "x", "expected_edits": 1}],
                "test_command": ""}

    monkeypatch.setattr(agent, "_run_subagent", mock_run_subagent)
    monkeypatch.setattr(agent, "_call_with_json_retry", mock_call_with_json_retry)

    agent.plan("加一个 multiply 函数")

    assert explorer_called["n"] == 0
    sys_prompt = captured_msgs["msgs"][0]["content"]
    assert "Code exploration results" not in sys_prompt


def test_plan_falls_back_when_explorer_raises(tmp_path, monkeypatch):
    """[P1 #5] explorer 抛异常时 plan() 不应崩，走原路径"""
    import config, tools
    config.set_workspace_dir(str(tmp_path))
    tools._reinit_paths()

    def mock_run_subagent_raises(task, role="explorer", max_steps=8):
        raise RuntimeError("explorer 假装挂了")

    captured_msgs = {}
    def mock_call_with_json_retry(label, msgs, parser, **kw):
        captured_msgs["msgs"] = msgs
        return {"files": [], "test_command": ""}

    monkeypatch.setattr(agent, "_run_subagent", mock_run_subagent_raises)
    monkeypatch.setattr(agent, "_call_with_json_retry", mock_call_with_json_retry)

    # 不应抛
    agent.plan("写改造方案，含改动范围和兼容分析")

    sys_prompt = captured_msgs["msgs"][0]["content"]
    # 没有 explorer summary 但 plan 仍能跑
    assert "Code exploration results" not in sys_prompt


# ============= P1 #4: plan 接受 coder "无需修改" 信号 =============

def test_summary_no_changes_chinese():
    """中文 summary 含强信号关键词时命中"""
    assert agent._summary_says_no_changes("三处均已存在，无需修改")
    assert agent._summary_says_no_changes("不需要修改")
    assert agent._summary_says_no_changes("max_bytes 已实现，无需改动")


def test_summary_no_changes_english():
    """英文 summary 含强信号关键词时命中（大小写不敏感）"""
    assert agent._summary_says_no_changes("All three places already exist; no changes needed")
    assert agent._summary_says_no_changes("Already implemented; nothing to modify")
    assert agent._summary_says_no_changes("NOTHING TO MODIFY")


def test_summary_no_changes_negative():
    """正常修复 summary 不命中"""
    assert not agent._summary_says_no_changes("已修复 list_files max_depth=1 边界 bug")
    assert not agent._summary_says_no_changes("Added multiply function")
    assert not agent._summary_says_no_changes("修了 3 处缺参问题")
    assert not agent._summary_says_no_changes("")


def test_summary_no_changes_avoids_partial_already_exists_false_positive():
    """[P1 #4 reviewer 误报回归] 局部'已存在/已实现'但实际还要改剩余 → 不应命中

    这些是 reviewer 实测的 5 个误吞 case，单独 trigger 会造成
    multi-file short-circuit 误跳过剩余文件。
    """
    # case 1: 局部已存在但还要改其他
    assert not agent._summary_says_no_changes("已经实现了 list_files，但还需要修改 max_depth")
    # case 2: "都已经" 独立太宽
    assert not agent._summary_says_no_changes("我都已经看完了，开始改第一个")
    # case 3: 描述被改对象的"已存在"
    assert not agent._summary_says_no_changes("已存在的逻辑被替换")
    # case 4: 部分已存在剩余处理
    assert not agent._summary_says_no_changes("部分已存在，剩余的开始处理")
    # case 5: 英文同类
    assert not agent._summary_says_no_changes("Already implemented X, but need to add Y")


def test_code_short_circuits_remaining_files_on_no_changes_needed(tmp_path):
    """[P1 #4] coder 第 1 文件报'无需修改' → 跳过剩余 expected_edits 文件"""
    import config, tools
    config.set_workspace_dir(str(tmp_path))
    tools._reinit_paths()
    # 准备 2 个文件
    (tmp_path / "a.py").write_text("# a\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("# b\n", encoding="utf-8")

    call_log = []
    def mock_llm(msgs, **kw):
        # 从 user content 看当前处理哪个文件
        last_user = next((m for m in reversed(msgs) if m.get("role") == "user"), None)
        text = (last_user.get("content") if last_user else "") or ""
        for fn in ("a.py", "b.py"):
            if f"当前文件：{fn}" in text:
                call_log.append(fn)
                break
        # 第 1 文件直接 task_complete success=True，summary 含'无需修改'
        return _make_response(tool_calls=[("c1", "task_complete",
                                            {"success": True,
                                             "summary": "代码已存在，无需修改"})])

    orig = agent.call_llm
    try:
        agent.call_llm = mock_llm
        result = agent.code(
            {"files": [{"filename": "a.py", "intent": "1"},
                       {"filename": "b.py", "intent": "2"}],
             "test_command": ""},
            mode="code", requirement="...",
        )
    finally:
        agent.call_llm = orig

    # 关键断言：b.py 不应被处理（被 short-circuit 跳过）
    assert "a.py" in call_log
    assert "b.py" not in call_log, f"P1 #4: b.py 应被 no_changes_needed 信号跳过，实际 call_log: {call_log}"
    assert result is not None
    assert result.get("no_changes_needed") is True


def test_code_continues_multi_file_when_summary_does_not_match(tmp_path):
    """coder 第 1 文件 task_complete success=True 但 summary 不含关键词 → multi-file 继续"""
    import config, tools
    config.set_workspace_dir(str(tmp_path))
    tools._reinit_paths()
    (tmp_path / "a.py").write_text("# a\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("# b\n", encoding="utf-8")

    call_log = []
    def mock_llm(msgs, **kw):
        last_user = next((m for m in reversed(msgs) if m.get("role") == "user"), None)
        text = (last_user.get("content") if last_user else "") or ""
        for fn in ("a.py", "b.py"):
            if f"当前文件：{fn}" in text:
                call_log.append(fn)
                break
        return _make_response(tool_calls=[("c1", "task_complete",
                                            {"success": True, "summary": "已修改完成"})])

    orig = agent.call_llm
    try:
        agent.call_llm = mock_llm
        result = agent.code(
            {"files": [{"filename": "a.py", "intent": "1"},
                       {"filename": "b.py", "intent": "2"}],
             "test_command": ""},
            mode="code", requirement="...",
        )
    finally:
        agent.call_llm = orig

    # b.py 应被处理（summary 没命中关键词，multi-file 继续）
    assert call_log == ["a.py", "b.py"]
    assert result is not None
    assert not result.get("no_changes_needed")


# ============= P1 #6: baseline 识别用户 prompt 关键词过滤 =============

def test_prompt_test_fix_chinese_keywords():
    """中文关键词命中"""
    assert agent._prompt_requests_test_fix("tests/unit/test_memory.py 有测试失败，定位 bug 并修复")
    assert agent._prompt_requests_test_fix("修复测试")
    assert agent._prompt_requests_test_fix("修 bug 让单测过")
    assert agent._prompt_requests_test_fix("失败的测试要 fix")
    assert agent._prompt_requests_test_fix("测试不通过")


def test_prompt_test_fix_english_keywords():
    """英文关键词命中（大小写不敏感）"""
    assert agent._prompt_requests_test_fix("Fix the failing tests in test_memory.py")
    assert agent._prompt_requests_test_fix("FIX BUG in calculator")
    assert agent._prompt_requests_test_fix("make the tests pass")
    assert agent._prompt_requests_test_fix("There is a test failure")


def test_prompt_test_fix_negative():
    """无关 prompt 不命中"""
    assert not agent._prompt_requests_test_fix("加一个 multiply 函数")
    assert not agent._prompt_requests_test_fix("Add a new feature for caching")
    assert not agent._prompt_requests_test_fix("写文档说明这个 API 的兼容性")
    assert not agent._prompt_requests_test_fix("")
    assert not agent._prompt_requests_test_fix(None)


def test_prompt_test_fix_avoids_false_positive_on_unrelated_bug_word():
    """单独 'bug' 字不命中（避免误吞 prompt 含 debug / bugfix mention）"""
    # 关键词都是组合词："修 bug" / "fix bug" / "fix the bug"
    assert not agent._prompt_requests_test_fix("生成一个 bug tracker UI")
    assert not agent._prompt_requests_test_fix("debug log 加更多上下文")


def test_fix_user_content_overrides_tester_role_when_disabled(tmp_path):
    """[P1 #6] disable_baseline_skip=True 时 fix() user content 含反向覆盖文案，
    禁止 LLM 按 _TESTER_ROLE Investigation order 归属跳过"""
    import config, tools
    config.set_workspace_dir(str(tmp_path))
    tools._reinit_paths()

    captured_msgs = {}
    def mock_llm(msgs, **kw):
        captured_msgs["msgs"] = msgs
        return _make_response(tool_calls=[("c1", "task_complete",
                                            {"success": False, "summary": "测试 mock"})])

    orig = agent.call_llm
    try:
        agent.call_llm = mock_llm
        agent.fix(
            {"returncode": 1, "stderr": "FAILED test_x", "stdout": ""},
            {"files": [{"filename": "x.py"}], "test_command": ""},
            baseline_failures=None,
            disable_baseline_skip=True,
        )
    finally:
        agent.call_llm = orig

    msgs = captured_msgs["msgs"]
    user_content = next(m["content"] for m in msgs if m["role"] == "user")
    # 强制覆盖文案存在
    assert "强制覆盖" in user_content
    assert "用户明确要求" in user_content or "用户 prompt" in user_content
    # 禁止按归属跳过
    assert "不允许按归属规则跳过" in user_content
    # 旧的"归属判断必读"段不应出现（避免歧义）
    assert "归属判断（必读" not in user_content


def test_fix_user_content_keeps_legacy_path_by_default(tmp_path):
    """[P1 #6] 未启用 disable_baseline_skip 时走原路径 — 保留归属判断引导"""
    import config, tools
    config.set_workspace_dir(str(tmp_path))
    tools._reinit_paths()

    captured_msgs = {}
    def mock_llm(msgs, **kw):
        captured_msgs["msgs"] = msgs
        return _make_response(tool_calls=[("c1", "task_complete",
                                            {"success": False, "summary": "测试 mock"})])

    orig = agent.call_llm
    try:
        agent.call_llm = mock_llm
        agent.fix(
            {"returncode": 1, "stderr": "FAILED test_x", "stdout": ""},
            {"files": [{"filename": "x.py"}], "test_command": ""},
            baseline_failures={"tests/unit/test_x.py::test_pre_existing"},
            disable_baseline_skip=False,
        )
    finally:
        agent.call_llm = orig

    msgs = captured_msgs["msgs"]
    user_content = next(m["content"] for m in msgs if m["role"] == "user")
    # 原文案保留
    assert "归属判断（必读" in user_content
    assert "Pre-existing baseline failures" in user_content
    # 强制覆盖文案不应出现
    assert "强制覆盖" not in user_content


# ============= P1 #3: mechanical error detector 扩展 =============

def _count_mech(error_info: str) -> dict:
    """跑一遍 _MECH_ERROR_PATTERNS，返回 {label: count}"""
    import re as _re
    hits = {}
    for pat, label in agent._MECH_ERROR_PATTERNS:
        c = len(_re.findall(pat, error_info))
        if c > 0:
            hits[label] = c
    return hits


def test_mech_detector_typeerror_missing_arg():
    """老 case 仍被识别"""
    err = "TypeError: foo() missing 2 required positional arguments: 'a', 'b'"
    assert _count_mech(err) == {"TypeError missing argument": 1}


def test_mech_detector_nameerror():
    """[P1 #3] NameError 触发追加预算"""
    err = "NameError: name 'undefined_var' is not defined"
    assert _count_mech(err) == {"NameError": 1}


def test_mech_detector_attributeerror():
    """[P1 #3] AttributeError 触发追加预算"""
    err = "AttributeError: 'Foo' object has no attribute 'bar'"
    assert _count_mech(err) == {"AttributeError": 1}


def test_mech_detector_mixed_errors():
    """3 类同时出现，各类计数独立"""
    err = (
        "test_a: TypeError: f() missing 1 required positional argument: 'x'\n"
        "test_b: NameError: name 'foo' is not defined\n"
        "test_c: NameError: name 'bar' is not defined\n"
        "test_d: AttributeError: 'X' object has no attribute 'y'"
    )
    hits = _count_mech(err)
    assert hits["TypeError missing argument"] == 1
    assert hits["NameError"] == 2
    assert hits["AttributeError"] == 1


def test_mech_detector_no_match():
    """无机械错时返回空"""
    err = "AssertionError: expected 5 but got 3"
    assert _count_mech(err) == {}


def test_report_includes_task_complete_signal_when_provided():
    sig = {"early_exit": True, "success": True, "summary": "完成"}
    out = agent.report(True, {"returncode": 0}, task_complete_signal=sig)
    assert out["task_complete_signal"] == sig
    assert out["success"] is True


def test_report_omits_task_complete_signal_when_none():
    out = agent.report(False, {"returncode": 1})
    assert "task_complete_signal" not in out
    assert out["success"] is False


# ============= task_log 持久化 task_complete_signal =============

def test_task_log_persists_task_complete_signal(tmp_path):
    """finish_task_log 收到 signal 时应持久化到日志文件"""
    import config, task_log
    config.set_workspace_dir(str(tmp_path))
    task_log._reinit_paths()

    task_log.init_task_log("test req", "code")
    sig = {"early_exit": True, "success": True, "summary": "完成 multiply"}
    task_log.finish_task_log(True, 0, {"returncode": 0, "stdout": "", "stderr": ""},
                             task_complete_signal=sig)

    # 读最新日志文件
    logs = sorted(task_log._LOG_DIR.glob("*.jsonl"))
    assert logs, "应至少写入一个日志文件"
    import json as _json
    entry = _json.loads(logs[-1].read_text(encoding="utf-8"))
    assert entry.get("task_complete_signal") == {
        "early_exit": True, "success": True, "summary": "完成 multiply",
    }


def test_task_log_omits_signal_when_none(tmp_path):
    """没 signal 时日志不应有 task_complete_signal 字段"""
    import config, task_log
    config.set_workspace_dir(str(tmp_path))
    task_log._reinit_paths()

    task_log.init_task_log("test req", "code")
    task_log.finish_task_log(True, 0, {"returncode": 0, "stdout": "", "stderr": ""})

    logs = sorted(task_log._LOG_DIR.glob("*.jsonl"))
    assert logs
    import json as _json
    entry = _json.loads(logs[-1].read_text(encoding="utf-8"))
    assert "task_complete_signal" not in entry


def test_task_log_truncates_long_summary(tmp_path):
    """summary 超 500 字符应截断，避免日志膨胀"""
    import config, task_log
    config.set_workspace_dir(str(tmp_path))
    task_log._reinit_paths()

    task_log.init_task_log("test req", "code")
    long_summary = "x" * 1000
    sig = {"early_exit": True, "success": False, "summary": long_summary}
    task_log.finish_task_log(False, 1, {"returncode": 1, "stdout": "", "stderr": "boom"},
                             task_complete_signal=sig)

    logs = sorted(task_log._LOG_DIR.glob("*.jsonl"))
    import json as _json
    entry = _json.loads(logs[-1].read_text(encoding="utf-8"))
    assert len(entry["task_complete_signal"]["summary"]) == 500


def test_audit_returns_signal_on_task_complete(tmp_path):
    """audit() 收到 task_complete sentinel 时返回值应包含 task_complete_signal"""
    import config, tools
    config.set_workspace_dir(str(tmp_path))
    tools._reinit_paths()

    orig = agent.call_llm
    try:
        call_count = {"n": 0}
        def mock_llm(msgs, **kw):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return _make_response(
                    content="审计报告内容",
                    tool_calls=[("a1", "task_complete",
                                 {"success": True, "summary": "审计完成：3 处问题"})],
                )
            return _make_response(content="", tool_calls=None)

        agent.call_llm = mock_llm
        result = agent.audit("审计 calc.py")
    finally:
        agent.call_llm = orig

    assert result["success"] is True
    sig = result.get("task_complete_signal")
    assert sig is not None
    assert sig["early_exit"] is True
    assert sig["success"] is True
    assert "3 处" in sig["summary"]


# =====================================================================
# P2 #4-B1：_estimate_messages_tokens helper
# =====================================================================

def test_estimate_messages_tokens_empty():
    """空 messages → 0"""
    assert agent._estimate_messages_tokens([]) == 0
    assert agent._estimate_messages_tokens(None) == 0


def test_estimate_messages_tokens_basic_accuracy():
    """估算误差 < 30%（cc 文档：4 chars ≈ 1 token，对 ASCII/中英混排都准）"""
    # 构造 ~4000 字符的英文 message → 期望 ~1000 tokens
    body = "hello world " * 333  # 333 * 12 = 3996 chars
    messages = [{"role": "user", "content": body}]
    est = agent._estimate_messages_tokens(messages)
    # JSON 序列化会多出 role/content/引号等约 30 chars overhead
    # 总 chars ~ 4030，估算 ~ 1007 tokens
    assert 900 < est < 1100, f"est={est}, expected ~1000 ±30%"


def test_estimate_messages_tokens_multi_message_additive():
    """多条 message 累加估算"""
    msg = {"role": "user", "content": "x" * 1000}
    one = agent._estimate_messages_tokens([msg])
    three = agent._estimate_messages_tokens([msg, msg, msg])
    # 3 条应约等于 1 条的 3 倍（允许 list 括号/逗号小开销）
    assert 2.8 * one < three < 3.2 * one


def test_estimate_messages_tokens_handles_non_serializable():
    """无法 json 序列化的字段（如 MagicMock）走 default=str 兜底，不抛"""
    from unittest.mock import MagicMock
    messages = [{"role": "user", "content": "ok"}, {"role": "assistant", "obj": MagicMock()}]
    est = agent._estimate_messages_tokens(messages)
    assert est > 0


def test_estimate_messages_tokens_tool_call_message():
    """tool_calls / tool_result 风格 message 也能估算"""
    messages = [
        {"role": "system", "content": "you are a coder"},
        {"role": "user", "content": "改 x.py"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "read_file", "arguments": '{"filename":"x.py"}'}}
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "x = 1\n" * 500},
    ]
    est = agent._estimate_messages_tokens(messages)
    # tool result body ~3000 chars → ~750 tokens 起，加上其他约 800-900
    assert 700 < est < 1000


# =====================================================================
# P2 #4-B2：_compact_messages
# =====================================================================

def _make_assistant_msg(text="ok", tool_calls=None):
    """构造 assistant message dict（模拟 SDK message 序列化后形态）"""
    m = {"role": "assistant", "content": text}
    if tool_calls:
        m["tool_calls"] = [
            {"id": cid, "type": "function",
             "function": {"name": name, "arguments": '{"x":1}'}}
            for cid, name in tool_calls
        ]
    return m


def _make_tool_msg(call_id, content="result"):
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def test_compact_returns_original_when_too_few_messages():
    """msgs < 4 → 不压"""
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    assert agent._compact_messages(msgs, keep_recent_pairs=2) == msgs


def test_compact_returns_original_when_pairs_below_keep():
    """pair 数 <= keep_recent_pairs → 不压"""
    msgs = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u_initial"},
        _make_assistant_msg("a1", [("c1", "read_file")]),
        _make_tool_msg("c1", "file body"),
    ]
    # 1 个 pair，keep=2 → 不压
    assert agent._compact_messages(msgs, keep_recent_pairs=2) == msgs


def test_compact_preserves_head_and_recent_and_tool_pairing(monkeypatch):
    """关键测：head 保留，recent N pairs 保留，tool_call/tool_result 配对不破"""
    # 构造 5 个 pair，keep=2 → 应压缩前 3 个
    msgs = [
        {"role": "system", "content": "system_prompt"},
        {"role": "user", "content": "user_initial"},
        # pair 1
        _make_assistant_msg("a1", [("c1", "read_file")]),
        _make_tool_msg("c1", "old1"),
        # pair 2
        _make_assistant_msg("a2", [("c2", "grep")]),
        _make_tool_msg("c2", "old2"),
        # pair 3
        _make_assistant_msg("a3", [("c3", "read_file")]),
        _make_tool_msg("c3", "old3"),
        # pair 4 (recent)
        _make_assistant_msg("a4", [("c4", "replace_in_file")]),
        _make_tool_msg("c4", "recent4"),
        # pair 5 (recent)
        _make_assistant_msg("a5", [("c5", "test_runner")]),
        _make_tool_msg("c5", "recent5"),
    ]

    captured = {}
    def fake_call_llm(messages, **kw):
        captured["summarize_msgs"] = messages
        resp = MagicMock()
        m = MagicMock()
        m.content = "[摘要] 调过 read/grep，读了 X.py，未改动"
        resp.choices = [MagicMock(message=m)]
        return resp

    monkeypatch.setattr(agent, "call_llm", fake_call_llm)

    out = agent._compact_messages(msgs, keep_recent_pairs=2)

    # 头部保留
    assert out[0]["role"] == "system" and out[0]["content"] == "system_prompt"
    assert out[1]["role"] == "user" and out[1]["content"] == "user_initial"
    # 第 3 条是摘要 system message
    assert out[2]["role"] == "system"
    assert "历史摘要" in out[2]["content"]
    assert "摘要" in out[2]["content"]
    # 后续 4 条是 recent 2 个 pair（assistant + tool）
    assert len(out) == 2 + 1 + 4
    assert out[3]["content"] == "a4"
    assert out[4]["tool_call_id"] == "c4"
    assert out[5]["content"] == "a5"
    assert out[6]["tool_call_id"] == "c5"
    # 旧 pair 内容不应在新 msgs 里出现
    serialized = str(out)
    assert "old1" not in serialized
    assert "old2" not in serialized
    assert "old3" not in serialized
    # summarize 调用真发生了
    assert "summarize_msgs" in captured
    # summarize prompt 含旧 pair 内容
    summary_user = next(m for m in captured["summarize_msgs"] if m["role"] == "user")
    assert "old1" in summary_user["content"]
    assert "old3" in summary_user["content"]


def test_compact_total_size_decreases(monkeypatch):
    """compact 后总 token 估算应显著下降"""
    big = "x" * 5000
    msgs = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
    ]
    # 加 6 个 pair，每个含 5KB 内容
    for i in range(6):
        msgs.append(_make_assistant_msg(f"a{i}", [(f"c{i}", "read_file")]))
        msgs.append(_make_tool_msg(f"c{i}", big))

    monkeypatch.setattr(agent, "call_llm", lambda m, **kw: SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="短摘要"))]
    ))

    before = agent._estimate_messages_tokens(msgs)
    out = agent._compact_messages(msgs, keep_recent_pairs=2)
    after = agent._estimate_messages_tokens(out)
    assert after < before * 0.5, f"compact 后 ({after}) 应 < 压缩前 ({before}) 的 50%"


def test_compact_empty_summary_falls_back(monkeypatch):
    """summarize 返回空 → 不压（避免丢历史）"""
    msgs = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        _make_assistant_msg("a1", [("c1", "read_file")]),
        _make_tool_msg("c1", "x"),
        _make_assistant_msg("a2", [("c2", "grep")]),
        _make_tool_msg("c2", "y"),
        _make_assistant_msg("a3", [("c3", "edit")]),
        _make_tool_msg("c3", "z"),
    ]
    monkeypatch.setattr(agent, "call_llm", lambda m, **kw: SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=""))]
    ))
    out = agent._compact_messages(msgs, keep_recent_pairs=2)
    assert out == msgs


def test_compact_summarize_exception_falls_back(monkeypatch):
    """summarize 抛异常 → 不压"""
    msgs = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        _make_assistant_msg("a1", [("c1", "read_file")]),
        _make_tool_msg("c1", "x"),
        _make_assistant_msg("a2", [("c2", "grep")]),
        _make_tool_msg("c2", "y"),
        _make_assistant_msg("a3", [("c3", "edit")]),
        _make_tool_msg("c3", "z"),
    ]
    def boom(m, **kw):
        raise RuntimeError("LLM down")
    monkeypatch.setattr(agent, "call_llm", boom)
    out = agent._compact_messages(msgs, keep_recent_pairs=2)
    assert out == msgs


def test_split_messages_into_pairs_basic():
    """切分核心逻辑：按 assistant 边界切，tool message 紧跟 assistant"""
    rest = [
        _make_assistant_msg("a1", [("c1", "read_file")]),
        _make_tool_msg("c1", "r1"),
        _make_assistant_msg("a2", [("c2", "grep")]),
        _make_tool_msg("c2", "r2a"),
        _make_tool_msg("c2", "r2b"),  # 假设有多个 tool result
        _make_assistant_msg("a3"),  # 无 tool_calls
    ]
    pairs = agent._split_messages_into_pairs(rest)
    assert len(pairs) == 3
    assert len(pairs[0]) == 2  # a1 + c1
    assert len(pairs[1]) == 3  # a2 + c2 + c2
    assert len(pairs[2]) == 1  # a3


def test_split_messages_into_pairs_leading_user():
    """rest 起始有 user（无 assistant 在前）→ 单独成 pair"""
    rest = [
        {"role": "user", "content": "follow up"},
        _make_assistant_msg("a1", [("c1", "read")]),
        _make_tool_msg("c1", "r"),
    ]
    pairs = agent._split_messages_into_pairs(rest)
    assert len(pairs) == 2
    assert pairs[0][0]["role"] == "user"
    assert pairs[1][0]["role"] == "assistant"


def test_compact_rejects_zero_keep():
    """review m3: keep_recent_pairs <= 0 直接 ValueError"""
    import pytest as _pt
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    with _pt.raises(ValueError):
        agent._compact_messages(msgs, keep_recent_pairs=0)
    with _pt.raises(ValueError):
        agent._compact_messages(msgs, keep_recent_pairs=-1)


def test_compact_pulls_back_when_recent_starts_with_tool(monkeypatch):
    """review M1: recent_pairs 起始若是 tool（孤立 tool message 单独成 pair），
    应回拉一个 old_pair 进 recent，避免 [summary, tool_result, ...] 违反 OpenAI 协议"""
    # 构造异常切分形态：用一个能被 _split_messages_into_pairs 切出 leading tool 的 rest
    # （实际场景罕见，但防御编程要求）
    msgs = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        # 第一个 pair：assistant + tool（正常）
        _make_assistant_msg("a1", [("c1", "read")]),
        _make_tool_msg("c1", "x"),
        # 第二个 pair：assistant + tool
        _make_assistant_msg("a2", [("c2", "grep")]),
        _make_tool_msg("c2", "y"),
        # 第三个 pair：assistant + tool
        _make_assistant_msg("a3", [("c3", "read")]),
        _make_tool_msg("c3", "z"),
    ]
    monkeypatch.setattr(agent, "call_llm", lambda m, **kw: SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="摘要"))]
    ))
    out = agent._compact_messages(msgs, keep_recent_pairs=2)
    # recent 第一条必须是 assistant 或 user，不能是 tool
    # head=2, summary=1, recent 起始就在 index 3
    assert agent._msg_role(out[3]) in ("assistant", "user"), \
        f"recent[0] role={agent._msg_role(out[3])}, 应为 assistant/user"


def test_estimate_under_threshold_no_compact():
    """msgs 不到阈值时调用方不应触发 compact（接口契约：调用方负责检测）

    本测验证 _compact_messages 即便被低阈值调用也行为正确：
    pair 数 <= keep → 返回原 msgs 不调用 LLM
    """
    msgs = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        _make_assistant_msg("a1", [("c1", "read")]),
        _make_tool_msg("c1", "r"),
    ]
    # 不 monkeypatch call_llm，故若错误调用 LLM 会真发请求；测试应该不调
    out = agent._compact_messages(msgs, keep_recent_pairs=2)
    assert out == msgs


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
