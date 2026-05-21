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


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
