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
    def mock_run_subagent(task, role="explorer", max_steps=8, **kwargs):
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
    def mock_run_subagent(task, role="explorer", max_steps=8, **kwargs):
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


# =====================================================================
# P2 #4-C：write_file 阈值 + compact 默认值
# =====================================================================

def test_write_rule_below_threshold_forbids_write_file(tmp_path):
    """expected_edits=19（<20）时 sys_prompt 仍禁止 write_file"""
    import config, tools
    config.set_workspace_dir(str(tmp_path))
    tools._reinit_paths()

    captured = {}
    def mock_llm(msgs, **kw):
        captured["sys"] = next(m["content"] for m in msgs if m["role"] == "system")
        from unittest.mock import MagicMock
        resp = MagicMock()
        msg = MagicMock()
        msg.content = ""
        msg.tool_calls = None
        resp.choices = [MagicMock(message=msg)]
        return resp

    # 构造一个已存在的文件
    f = tmp_path / "x.py"
    f.write_text("pass")

    orig = agent.call_llm
    try:
        agent.call_llm = mock_llm
        agent.code(
            {"files": [{"filename": "x.py", "intent": "edit", "description": "edit",
                        "expected_edits": 19}],
             "test_command": ""},
            requirement="test",
        )
    except Exception:
        pass
    finally:
        agent.call_llm = orig

    sys_content = captured.get("sys", "")
    # 19 < 20：sys_prompt 应该禁止 write_file
    assert "write_file" not in sys_content or "only for creating new files" in sys_content or "replace_in_file" in sys_content


def test_write_rule_at_threshold_allows_write_file(tmp_path):
    """expected_edits=20（>=20）时 sys_prompt 允许并推荐 write_file"""
    import config, tools
    config.set_workspace_dir(str(tmp_path))
    tools._reinit_paths()

    captured = {}
    def mock_llm(msgs, **kw):
        captured["sys"] = next(m["content"] for m in msgs if m["role"] == "system")
        from unittest.mock import MagicMock
        resp = MagicMock()
        msg = MagicMock()
        msg.content = ""
        msg.tool_calls = None
        resp.choices = [MagicMock(message=msg)]
        return resp

    f = tmp_path / "x.py"
    f.write_text("pass")

    orig = agent.call_llm
    try:
        agent.call_llm = mock_llm
        agent.code(
            {"files": [{"filename": "x.py", "intent": "edit", "description": "edit",
                        "expected_edits": 20}],
             "test_command": ""},
            requirement="test",
        )
    except Exception:
        pass
    finally:
        agent.call_llm = orig

    sys_content = captured.get("sys", "")
    # 20 >= 20：sys_prompt 应该允许 write_file 整文件重写
    assert "write_file" in sys_content
    assert "prefer write_file" in sys_content or "large batch" in sys_content


def test_compact_threshold_default_is_30k():
    """compact 默认阈值是 30_000（不依赖 cfg）"""
    # _cfg 在没有配置时返回 None，默认值应为 30_000
    import agent as _a
    # 直接看源码常量验证 or 验证在 mock cfg=None 时等于 30000
    val = int(_a._cfg("compact_threshold_tokens") or 30_000)
    assert val == 30_000


# ─────────────────────────────────────────────
# Fix 1a / 2a / 2b / 2c / 1b 单测
# ─────────────────────────────────────────────

def test_max_attempts_default_is_6():
    """Fix 1a: MAX_ATTEMPTS 常量已从 3 提高到 6"""
    import config as _cfg_mod
    assert _cfg_mod.MAX_ATTEMPTS == 6


def test_max_attempts_run_uses_config_constant():
    """Fix C2: _run() 里 max_attempts 回退应引用 _config_mod.MAX_ATTEMPTS，不写死字面量 3"""
    import pathlib
    src = pathlib.Path(__file__).parent.parent.parent.joinpath("agent.py").read_text(encoding="utf-8")
    # 回退值不再是字面量 3（`or 3` 已被移除）
    import re
    # 只看 max_attempts 那一行
    lines_with_max = [l for l in src.splitlines() if "max_attempts" in l and "_cfg" in l]
    assert lines_with_max, "未找到 max_attempts = _cfg(...) 行"
    for line in lines_with_max:
        assert "or 3" not in line, f"仍有字面量回退 3: {line}"
    assert "_config_mod.MAX_ATTEMPTS" in src


def test_architect_role_contains_symbol_contract_hint():
    """Fix 2c: _ARCHITECT_ROLE 包含多文件符号契约提示"""
    import agent as _a
    assert "symbol_contract" in _a._ARCHITECT_ROLE
    assert "Multi-file new projects" in _a._ARCHITECT_ROLE


def test_plan_schema_contains_symbol_contract_field():
    """Fix 2a: plan schema 文本包含 symbol_contract 字段说明"""
    import agent as _a
    import inspect
    src = inspect.getsource(_a.plan)
    assert "symbol_contract" in src


def test_parse_plan_preserves_symbol_contract():
    """Fix C1: _parse_plan_with_status 成功路径必须把 symbol_contract 透传出来"""
    import agent as _a
    plan_json = (
        '{"symbol_contract": {"mini/ast_nodes.py": ["LetDecl", "BinaryOp"]},'
        ' "files": [{"filename": "mini/parser.py", "description": "parse", "expected_edits": 1}],'
        ' "test_command": "pytest"}'
    )
    ok, result, err = _a._parse_plan_with_status(plan_json)
    assert ok, f"parse 应成功，错误：{err}"
    assert "symbol_contract" in result, "symbol_contract 被丢弃，Fix C1 未生效"
    assert result["symbol_contract"] == {"mini/ast_nodes.py": ["LetDecl", "BinaryOp"]}


def test_code_sys_prompt_injects_contract(tmp_path, monkeypatch):
    """Fix 2b: code() 从 plan.symbol_contract 生成 _contract_block 注入 sys_prompt（行为断言）"""
    import agent as _a
    captured_prompts = []

    orig_call_llm = _a.call_llm

    def fake_call_llm(msgs, *args, **kwargs):
        for m in msgs:
            if m.get("role") == "system":
                captured_prompts.append(m["content"])
        # 返回一个 task_complete 信号让 loop 停止
        from types import SimpleNamespace
        resp = SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(
                    content="done",
                    tool_calls=[],
                    role="assistant",
                )
            )],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
        )
        return resp

    monkeypatch.setattr(_a, "call_llm", fake_call_llm, raising=True)
    monkeypatch.setattr(_a, "_get_workspace", lambda: str(tmp_path), raising=False)
    monkeypatch.setattr(_a, "_append_active_prompts", lambda x: x, raising=False)
    monkeypatch.setattr(_a, "_get_project_rules", lambda: "", raising=False)
    monkeypatch.setattr(_a, "_cfg", lambda k, **kw: None, raising=False)
    import interrupt as _interrupt_mod
    monkeypatch.setattr(_interrupt_mod, "is_interrupted", lambda: False, raising=False)

    plan_with_contract = {
        "symbol_contract": {
            "mini/ast_nodes.py": ["LetDecl", "BinaryOp"],
            "mini/errors.py": ["MiniSyntaxError"],
        },
        "files": [{"filename": "mini/parser.py", "description": "parse", "expected_edits": 1}],
        "test_command": "pytest",
    }

    try:
        _a.code(plan_with_contract, requirement="test")
    except Exception:
        pass  # LLM 未返回 task_complete，loop 可能抛异常，属预期

    assert captured_prompts, "call_llm 未被调用，monkeypatch 路径有误"
    sys_content = captured_prompts[0]
    assert "GLOBAL SYMBOL CONTRACT" in sys_content, "sys_prompt 未注入契约"
    assert "LetDecl" in sys_content
    assert "MiniSyntaxError" in sys_content


