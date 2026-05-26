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


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
