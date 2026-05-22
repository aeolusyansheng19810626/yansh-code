"""JSON 解析与 schema 校验：plan/review 响应的健壮性"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import agent


# ---------- _extract_json 边界 ----------

def test_extract_json_from_markdown_block():
    raw = '```json\n{"a": 1}\n```'
    assert agent._extract_json(raw).strip() == '{"a": 1}'


def test_extract_json_from_plain_braces():
    raw = '前缀文字 {"a": 1, "b": 2} 后缀'
    assert '{"a": 1, "b": 2}' in agent._extract_json(raw)


def test_extract_json_handles_unclosed_braces():
    """没有闭合大括号时返回原文（由调用方 json.loads 失败）"""
    raw = '不是 JSON 的纯文本'
    assert agent._extract_json(raw) == raw


# ---------- plan 响应解析 ----------

def test_parse_plan_valid():
    content = '{"files": [{"filename": "a.py", "description": "新建"}], "test_command": "python a.py"}'
    res = agent._parse_plan_response(content)
    assert res["test_command"] == "python a.py"
    assert len(res["files"]) == 1
    assert res["files"][0]["filename"] == "a.py"
    assert res["files"][0]["description"] == "新建"


def test_parse_plan_with_markdown_block():
    content = '```json\n{"files": [], "test_command": "pytest"}\n```'
    res = agent._parse_plan_response(content)
    assert res["test_command"] == "pytest"
    assert res["files"] == []


def test_parse_plan_array_top_level_compat():
    """旧形态：LLM 直接返回数组，应兼容包成 {files: [...]}"""
    content = '[{"filename": "x.py", "description": "test"}]'
    res = agent._parse_plan_response(content)
    assert len(res["files"]) == 1
    assert res["files"][0]["filename"] == "x.py"


def test_parse_plan_invalid_json_returns_empty(capsys):
    """非 JSON：返回空 plan 且必须 log（不静默吞）"""
    content = "this is not json at all"
    res = agent._parse_plan_response(content)
    assert res == {"files": [], "test_command": ""}
    captured = capsys.readouterr()
    # console.print 默认到 stdout；只要包含告警关键字即算 log
    assert "JSON 校验失败" in captured.out or "JSON 校验失败" in captured.err


def test_parse_plan_empty_returns_empty_with_log(capsys):
    res = agent._parse_plan_response("")
    assert res == {"files": [], "test_command": ""}
    captured = capsys.readouterr()
    assert "JSON 校验失败" in captured.out or "JSON 校验失败" in captured.err


def test_parse_plan_extra_fields_allowed():
    """LLM 加额外字段不应失败"""
    content = '{"files": [], "test_command": "pytest", "extra_field": 123}'
    res = agent._parse_plan_response(content)
    assert res["test_command"] == "pytest"


# ---------- review 响应解析 ----------

def test_parse_review_valid():
    content = '{"approved": true, "issues": [], "suggestions": ["s1"]}'
    res = agent._parse_review_response(content)
    assert res["approved"] is True
    assert res["suggestions"] == ["s1"]


def test_parse_review_invalid_json_logs_and_carries_error(capsys):
    res = agent._parse_review_response("not json")
    assert res["approved"] is False
    assert any("review_error" in i for i in res["issues"])
    captured = capsys.readouterr()
    assert "JSON 校验失败" in captured.out or "JSON 校验失败" in captured.err


def test_parse_review_missing_required_fields(capsys):
    """approved 缺失：schema 校验失败但兜底返回 dict"""
    content = '{"issues": ["x"], "suggestions": []}'
    res = agent._parse_review_response(content)
    assert res["approved"] is False
    captured = capsys.readouterr()
    assert "schema 校验失败" in captured.out or "schema 校验失败" in captured.err


def test_parse_review_dict_issues_allowed():
    """issues 元素允许是 dict（实际 LLM 偶尔返回结构化对象）"""
    content = '{"approved": false, "issues": [{"file": "a.py", "msg": "x"}], "suggestions": []}'
    res = agent._parse_review_response(content)
    assert res["approved"] is False
    assert isinstance(res["issues"][0], dict)


# ---------- pydantic schema 直接验证 ----------

def test_plan_schema_direct():
    p = agent.PlanResult(files=[{"filename": "a.py"}], test_command="pytest")
    assert p.test_command == "pytest"
    assert len(p.files) == 1


def test_review_schema_direct():
    r = agent.ReviewResult(approved=False, issues=["x"], suggestions=[])
    assert r.approved is False


# ============= P1 #4：retry 包装 + ICA response_format 探测 =============

from types import SimpleNamespace
from unittest.mock import MagicMock


def _mk_resp(content):
    msg = MagicMock()
    msg.content = content
    msg.tool_calls = None
    resp = MagicMock()
    resp.choices = [MagicMock(message=msg)]
    return resp


def test_call_with_json_retry_succeeds_first_try():
    """parser_fn 第一次返回 ok=True 时不应 retry"""
    calls = {"n": 0}

    def fake_call_llm(**kwargs):
        calls["n"] += 1
        return _mk_resp('{"ok": 1}')

    orig = agent.call_llm
    try:
        agent.call_llm = fake_call_llm
        parser = lambda c: (True, {"ok": 1}, None)
        result = agent._call_with_json_retry("test", [{"role": "user", "content": "x"}], parser)
    finally:
        agent.call_llm = orig
    assert calls["n"] == 1
    assert result == {"ok": 1}


def test_call_with_json_retry_recovers_on_second_try():
    """第一次失败 → 第二次成功，应只调两次 call_llm 并返回成功值"""
    calls = {"n": 0, "second_messages": None}

    def fake_call_llm(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _mk_resp("not valid json")
        calls["second_messages"] = kwargs.get("messages")
        return _mk_resp('{"recovered": true}')

    parse_calls = {"n": 0}
    def parser(c):
        parse_calls["n"] += 1
        if "recovered" in c:
            return (True, {"recovered": True}, None)
        return (False, {}, "json.loads 失败：mock")

    orig = agent.call_llm
    try:
        agent.call_llm = fake_call_llm
        result = agent._call_with_json_retry(
            "test", [{"role": "user", "content": "x"}], parser
        )
    finally:
        agent.call_llm = orig

    assert calls["n"] == 2
    assert result == {"recovered": True}
    # 第二次 messages 必须比第一次多 2 条（assistant raw + user 修正）
    assert len(calls["second_messages"]) == 3
    assert calls["second_messages"][-1]["role"] == "user"
    assert "无法被解析" in calls["second_messages"][-1]["content"]


def test_call_with_json_retry_returns_fallback_on_double_fail(capsys):
    """两次都失败 → log raw + 返回 parser 给的降级 dict"""
    def fake_call_llm(**kwargs):
        return _mk_resp("garbage")

    fallback = {"files": [], "test_command": ""}
    parser = lambda c: (False, fallback, "json.loads: garbage")

    orig = agent.call_llm
    try:
        agent.call_llm = fake_call_llm
        result = agent._call_with_json_retry(
            "test", [{"role": "user", "content": "x"}], parser
        )
    finally:
        agent.call_llm = orig

    assert result is fallback
    captured = capsys.readouterr()
    out = captured.out + captured.err
    # 两个标志都应出现：retry 提示 + 最终 log
    assert "retry" in out.lower() or "重试" in out or "JSON 解析失败" in out
    assert "JSON 校验失败" in out


def test_parse_plan_with_status_returns_tri():
    """新版 _parse_plan_with_status 必须返回 3 元组"""
    ok, data, err = agent._parse_plan_with_status('{"files": [], "test_command": "x"}')
    assert ok is True and err is None and data["test_command"] == "x"

    ok, data, err = agent._parse_plan_with_status("not json")
    assert ok is False and err and "json.loads" in err


def test_parse_review_with_status_returns_tri():
    ok, data, err = agent._parse_review_with_status(
        '{"approved": true, "issues": [], "suggestions": []}'
    )
    assert ok is True and err is None
    ok, data, err = agent._parse_review_with_status('{"issues": []}')
    # 缺 approved 字段 → schema 校验失败但兜底 dict
    assert ok is False and "schema" in err
    assert data["approved"] is False


def test_llm_client_rf_unsupported_detection():
    """探测：后端 400 提示 response_format 不支持 → 该 model 加进黑名单后续跳过"""
    import llm_client
    llm_client._RF_UNSUPPORTED.clear()
    err = Exception("Unknown parameter: response_format")
    assert llm_client._looks_like_rf_rejection(err) is True
    err2 = Exception("rate limit exceeded")
    assert llm_client._looks_like_rf_rejection(err2) is False


def test_llm_client_rf_unsupported_keywords():
    """更多关键字命中"""
    import llm_client
    for msg in ("not supported: response_format", "unsupported parameter",
                "json_object format", "response_format invalid"):
        assert llm_client._looks_like_rf_rejection(Exception(msg)) is True


def test_llm_client_should_skip_rf_claude_hardcoded():
    """Claude 走 ICA 实测降质（输出 {}）→ 硬跳过 response_format"""
    import llm_client
    assert llm_client._should_skip_rf("claude-sonnet-4-6") is True
    assert llm_client._should_skip_rf("claude-haiku-4-5") is True
    # 非 Claude 不在硬规则里，默认不跳
    assert llm_client._should_skip_rf("gpt-4o-mini") is False


def test_llm_client_should_skip_rf_dynamic_blacklist():
    """动态黑名单（运行时报 400 后加入）应让任何 model 被跳过"""
    import llm_client
    llm_client._RF_UNSUPPORTED.discard("test-model")
    assert llm_client._should_skip_rf("test-model") is False
    llm_client._RF_UNSUPPORTED.add("test-model")
    try:
        assert llm_client._should_skip_rf("test-model") is True
    finally:
        llm_client._RF_UNSUPPORTED.discard("test-model")


# ============= P1 #5：Session / state 模块 =============

def test_session_pull_push_roundtrip(tmp_path):
    """pull 拍快照 → 修改模块级 → push 恢复"""
    import agent as _a
    import state

    snap = state.Session().pull()
    saved = snap.batch_mode
    _a._BATCH_MODE = not saved
    state.Session(batch_mode=saved).push()
    assert _a._BATCH_MODE == saved


def test_session_reset_clears_state(tmp_path):
    import agent as _a
    import tools as _t
    import state

    _a._BATCH_MODE = True
    _a._PROJECT_TYPE = "Python"
    _a._CURRENT_SNAPSHOT = {"foo": "bar"}
    # 写一条 AST 缓存
    _t._AST_CACHE["dummy"] = (0, [{"name": "x"}])

    state.Session().reset(workspace_dir=str(tmp_path))

    assert _a._BATCH_MODE is False
    assert _a._PROJECT_TYPE is None
    assert _a._CURRENT_SNAPSHOT is None
    assert "dummy" not in _t._AST_CACHE


def test_scoped_session_restores_outer(tmp_path):
    import agent as _a
    import state

    _a._BATCH_MODE = True
    _a._PROJECT_TYPE = "outer"
    with state.scoped_session(tmp_path):
        # 内部状态被清零
        assert _a._BATCH_MODE is False
        assert _a._PROJECT_TYPE is None
        # 内部修改不应泄露到外面
        _a._BATCH_MODE = True
    # 退出时恢复
    assert _a._BATCH_MODE is True
    assert _a._PROJECT_TYPE == "outer"


# ============= P1 #6：sandbox =============

def test_sandbox_disabled_passthrough():
    import sandbox
    sandbox.set_config(sandbox.SandboxConfig(enabled=False))
    assert sandbox.wrap_command("echo hi", "/tmp/ws") == "echo hi"


def test_sandbox_docker_wraps():
    import sandbox
    sandbox.set_config(sandbox.SandboxConfig(enabled=True, backend="docker", image="python:3.11-slim"))
    out = sandbox.wrap_command("python -V", "/tmp/ws")
    assert "docker run" in out
    assert "-v" in out
    assert "/ws" in out
    assert "python:3.11-slim" in out
    assert "sh -c" in out
    sandbox.set_config(sandbox.SandboxConfig())  # 清理


def test_sandbox_docker_quotes_workspace_with_spaces():
    import sandbox
    sandbox.set_config(sandbox.SandboxConfig(enabled=True, backend="docker"))
    out = sandbox.wrap_command("echo x", "/tmp/has space/ws")
    # shlex.quote 必须把含空格的路径用引号包起来；具体引号字符（'）在 Windows/Posix 都成立
    assert "'" in out and "has space" in out
    sandbox.set_config(sandbox.SandboxConfig())


def test_sandbox_parse_cli_arg_variants():
    import sandbox
    assert sandbox.parse_cli_arg(None).enabled is False
    assert sandbox.parse_cli_arg("none").enabled is False
    cfg = sandbox.parse_cli_arg("docker")
    assert cfg.enabled is True and cfg.image == sandbox.DEFAULT_IMAGE
    cfg2 = sandbox.parse_cli_arg("docker:python:3.12")
    assert cfg2.enabled is True and cfg2.image == "python:3.12"


def test_sandbox_parse_cli_arg_unknown_backend():
    import sandbox
    import pytest
    with pytest.raises(ValueError):
        sandbox.parse_cli_arg("podman")


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