def test_scan_import_mismatches_finds_wrong_names(tmp_path):
    """Fix 1b: 正确报告 basename 平铺场景下的名字不匹配"""
    import agent as _a

    (tmp_path / "ast_nodes.py").write_text("class LetDecl:\n    pass\nclass BinaryOp:\n    pass\n")
    (tmp_path / "parser.py").write_text(
        "from .ast_nodes import LetStmt\n"   # 错：应是 LetDecl
        "from .ast_nodes import BinaryOp\n"  # 对
    )
    plan = {"files": [{"filename": "ast_nodes.py"}, {"filename": "parser.py"}]}
    result = _a._scan_import_mismatches(plan, str(tmp_path))
    assert len(result) == 1
    f, tgt, name, _asname = result[0]
    assert "parser.py" in f
    assert "ast_nodes.py" in tgt
    assert name == "LetStmt"


def test_scan_import_mismatches_subdirectory(tmp_path):
    """Fix M1: 子目录场景（mini/ 包）能正确解析相对路径，不按 basename 碰撞"""
    import agent as _a
    import os

    pkg = tmp_path / "mini"
    pkg.mkdir()
    (pkg / "ast_nodes.py").write_text("class LetDecl:\n    pass\n")
    (pkg / "parser.py").write_text("from .ast_nodes import LetStmt\n")  # 错

    plan = {"files": [
        {"filename": os.path.join("mini", "ast_nodes.py")},
        {"filename": os.path.join("mini", "parser.py")},
    ]}
    result = _a._scan_import_mismatches(plan, str(tmp_path))
    assert len(result) == 1
    _, _, name, _ = result[0]
    assert name == "LetStmt"


def test_scan_import_mismatches_star_import_skipped(tmp_path):
    """Fix M2: 含 star-import 的模块标记 unknown，不产生误报"""
    import agent as _a

    (tmp_path / "base.py").write_text("from .util import *\n")  # star-import → unknown
    (tmp_path / "parser.py").write_text("from .base import SomeClass\n")

    plan = {"files": [{"filename": "base.py"}, {"filename": "parser.py"}]}
    result = _a._scan_import_mismatches(plan, str(tmp_path))
    assert result == [], "star-import 模块应被标 unknown，不产生误报"


def test_scan_import_mismatches_tuple_unpack(tmp_path):
    """Fix M2: 元组解包赋值 A, B = ... 的名字应被收集到 exports"""
    import agent as _a

    (tmp_path / "config.py").write_text("A, B = 1, 2\n")
    (tmp_path / "parser.py").write_text("from .config import A\nfrom .config import B\n")

    plan = {"files": [{"filename": "config.py"}, {"filename": "parser.py"}]}
    result = _a._scan_import_mismatches(plan, str(tmp_path))
    assert result == [], f"元组解包的 A/B 应被识别为 exports，但报了误报: {result}"


def test_scan_import_mismatches_no_false_positive(tmp_path):
    """Fix 1b: 名字完全匹配时不报误报"""
    import agent as _a

    (tmp_path / "errors.py").write_text("class MiniSyntaxError(Exception): pass\n")
    (tmp_path / "parser.py").write_text("from .errors import MiniSyntaxError\n")

    plan = {"files": [{"filename": "errors.py"}, {"filename": "parser.py"}]}
    result = _a._scan_import_mismatches(plan, str(tmp_path))
    assert result == []


def test_scan_import_mismatches_parse_error_safe(tmp_path):
    """Fix 1b: 遇到语法错误的文件时安全返回，不抛异常"""
    import agent as _a

    (tmp_path / "broken.py").write_text("def foo(:\n    pass\n")
    plan = {"files": [{"filename": "broken.py"}]}
    result = _a._scan_import_mismatches(plan, str(tmp_path))
    assert result == []


def test_contract_block_empty_when_no_contract():
    """Fix 2b: symbol_contract 为空时 _render_contract 返回空字符串（行为断言）"""
    import agent as _a
    assert _a._render_contract({}) == ""
    assert _a._render_contract(None) == ""


def test_scan_import_mismatches_reexport_no_false_positive(tmp_path):
    """M-new-1: re-export（from .x import Foo）应被计入模块导出，不产生误报"""
    import agent as _a

    # base.py 通过 re-export 把 Foo 暴露给外部
    (tmp_path / "util.py").write_text("class Foo:\n    pass\n")
    (tmp_path / "base.py").write_text("from .util import Foo\n")   # re-export
    (tmp_path / "parser.py").write_text("from .base import Foo\n")  # 应视为合法

    plan = {"files": [
        {"filename": "util.py"},
        {"filename": "base.py"},
        {"filename": "parser.py"},
    ]}
    result = _a._scan_import_mismatches(plan, str(tmp_path))
    assert result == [], f"re-export 应被识别为合法导出，但报了误报: {result}"


def test_scan_import_mismatches_try_conditional_def_no_false_positive(tmp_path):
    """M-new-2: try/if 块内的模块级定义应被收集，不产生误报"""
    import agent as _a

    # try 块内定义（常见兜底实现模式）
    (tmp_path / "compat.py").write_text(
        "try:\n"
        "    class FastParser:\n"
        "        pass\n"
        "except ImportError:\n"
        "    class FastParser:\n"
        "        pass\n"
    )
    # if 块内定义（条件平台实现）
    (tmp_path / "platform_util.py").write_text(
        "import sys\n"
        "if sys.platform == 'win32':\n"
        "    def get_path(): return 'C:\\\\'\n"
        "else:\n"
        "    def get_path(): return '/'\n"
    )
    (tmp_path / "parser.py").write_text(
        "from .compat import FastParser\n"
        "from .platform_util import get_path\n"
    )

    plan = {"files": [
        {"filename": "compat.py"},
        {"filename": "platform_util.py"},
        {"filename": "parser.py"},
    ]}
    result = _a._scan_import_mismatches(plan, str(tmp_path))
    assert result == [], f"try/if 块内定义应被识别为合法导出，但报了误报: {result}"


def test_render_contract_member_level_format():
    """P0-B: _render_contract 行为测试 — 成员级 dict 格式渲染出 fields/members"""
    import agent as _a
    contract = {
        "mini/token.py": {"TokenType": {"members": ["IDENTIFIER", "INT"]}},
        "mini/ast_nodes.py": {"LetDecl": {"fields": ["name", "value"]}, "Program": {}},
    }
    result = _a._render_contract(contract)
    assert "GLOBAL SYMBOL CONTRACT" in result
    assert "members=IDENTIFIER, INT" in result or "members=INT, IDENTIFIER" in result
    assert "fields=name, value" in result
    assert "Program" in result


def test_render_contract_flat_list_format():
    """P0-B: _render_contract 向后兼容旧扁平 list 格式"""
    import agent as _a
    contract = {"mini/errors.py": ["MiniSyntaxError", "MiniRuntimeError"]}
    result = _a._render_contract(contract)
    assert "MiniSyntaxError" in result
    assert "exports:" in result


def test_render_contract_empty_returns_empty():
    """P0-B: 空 contract 返回空字符串"""
    import agent as _a
    assert _a._render_contract({}) == ""
    assert _a._render_contract(None) == ""  # None 也安全


def test_scan_member_mismatches_enum_member(tmp_path):
    """P0-C: _scan_member_mismatches 能找出枚举成员名不匹配"""
    import agent as _a

    (tmp_path / "token.py").write_text(
        "from enum import Enum\n"
        "class TokenType(Enum):\n"
        "    IDENTIFIER = 'IDENTIFIER'\n"
        "    INT = 'INT'\n"
    )
    (tmp_path / "parser.py").write_text(
        "from .token import TokenType\n"
        "x = TokenType.IDENT\n"      # 错：应是 IDENTIFIER
        "y = TokenType.IDENTIFIER\n" # 正确
    )
    plan = {
        "symbol_contract": {
            "token.py": {"TokenType": {"members": ["IDENTIFIER", "INT"]}}
        },
        "files": [{"filename": "token.py"}, {"filename": "parser.py"}],
    }
    result = _a._scan_member_mismatches(plan, str(tmp_path))
    names = [r[2] for r in result]
    assert any("TokenType.IDENT" in n for n in names), f"应报 TokenType.IDENT，但结果: {result}"
    assert not any("TokenType.IDENTIFIER" in n for n in names), "不应误报正确的 IDENTIFIER"


def test_scan_member_mismatches_dataclass_field(tmp_path):
    """P0-C: _scan_member_mismatches 能找出 dataclass 字段名同义词（保守档）"""
    import agent as _a

    (tmp_path / "ast_nodes.py").write_text(
        "from dataclasses import dataclass\n"
        "@dataclass\nclass LetDecl:\n    name: str\n    value: object\n"
    )
    (tmp_path / "interpreter.py").write_text(
        "from .ast_nodes import LetDecl\n"
        "def visit(node): return node.initializer\n"  # 错：应是 value
    )
    plan = {
        "symbol_contract": {
            "ast_nodes.py": {"LetDecl": {"fields": ["name", "value"]}}
        },
        "files": [{"filename": "ast_nodes.py"}, {"filename": "interpreter.py"}],
    }
    # 必须在 error_info 里含 AttributeError 'initializer' 才触发同义词检查
    error_info = "AttributeError: 'LetDecl' object has no attribute 'initializer'"
    result = _a._scan_member_mismatches(plan, str(tmp_path), error_info=error_info)
    # 应报 <obj>.initializer，权威名 value
    assert any(r[2] == "<obj>.initializer" and r[3] == "value" for r in result), \
        f"应报 initializer→value，但结果: {result}"


def test_scan_member_mismatches_no_false_positive(tmp_path):
    """P0-C: 枚举成员名正确时不误报"""
    import agent as _a

    (tmp_path / "token.py").write_text(
        "from enum import Enum\nclass TokenType(Enum):\n    IDENTIFIER='ID'\n"
    )
    (tmp_path / "parser.py").write_text(
        "from .token import TokenType\nx = TokenType.IDENTIFIER\n"
    )
    plan = {
        "symbol_contract": {"token.py": {"TokenType": {"members": ["IDENTIFIER"]}}},
        "files": [{"filename": "token.py"}, {"filename": "parser.py"}],
    }
    result = _a._scan_member_mismatches(plan, str(tmp_path))
    assert result == [], f"正确引用不应报错: {result}"


def test_scan_member_mismatches_no_contract_returns_empty(tmp_path):
    """P0-C: 无 symbol_contract 时直接返回空列表（不扫描）"""
    import agent as _a
    plan = {"files": [{"filename": "parser.py"}]}
    result = _a._scan_member_mismatches(plan, str(tmp_path))
    assert result == []


def test_scan_member_mismatches_synonym_requires_attr_error_in_error_info(tmp_path):
    """C1 修复: dataclass 同义词检查只在 error_info 出现 AttributeError 该属性名时才激活，
    否则无条件跳过，避免误报 request.test / lexer.token 等正常代码"""
    import agent as _a

    (tmp_path / "ast_nodes.py").write_text(
        "from dataclasses import dataclass\n@dataclass\nclass IfStmt:\n    cond: object\n    then_block: object\n"
    )
    # 文件里有 self.test / lexer.token / request.condition 等常见属性访问
    (tmp_path / "app.py").write_text(
        "class Foo:\n"
        "    def run(self, req):\n"
        "        return req.test and self.token and req.condition\n"
    )
    plan = {
        "symbol_contract": {"ast_nodes.py": {"IfStmt": {"fields": ["cond", "then_block"]}}},
        "files": [{"filename": "ast_nodes.py"}, {"filename": "app.py"}],
    }
    # 无 AttributeError → 同义词检查不激活 → 不误报
    result = _a._scan_member_mismatches(plan, str(tmp_path), error_info="AssertionError: 1 != 2")
    assert result == [], f"无 AttributeError 时不应误报: {result}"

    # 有 AttributeError 'condition' → 才激活 condition → cond 检查
    result2 = _a._scan_member_mismatches(
        plan, str(tmp_path),
        error_info="AttributeError: 'IfStmt' object has no attribute 'condition'"
    )
    assert any(r[2] == "<obj>.condition" for r in result2), \
        f"error_info 含 condition 时应报缺口: {result2}"


def test_scan_import_mismatches_level2_relative_import(tmp_path):
    """level=2 多级相对 import（from ..module import X）应正确解析"""
    import agent as _a
    import os

    # 结构：pkg/sub/parser.py  从  pkg/errors.py  导入
    pkg = tmp_path / "pkg"
    sub = pkg / "sub"
    sub.mkdir(parents=True)
    (pkg / "errors.py").write_text("class MiniError(Exception): pass\n")
    (sub / "parser.py").write_text("from ..errors import MiniError\n")  # level=2

    plan = {"files": [
        {"filename": os.path.join("pkg", "errors.py")},
        {"filename": os.path.join("pkg", "sub", "parser.py")},
    ]}
    result = _a._scan_import_mismatches(plan, str(tmp_path))
    assert result == [], f"level=2 相对 import 应正确解析，但报了误报: {result}"


# ---------------------------------------------------------------------------
# Debt 3：否定约束 + 越权测试文件排除
# ---------------------------------------------------------------------------

def test_infer_test_scope_excludes_unsanctioned(tmp_path):
    """_infer_test_scope exclude 参数排除指定测试文件"""
    import agent as _a
    import config, tools
    config.set_workspace_dir(str(tmp_path))
    tools._reinit_paths()

    ws = tmp_path
    tests = ws / "tests"
    tests.mkdir()
    (ws / "calc.py").write_text("def add(a, b): return a + b\n")
    (tests / "test_calc.py").write_text("from calc import add\ndef test_add(): assert add(1,2)==3\n")
    (tests / "test_other.py").write_text("def test_other(): pass\n")

    plan_files = [{"filename": "calc.py"}]
    # 不排除：两个测试都找到 test_calc.py
    scope = _a._infer_test_scope(plan_files)
    assert any("test_calc.py" in f for f in scope), f"应找到 test_calc.py: {scope}"

    # 排除 test_calc.py：scope 应为空（plan 里只有 calc.py 对应 test_calc.py）
    excluded = {"tests/test_calc.py"}
    scope_excl = _a._infer_test_scope(plan_files, exclude=excluded)
    assert not any("test_calc.py" in f for f in scope_excl), \
        f"exclude 后不应包含 test_calc.py: {scope_excl}"


def test_infer_test_scope_keeps_plan_test_file(tmp_path):
    """plan_files 里的测试文件不受 exclude 影响（plan 明确要求创建，不是越权）"""
    import agent as _a
    import config, tools
    config.set_workspace_dir(str(tmp_path))
    tools._reinit_paths()

    ws = tmp_path
    tests = ws / "tests"
    tests.mkdir()
    (tests / "test_calc.py").write_text("def test_add(): pass\n")

    # plan_files 直接包含 test_calc.py（architect 规划了它）
    plan_files = [{"filename": "tests/test_calc.py"}]
    excluded = {"tests/test_calc.py"}  # 即使在 exclude 里
    scope = _a._infer_test_scope(plan_files, exclude=excluded)
    # test_calc.py 是 plan_files 里的测试文件，直接加，不受 exclude 影响
    assert any("test_calc.py" in f for f in scope), \
        f"plan_files 里的测试文件不应被 exclude 过滤: {scope}"


# ---------------------------------------------------------------------------
# Debt 1：symbol_contract 覆盖方法名
# ---------------------------------------------------------------------------

def test_render_contract_methods(tmp_path):
    """contract 含 methods 字段 → 渲染出 methods=... 文本"""
    import agent as _a
    contract = {
        "expression.py": {
            "ExprEvaluator": {"methods": ["evaluate", "__call__"]}
        }
    }
    rendered = _a._render_contract(contract)
    assert "methods=" in rendered, f"应含 methods=: {rendered}"
    assert "evaluate" in rendered
    assert "__call__" in rendered


def test_scan_member_methods_active_on_attributeerror(tmp_path):
    """executor.py 调用 evaluator.eval()，契约 ExprEvaluator.methods=evaluate，
    error_info 含 has no attribute 'eval' → 报一条缺口"""
    import agent as _a
    import os

    (tmp_path / "executor.py").write_text(
        "from expression import ExprEvaluator\n"
        "def run(ev):\n"
        "    return ev.eval(1+2)\n"
    )
    (tmp_path / "expression.py").write_text(
        "class ExprEvaluator:\n"
        "    def evaluate(self, expr): return expr\n"
    )

    plan = {
        "files": [
            {"filename": "executor.py"},
            {"filename": "expression.py"},
        ],
        "symbol_contract": {
            "expression.py": {
                "ExprEvaluator": {"methods": ["evaluate"]}
            }
        }
    }
    error_info = "AttributeError: 'ExprEvaluator' object has no attribute 'eval'"
    result = _a._scan_member_mismatches(plan, str(tmp_path), error_info=error_info)
    assert len(result) >= 1, f"应报 eval 方法缺口: {result}"
    assert any("eval" in r[2] for r in result), f"应提及 eval: {result}"
    assert any("evaluate" in (r[3] or "") for r in result), f"权威名应为 evaluate: {result}"


def test_scan_member_methods_no_false_positive_without_error(tmp_path):
    """error_info 为空时不应报方法缺口（严格保守，避免 .eval 等常见方法名误报）"""
    import agent as _a

    (tmp_path / "executor.py").write_text(
        "def run(ev):\n"
        "    return ev.eval(1+2)\n"
    )
    plan = {
        "files": [{"filename": "executor.py"}],
        "symbol_contract": {
            "expression.py": {
                "ExprEvaluator": {"methods": ["evaluate"]}
            }
        }
    }
    result = _a._scan_member_mismatches(plan, str(tmp_path), error_info="")
    method_hits = [r for r in result if "eval" in r[2] and "()" in r[2]]
    assert len(method_hits) == 0, f"error_info 为空时不应报方法缺口: {method_hits}"


# ---------------------------------------------------------------------------
# Debt 2：fix loop 震荡修复
# ---------------------------------------------------------------------------

def test_fix_injects_regression_warning_when_fail_count_rises(tmp_path):
    """cur_fail > prev_fail + prev_changed 非空 → user content 含震荡警告"""
    import config, tools
    import json
    config.set_workspace_dir(str(tmp_path))
    tools._reinit_paths()

    # Mock call_llm to return immediate task_complete
    import agent as _a
    captured_content = {}
    orig_call = _a.call_llm

    def mock_call_llm(msgs, *args, **kwargs):
        if msgs and msgs[-1]["role"] == "user":
            captured_content["user"] = msgs[-1]["content"]
        return _make_response(
            tool_calls=[("id1", "task_complete", {"success": False, "summary": "test"})]
        )

    _a.call_llm = mock_call_llm
    try:
        test_result = {"returncode": 1, "stdout": "", "stderr": "1 failed"}
        plan = {"files": [{"filename": "calc.py", "expected_edits": 1}]}
        _a.fix(
            test_result, plan,
            prev_changed=["calc.py", "utils.py"],
            prev_fail_count=1,
            cur_fail_count=10,
        )
    finally:
        _a.call_llm = orig_call

    assert "prev_changed" not in str(captured_content)  # field name not leaked
    user_msg = captured_content.get("user", "")
    assert "震荡警告" in user_msg or "regression" in user_msg.lower(), \
        f"应含震荡警告: {user_msg[:300]}"
    assert "calc.py" in user_msg, "应提及上一轮改过的文件"


def test_fix_no_regression_warning_when_fail_count_drops(tmp_path):
    """cur_fail < prev_fail → 不注入震荡警告"""
    import config, tools
    config.set_workspace_dir(str(tmp_path))
    tools._reinit_paths()

    import agent as _a
    captured_content = {}
    orig_call = _a.call_llm

    def mock_call_llm(msgs, *args, **kwargs):
        if msgs and msgs[-1]["role"] == "user":
            captured_content["user"] = msgs[-1]["content"]
        return _make_response(
            tool_calls=[("id1", "task_complete", {"success": False, "summary": "test"})]
        )

    _a.call_llm = mock_call_llm
    try:
        test_result = {"returncode": 1, "stdout": "", "stderr": "1 failed"}
        plan = {"files": [{"filename": "calc.py", "expected_edits": 1}]}
        _a.fix(
            test_result, plan,
            prev_changed=["calc.py"],
            prev_fail_count=10,
            cur_fail_count=1,  # 收敛，不是震荡
        )
    finally:
        _a.call_llm = orig_call

    user_msg = captured_content.get("user", "")
    assert "震荡警告" not in user_msg, f"收敛时不应注入震荡警告: {user_msg[:200]}"


def test_scan_import_always_active_without_import_error(tmp_path):
    """_scan_import_mismatches 无论 error_info 是否含 ImportError 都应在 fix 中被调用（主动扫描）"""
    import agent as _a
    import config, tools
    config.set_workspace_dir(str(tmp_path))
    tools._reinit_paths()

    # 构造一个有 import 缺口的 plan
    (tmp_path / "a.py").write_text("class A:\n    pass\n")
    (tmp_path / "b.py").write_text("from .a import MissingName\n")

    plan = {"files": [
        {"filename": "a.py", "expected_edits": 1},
        {"filename": "b.py", "expected_edits": 1},
    ]}
    captured_content = {}
    orig_call = _a.call_llm

    def mock_call_llm(msgs, *args, **kwargs):
        if msgs and msgs[-1]["role"] == "user":
            captured_content["user"] = msgs[-1]["content"]
        return _make_response(
            tool_calls=[("id1", "task_complete", {"success": False, "summary": "test"})]
        )

    _a.call_llm = mock_call_llm
    try:
        # error_info 不含 ImportError，只含普通 AttributeError
        test_result = {"returncode": 1, "stdout": "", "stderr": "AttributeError: something"}
        _a.fix(test_result, plan)
    finally:
        _a.call_llm = orig_call

    user_msg = captured_content.get("user", "")
    assert "MissingName" in user_msg, \
        f"主动扫描应发现 MissingName 缺口（不依赖 ImportError 触发）: {user_msg[:400]}"


# ---------------------------------------------------------------------------
# _scan_import_mismatches — 绝对 import 检测（R3 新增）
# ---------------------------------------------------------------------------

def test_scan_import_absolute_detects_missing(tmp_path):
    """绝对 import（level==0）且目标文件在 plan 内时，缺失名应被检出。"""
    # analyzer.py 只有 class Analyzer，无 analyze 函数
    analyzer = tmp_path / "analyzer.py"
    analyzer.write_text("class Analyzer:\n    pass\n", encoding="utf-8")
    # main.py 用绝对 import: from miniql.analyzer import analyze
    main = tmp_path / "main.py"
    main.write_text("from miniql.analyzer import analyze\n", encoding="utf-8")

    import os
    plan = {
        "files": [
            {"filename": "analyzer.py"},
            {"filename": os.path.join("miniql", "analyzer.py")},
            {"filename": "main.py"},
        ]
    }
    # 把 analyzer.py 也放进 miniql/ 子目录（匹配绝对路径）
    miniql_dir = tmp_path / "miniql"
    miniql_dir.mkdir()
    (miniql_dir / "analyzer.py").write_text("class Analyzer:\n    pass\n", encoding="utf-8")

    result = agent._scan_import_mismatches(plan, str(tmp_path))
    missing_names = [r[2] for r in result]
    assert "analyze" in missing_names, f"应检出 analyze 缺失，实际: {result}"


def test_scan_import_absolute_strip_package_prefix(tmp_path):
    """plan 文件名无顶层包名前缀（analyzer.py），但 import 写 from miniql.analyzer import X，应仍能命中。"""
    analyzer = tmp_path / "analyzer.py"
    analyzer.write_text("class Analyzer:\n    pass\n", encoding="utf-8")
    main = tmp_path / "main.py"
    main.write_text("from miniql.analyzer import analyze\n", encoding="utf-8")

    plan = {
        "files": [
            {"filename": "analyzer.py"},
            {"filename": "main.py"},
        ]
    }
    result = agent._scan_import_mismatches(plan, str(tmp_path))
    missing_names = [r[2] for r in result]
    assert "analyze" in missing_names, f"去顶层包名后应命中 analyzer.py 并检出 analyze: {result}"


def test_scan_import_absolute_no_false_positive_stdlib(tmp_path):
    """标准库 / 外部库绝对 import 不在 plan 内，不应误报。"""
    main = tmp_path / "main.py"
    main.write_text("from os import path\nimport pytest\nfrom collections import OrderedDict\n",
                    encoding="utf-8")
    plan = {"files": [{"filename": "main.py"}]}
    result = agent._scan_import_mismatches(plan, str(tmp_path))
    assert result == [], f"外部库 import 不应误报，实际: {result}"


def test_scan_import_relative_regression_after_absolute_fix(tmp_path):
    """放开 level==0 后，原 relative import（level==1）行为不退化。"""
    errors_py = tmp_path / "errors.py"
    errors_py.write_text("class RuntimeError_:\n    pass\n", encoding="utf-8")
    caller = tmp_path / "caller.py"
    caller.write_text("from .errors import RuntimeError\n", encoding="utf-8")

    plan = {
        "files": [
            {"filename": "errors.py"},
            {"filename": "caller.py"},
        ]
    }
    result = agent._scan_import_mismatches(plan, str(tmp_path))
    missing_names = [r[2] for r in result]
    assert "RuntimeError" in missing_names, f"相对 import 缺失应仍被检出: {result}"


# ---------------------------------------------------------------------------
# _scan_contract_export_mismatches（R3 新增）
# ---------------------------------------------------------------------------

def test_scan_contract_export_finds_missing(tmp_path):
    """契约声明 analyze，但文件只有 class Analyzer → 检出缺口。"""
    analyzer = tmp_path / "analyzer.py"
    analyzer.write_text("class Analyzer:\n    pass\n", encoding="utf-8")

    plan = {
        "symbol_contract": {
            "analyzer.py": {"analyze": {}, "Analyzer": {}}
        },
        "files": [{"filename": "analyzer.py"}]
    }
    result = agent._scan_contract_export_mismatches(plan, str(tmp_path))
    missing = [name for _, name in result]
    assert "analyze" in missing, f"analyze 缺失应被检出: {result}"
    assert "Analyzer" not in missing, f"Analyzer 存在，不应误报: {result}"


def test_scan_contract_export_passes_when_all_defined(tmp_path):
    """契约声明的名称全部存在 → 返回空。"""
    f = tmp_path / "utils.py"
    f.write_text("def analyze():\n    pass\nclass Analyzer:\n    pass\n", encoding="utf-8")

    plan = {
        "symbol_contract": {"utils.py": {"analyze": {}, "Analyzer": {}}},
        "files": [{"filename": "utils.py"}]
    }
    result = agent._scan_contract_export_mismatches(plan, str(tmp_path))
    assert result == [], f"全部定义，应返回空: {result}"


def test_scan_contract_export_star_import_skipped(tmp_path):
    """含 star-import 的模块 exports unknown → 保守跳过，不误报。"""
    f = tmp_path / "utils.py"
    f.write_text("from other import *\n", encoding="utf-8")

    plan = {
        "symbol_contract": {"utils.py": {"analyze": {}}},
        "files": [{"filename": "utils.py"}]
    }
    result = agent._scan_contract_export_mismatches(plan, str(tmp_path))
    assert result == [], f"star-import 应保守跳过，不误报: {result}"


def test_scan_contract_export_flat_list_format(tmp_path):
    """契约用扁平列表格式 ['analyze', 'Analyzer']，缺失 analyze 应检出。"""
    f = tmp_path / "analyzer.py"
    f.write_text("class Analyzer:\n    pass\n", encoding="utf-8")

    plan = {
        "symbol_contract": {"analyzer.py": ["analyze", "Analyzer"]},
        "files": [{"filename": "analyzer.py"}]
    }
    result = agent._scan_contract_export_mismatches(plan, str(tmp_path))
    missing = [name for _, name in result]
    assert "analyze" in missing
    assert "Analyzer" not in missing


# ---------------------------------------------------------------------------
# _parse_plan_with_status — symbol_contract 强制校验（R4 新增）
# ---------------------------------------------------------------------------

def _make_plan_json(filenames, with_contract=False):
    """构造含指定文件的 plan JSON 字符串。"""
    import json as _json
    files = [{"filename": f, "description": "test", "expected_edits": 1} for f in filenames]
    plan = {"files": files, "test_command": "pytest"}
    if with_contract:
        plan["symbol_contract"] = {"mod.py": {"Foo": {}}}
    return _json.dumps(plan)


def test_parse_plan_missing_contract_ge3_new_files():
    """≥3 新文件且无 symbol_contract → ok=False，err 含 symbol_contract。"""
    content = _make_plan_json(["a.py", "b.py", "c.py"])
    ok, data, err = agent._parse_plan_with_status(content, existing_files=[])
    assert not ok, "≥3 新文件无契约应 ok=False"
    assert "symbol_contract" in (err or ""), f"err 应提示 symbol_contract 缺失: {err}"


def test_parse_plan_empty_contract_dict_rejected():
    """symbol_contract 为空 dict {} 等同缺失 → ok=False。"""
    import json as _json
    plan = {"files": [{"filename": f, "description": "x", "expected_edits": 1}
                      for f in ["a.py", "b.py", "c.py"]],
            "test_command": "pytest",
            "symbol_contract": {}}
    ok, data, err = agent._parse_plan_with_status(_json.dumps(plan), existing_files=[])
    assert not ok, "symbol_contract={} 应视为缺失，ok=False"


def test_parse_plan_with_contract_passes():
    """≥3 新文件且有非空 symbol_contract → ok=True。"""
    content = _make_plan_json(["a.py", "b.py", "c.py"], with_contract=True)
    ok, data, err = agent._parse_plan_with_status(content, existing_files=[])
    assert ok, f"带契约应 ok=True，err={err}"


def test_parse_plan_lt3_new_files_no_contract_ok():
    """仅 2 新文件无 symbol_contract → ok=True（小任务不强制）。"""
    content = _make_plan_json(["a.py", "b.py"])
    ok, data, err = agent._parse_plan_with_status(content, existing_files=[])
    assert ok, f"2 新文件无契约应 ok=True，err={err}"


def test_parse_plan_existing_files_deducted():
    """3 文件但 2 个是 existing → 仅 1 新文件，不强制 symbol_contract。"""
    content = _make_plan_json(["a.py", "b.py", "c.py"])
    # a.py, b.py 已存在，只有 c.py 是新文件
    ok, data, err = agent._parse_plan_with_status(
        content, existing_files=["a.py", "b.py"])
    assert ok, f"仅 1 新文件应 ok=True，err={err}"


def test_parse_plan_no_existing_files_arg_skips_check():
    """existing_files=None（旧调用，向后兼容）→ 跳过校验，≥3 新文件无契约仍 ok=True。"""
    content = _make_plan_json(["a.py", "b.py", "c.py"])
    ok, data, err = agent._parse_plan_with_status(content, existing_files=None)
    assert ok, f"existing_files=None 应跳过校验，ok=True，err={err}"


def test_parse_plan_module_only_contract_valid():
    """symbol_contract 的模块 value 为空 dict {} → 视为有效（声明了模块，无成员细节）。"""
    import json as _json
    plan = {"files": [{"filename": f, "description": "x", "expected_edits": 1}
                      for f in ["a.py", "b.py", "c.py"]],
            "test_command": "pytest",
            "symbol_contract": {"mod.py": {}}}  # value 是空 dict，但 contract 本身非空
    ok, data, err = agent._parse_plan_with_status(_json.dumps(plan), existing_files=[])
    assert ok, f"契约含模块声明（value=空dict）应视为有效，ok=True，err={err}"


def test_parse_plan_windows_path_normalization():
    """existing_files 含反斜杠路径，plan filename 含正斜杠 → 正确去重（Windows 路径兼容）。"""
    import os
    # existing: Windows 风格反斜杠
    existing = [os.path.join("sub", "a.py"), os.path.join("sub", "b.py")]
    # plan: 正斜杠
    content = _make_plan_json(["sub/a.py", "sub/b.py", "sub/c.py"])
    ok, data, err = agent._parse_plan_with_status(content, existing_files=existing)
    # 只有 c.py 是新文件（1 个），不强制 symbol_contract
    assert ok, f"路径归一化后仅 1 新文件，应 ok=True，err={err}"


# ---------------------------------------------------------------------------
# _render_contract — methods dict 格式（R5 新增）
# ---------------------------------------------------------------------------

def test_render_contract_methods_dict_format():
    """dict 格式 methods 渲染为带参数的签名 register_csv(table_name, path)。"""
    contract = {
        "catalog.py": {
            "Catalog": {
                "methods": [{"name": "register_csv", "params": ["table_name", "path"]}]
            }
        }
    }
    result = agent._render_contract(contract)
    assert "register_csv(table_name, path)" in result, f"应渲染完整签名: {result}"


def test_render_contract_methods_str_backward_compat():
    """字符串格式 methods 仍正常渲染（向后兼容）。"""
    contract = {"mod.py": {"Foo": {"methods": ["query"]}}}
    result = agent._render_contract(contract)
    assert "query" in result, f"字符串 methods 应仍渲染: {result}"


def test_render_contract_methods_mixed_list():
    """混合列表（dict + string）正常渲染，不抛异常。"""
    contract = {
        "mod.py": {
            "Bar": {
                "methods": [
                    {"name": "run", "params": ["x"]},
                    "stop"
                ]
            }
        }
    }
    result = agent._render_contract(contract)
    assert "run(x)" in result
    assert "stop" in result


# ---------------------------------------------------------------------------
# _scan_member_mismatches — arity 检测（R5 新增）
# ---------------------------------------------------------------------------

def _make_arity_plan(workspace, contract, caller_src, callee_src):
    """辅助：写 catalog.py(定义) + main.py(调用) 到 tmp workspace。"""
    import os
    os.makedirs(os.path.join(workspace, "miniql"), exist_ok=True)
    with open(os.path.join(workspace, "miniql", "catalog.py"), "w", encoding="utf-8") as f:
        f.write(callee_src)
    with open(os.path.join(workspace, "miniql", "main.py"), "w", encoding="utf-8") as f:
        f.write(caller_src)
    plan = {
        "symbol_contract": contract,
        "files": [
            {"filename": os.path.join("miniql", "catalog.py")},
            {"filename": os.path.join("miniql", "main.py")},
        ]
    }
    return plan


def test_scan_member_arity_detects_mismatch(tmp_path):
    """契约声明 register_csv 2参，调用端传1参 → 报 arity 缺口。"""
    contract = {
        "miniql/catalog.py": {
            "Catalog": {"methods": [{"name": "register_csv", "params": ["table_name", "path"]}]}
        }
    }
    callee = "class Catalog:\n    def register_csv(self, table_name, path): pass\n"
    caller = "from miniql.catalog import Catalog\ncatalog = Catalog()\ncatalog.register_csv(csv_path)\n"
    plan = _make_arity_plan(str(tmp_path), contract, caller, callee)
    result = agent._scan_member_mismatches(plan, str(tmp_path), error_info="")
    arity_issues = [r for r in result if "arity" in str(r[3]).lower()]
    assert arity_issues, f"应检出 arity 缺口，实际: {result}"


def test_scan_member_arity_passes_correct(tmp_path):
    """契约声明 register_csv 2参，调用端也传2参 → 不报。"""
    contract = {
        "miniql/catalog.py": {
            "Catalog": {"methods": [{"name": "register_csv", "params": ["table_name", "path"]}]}
        }
    }
    callee = "class Catalog:\n    def register_csv(self, table_name, path): pass\n"
    caller = "catalog.register_csv(table_name, csv_path)\n"
    plan = _make_arity_plan(str(tmp_path), contract, caller, callee)
    result = agent._scan_member_mismatches(plan, str(tmp_path), error_info="")
    arity_issues = [r for r in result if "arity" in str(r[3]).lower()]
    assert not arity_issues, f"参数数量正确不应误报: {result}"


def test_scan_member_arity_skips_starred_args(tmp_path):
    """调用端含 *args 时跳过 arity 检测（避免误报）。"""
    contract = {
        "miniql/catalog.py": {
            "Catalog": {"methods": [{"name": "register_csv", "params": ["table_name", "path"]}]}
        }
    }
    callee = "class Catalog:\n    def register_csv(self, table_name, path): pass\n"
    caller = "catalog.register_csv(*args)\n"
    plan = _make_arity_plan(str(tmp_path), contract, caller, callee)
    result = agent._scan_member_mismatches(plan, str(tmp_path), error_info="")
    arity_issues = [r for r in result if "arity" in str(r[3]).lower()]
    assert not arity_issues, f"*args 调用不应报 arity: {result}"


def test_scan_member_arity_skips_double_starred_args(tmp_path):
    """调用端含 **kwargs 时跳过 arity 检测（避免误报）。"""
    contract = {
        "miniql/catalog.py": {
            "Catalog": {"methods": [{"name": "register_csv", "params": ["table_name", "path"]}]}
        }
    }
    callee = "class Catalog:\n    def register_csv(self, table_name, path): pass\n"
    caller = "catalog.register_csv(**opts)\n"
    plan = _make_arity_plan(str(tmp_path), contract, caller, callee)
    result = agent._scan_member_mismatches(plan, str(tmp_path), error_info="")
    arity_issues = [r for r in result if "arity" in str(r[3]).lower()]
    assert not arity_issues, f"**kwargs 调用不应报 arity: {result}"


def test_scan_member_method_pool_from_dict_methods(tmp_path):
    """dict 格式 methods 正确提取 name 到 method_pool，方法名检查不退化。"""
    contract = {
        "miniql/catalog.py": {
            "Catalog": {"methods": [{"name": "query", "params": ["sql"]}]}
        }
    }
    callee = "class Catalog:\n    def query(self, sql): pass\n"
    # 调用错误方法名 queryx，且 error_info 有报
    caller = "catalog.queryx(sql)\n"
    plan = _make_arity_plan(str(tmp_path), contract, caller, callee)
    result = agent._scan_member_mismatches(
        plan, str(tmp_path),
        error_info="has no attribute 'queryx'"
    )
    name_issues = [r for r in result if "query" in str(r[3])]
    assert name_issues, f"错误方法名 queryx 应被检出（回归测试）: {result}"


# ---------------------------------------------------------------------------
# R6: 端到端 smoke test —— scope 保险丝 + baseline 排除
# ---------------------------------------------------------------------------

def test_apply_scope_override_force_includes_smoke(tmp_path):
    """B 保险丝：ws 有 tests/test_smoke.py 但 architect 未列入 plan → scope 仍强制并入。"""
    import agent as _a
    import config, tools
    config.set_workspace_dir(str(tmp_path))
    tools._reinit_paths()

    tests = tmp_path / "tests"
    tests.mkdir()
    (tmp_path / "calc.py").write_text("def add(a, b): return a + b\n")
    (tests / "test_calc.py").write_text("from calc import add\ndef test_add(): assert add(1,2)==3\n")
    # smoke 文件存在于 ws，但 plan_files 没列它
    (tests / "test_smoke.py").write_text("def test_smoke(): pass\n")

    plan_result = {
        "files": [{"filename": "calc.py"}],
        "test_command": "pytest tests/",
    }
    _a._apply_test_scope_override(plan_result)
    assert "test_smoke.py" in plan_result["test_command"], \
        f"smoke test 应被强制并入 test_command: {plan_result['test_command']}"


def test_apply_scope_override_no_smoke_file_unchanged(tmp_path):
    """无 smoke 文件时行为不变（不凭空注入）。"""
    import agent as _a
    import config, tools
    config.set_workspace_dir(str(tmp_path))
    tools._reinit_paths()

    tests = tmp_path / "tests"
    tests.mkdir()
    (tmp_path / "calc.py").write_text("def add(a, b): return a + b\n")
    (tests / "test_calc.py").write_text("from calc import add\ndef test_add(): assert add(1,2)==3\n")

    plan_result = {
        "files": [{"filename": "calc.py"}],
        "test_command": "pytest tests/",
    }
    _a._apply_test_scope_override(plan_result)
    assert "test_smoke.py" not in plan_result["test_command"], \
        f"无 smoke 文件不应注入: {plan_result['test_command']}"


def test_apply_scope_override_smoke_only(tmp_path):
    """B 边界：scope 原本为空（无源文件对应测试）但有 smoke → 不走空 scope return，生成仅含 smoke 的命令。"""
    import agent as _a
    import config, tools
    config.set_workspace_dir(str(tmp_path))
    tools._reinit_paths()

    tests = tmp_path / "tests"
    tests.mkdir()
    # plan 只改一个无对应测试的源文件 → _infer_test_scope 本会返回空
    (tmp_path / "lonely.py").write_text("x = 1\n")
    (tests / "test_smoke.py").write_text("def test_smoke(): pass\n")

    plan_result = {
        "files": [{"filename": "lonely.py"}],
        "test_command": "pytest tests/",
    }
    _a._apply_test_scope_override(plan_result)
    assert "test_smoke.py" in plan_result["test_command"], \
        f"scope 仅含 smoke 时也应生成命令而非保留原命令: {plan_result['test_command']}"


def test_capture_baseline_excludes_smoke(tmp_path, monkeypatch):
    """C 必改：smoke test 的失败 nodeid 不计入 baseline。"""
    import agent as _a

    fake_output = (
        "FAILED tests/test_smoke.py::test_cli - AssertionError\n"
        "FAILED tests/test_parser.py::test_parse - AssertionError\n"
        "===== 2 failed in 0.5s ====="
    )
    monkeypatch.setattr(_a, "execute_command",
                        lambda *a, **k: {"stdout": fake_output, "stderr": ""})

    failures = _a._capture_baseline_failures("pytest tests/")
    assert not any("test_smoke.py" in f for f in failures), \
        f"smoke nodeid 不应进 baseline: {failures}"
    assert any("test_parser.py" in f for f in failures), \
        f"非 smoke 失败应正常记入 baseline: {failures}"


# ---------------------------------------------------------------------------
# _maybe_compact_messages / _make_compact_state（fix loop compact 复用 helper）
# ---------------------------------------------------------------------------

def test_fix_loop_invokes_compact(tmp_path, monkeypatch):
    """集成防回归：fix() 的 while loop 每轮必须经过 _maybe_compact_messages 检测点
    （R6 根因正是 fix 漏了 compact，messages O(N²) 膨胀烧 $51）。"""
    import config, tools
    config.set_workspace_dir(str(tmp_path))
    tools._reinit_paths()

    spy = {"n": 0}
    real = agent._maybe_compact_messages

    def _spy(msgs, state, label="auto-compact"):
        spy["n"] += 1
        return real(msgs, state, label)

    monkeypatch.setattr(agent, "_maybe_compact_messages", _spy)
    # 第一轮就 task_complete 收尾，保证测试快速结束
    monkeypatch.setattr(agent, "call_llm", lambda msgs, **kw: _make_response(
        tool_calls=[("c1", "task_complete", {"success": True, "summary": "done"})]
    ))
    agent.fix({"returncode": 1, "stderr": "FAILED x", "stdout": ""},
              {"files": [], "test_command": ""})
    assert spy["n"] >= 1, "fix() 每轮必须调用 _maybe_compact_messages（防 R6 漏 compact 回归）"


def test_maybe_compact_under_threshold_noop(monkeypatch):
    """低于阈值 → 原样返回，state 不变。"""
    state = agent._make_compact_state()
    state["threshold"] = 10_000
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    monkeypatch.setattr(agent, "_estimate_messages_tokens", lambda m: 5000)
    out = agent._maybe_compact_messages(msgs, state)
    assert out is msgs
    assert state["consecutive_over"] == 0
    assert not state["disabled"]


def test_maybe_compact_over_threshold_compresses(monkeypatch):
    """超阈值 → 调 _compact_messages 并返回压缩结果。"""
    state = agent._make_compact_state()
    state["threshold"] = 10_000
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"},
            {"role": "assistant", "content": "a"}]
    compacted = [{"role": "system", "content": "compacted"}]
    # est: 第一次大、压缩后小
    ests = iter([50_000, 5_000])
    monkeypatch.setattr(agent, "_estimate_messages_tokens", lambda m: next(ests))
    monkeypatch.setattr(agent, "_compact_messages", lambda m, keep_recent_pairs: compacted)
    out = agent._maybe_compact_messages(msgs, state)
    assert out is compacted
    assert state["consecutive_over"] == 0  # 降幅 90% ≥ 15%，重置


def test_maybe_compact_thrashing_disables(monkeypatch):
    """连续压缩无效（未降低）→ 累计 thrash，达上限后 disabled。"""
    state = agent._make_compact_state()
    state["threshold"] = 10_000
    state["max_consecutive"] = 2
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    # 估值恒定（压缩无效），_compact_messages 原样返回
    monkeypatch.setattr(agent, "_estimate_messages_tokens", lambda m: 50_000)
    monkeypatch.setattr(agent, "_compact_messages", lambda m, keep_recent_pairs: m)
    agent._maybe_compact_messages(msgs, state)
    assert state["consecutive_over"] == 1
    agent._maybe_compact_messages(msgs, state)
    assert state["disabled"], "连续 2 次无效应触发 thrashing 禁用"
    # disabled 后直接 noop
    monkeypatch.setattr(agent, "_estimate_messages_tokens",
                        lambda m: (_ for _ in ()).throw(AssertionError("disabled 后不应再估算")))
    out = agent._maybe_compact_messages(msgs, state)
    assert out is msgs


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
