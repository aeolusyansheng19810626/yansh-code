"""P2 #8 Skills 系统单元测试。

覆盖：
  - frontmatter 解析（标量/list/缺失/带注释）
  - 文件发现（项目级 / 全局优先级）
  - 关键字匹配（大小写无关 / 多 trigger / mode 过滤）
  - prompt 注入格式
  - run() 入口集成（_ACTIVE_SKILLS_PROMPT）
"""
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import skills


# ---------- frontmatter 解析 ----------

def test_parse_frontmatter_basic(tmp_path):
    f = tmp_path / "demo.md"
    f.write_text(
        "---\n"
        "name: refactor\n"
        "description: 重构工作流\n"
        'triggers: ["重构", "refactor"]\n'
        "---\n"
        "## 检查清单\n- 命名\n- 边界",
        encoding="utf-8",
    )
    sk = skills.parse_skill_file(str(f))
    assert sk is not None
    assert sk.name == "refactor"
    assert sk.description == "重构工作流"
    assert sk.triggers == ["重构", "refactor"]
    assert "检查清单" in sk.body


def test_parse_frontmatter_lowercase_triggers(tmp_path):
    """triggers 应被规整为小写"""
    f = tmp_path / "x.md"
    f.write_text(
        "---\n"
        'triggers: ["Review", "AUDIT"]\n'
        "---\nbody",
        encoding="utf-8",
    )
    sk = skills.parse_skill_file(str(f))
    assert sk.triggers == ["review", "audit"]


def test_parse_frontmatter_no_frontmatter(tmp_path):
    """无 frontmatter 的文件：name 用文件名 stem，triggers 为空"""
    f = tmp_path / "plain.md"
    f.write_text("# 纯 markdown\n内容", encoding="utf-8")
    sk = skills.parse_skill_file(str(f))
    assert sk.name == "plain"
    assert sk.triggers == []
    assert "纯 markdown" in sk.body


def test_parse_frontmatter_modes_field(tmp_path):
    f = tmp_path / "auditor.md"
    f.write_text(
        "---\n"
        "name: code-review\n"
        'triggers: ["review"]\n'
        'modes: ["audit", "plan"]\n'
        "---\nbody",
        encoding="utf-8",
    )
    sk = skills.parse_skill_file(str(f))
    assert sk.modes == ["audit", "plan"]
    assert sk.applies_to_mode("audit") is True
    assert sk.applies_to_mode("code") is False
    assert sk.applies_to_mode(None) is True   # 未指定 mode 时通用


def test_parse_frontmatter_empty_modes(tmp_path):
    """modes 缺失 = 全 mode 适用"""
    f = tmp_path / "any.md"
    f.write_text("---\ntriggers: [\"x\"]\n---\nbody", encoding="utf-8")
    sk = skills.parse_skill_file(str(f))
    assert sk.modes == []
    for m in ("plan", "code", "audit", "auto"):
        assert sk.applies_to_mode(m) is True


def test_parse_frontmatter_with_comments(tmp_path):
    f = tmp_path / "c.md"
    f.write_text(
        "---\n"
        "# 这行是注释\n"
        "name: x\n"
        'triggers: ["a"]\n'
        "---\nbody",
        encoding="utf-8",
    )
    sk = skills.parse_skill_file(str(f))
    assert sk.name == "x"
    assert sk.triggers == ["a"]


def test_parse_skill_file_missing_returns_none(tmp_path):
    """文件不存在时返回 None，不抛错"""
    sk = skills.parse_skill_file(str(tmp_path / "nope.md"))
    assert sk is None


# ---------- 发现 ----------

def test_discover_skills_project_level(tmp_path):
    sk_dir = tmp_path / "skills"
    sk_dir.mkdir()
    (sk_dir / "a.md").write_text(
        '---\nname: a\ntriggers: ["x"]\n---\nA-body', encoding="utf-8"
    )
    (sk_dir / "b.md").write_text(
        '---\nname: b\ntriggers: ["y"]\n---\nB-body', encoding="utf-8"
    )
    found = skills.discover_skills(str(tmp_path))
    names = sorted(s.name for s in found)
    assert names == ["a", "b"]


def test_discover_skills_no_dir_returns_empty(tmp_path):
    """workspace 没有 skills/ 目录时返回空列表，不报错"""
    assert skills.discover_skills(str(tmp_path)) == []


def test_discover_skills_project_overrides_global(tmp_path, monkeypatch):
    """同名 skill：项目级覆盖全局级"""
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    global_dir = fake_home / ".yansh" / "skills"
    global_dir.mkdir(parents=True)
    (global_dir / "shared.md").write_text(
        '---\nname: shared\ntriggers: ["g"]\n---\nGLOBAL', encoding="utf-8"
    )

    proj_dir = tmp_path / "proj" / "skills"
    proj_dir.mkdir(parents=True)
    (proj_dir / "shared.md").write_text(
        '---\nname: shared\ntriggers: ["p"]\n---\nPROJECT', encoding="utf-8"
    )

    found = skills.discover_skills(str(tmp_path / "proj"))
    assert len(found) == 1
    assert found[0].body == "PROJECT"
    assert found[0].triggers == ["p"]


def test_discover_skills_broken_file_does_not_crash(tmp_path):
    sk_dir = tmp_path / "skills"
    sk_dir.mkdir()
    # frontmatter 没闭合（缺尾 ---）
    (sk_dir / "broken.md").write_text("---\nname: x\nNOT CLOSED", encoding="utf-8")
    (sk_dir / "good.md").write_text(
        '---\nname: good\ntriggers: ["g"]\n---\nbody', encoding="utf-8"
    )
    found = skills.discover_skills(str(tmp_path))
    names = {s.name for s in found}
    assert "good" in names
    # broken 仍能解析（无 frontmatter 时退化 stem）；不应导致整体失败


# ---------- 匹配 ----------

def test_match_skills_by_keyword(tmp_path):
    sk_dir = tmp_path / "skills"
    sk_dir.mkdir()
    (sk_dir / "a.md").write_text(
        '---\nname: a\ntriggers: ["重构", "refactor"]\n---\nbody', encoding="utf-8"
    )
    all_sk = skills.discover_skills(str(tmp_path))

    assert [s.name for s in skills.match_skills("帮我重构这个函数", all_sk)] == ["a"]
    assert [s.name for s in skills.match_skills("Please REFACTOR this", all_sk)] == ["a"]
    assert skills.match_skills("无关需求", all_sk) == []


def test_match_skills_filters_by_mode(tmp_path):
    sk_dir = tmp_path / "skills"
    sk_dir.mkdir()
    (sk_dir / "a.md").write_text(
        '---\nname: a\ntriggers: ["x"]\nmodes: ["audit"]\n---\nbody',
        encoding="utf-8",
    )
    (sk_dir / "b.md").write_text(
        '---\nname: b\ntriggers: ["x"]\n---\nbody', encoding="utf-8"
    )
    all_sk = skills.discover_skills(str(tmp_path))

    # mode=audit 命中 a 和 b
    audit_match = sorted(s.name for s in skills.match_skills("x", all_sk, mode="audit"))
    assert audit_match == ["a", "b"]
    # mode=code 命中 b 不命中 a
    code_match = sorted(s.name for s in skills.match_skills("x", all_sk, mode="code"))
    assert code_match == ["b"]


def test_match_skills_empty_input(tmp_path):
    sk_dir = tmp_path / "skills"
    sk_dir.mkdir()
    (sk_dir / "a.md").write_text(
        '---\nname: a\ntriggers: ["x"]\n---\nbody', encoding="utf-8"
    )
    all_sk = skills.discover_skills(str(tmp_path))
    assert skills.match_skills("", all_sk) == []
    assert skills.match_skills(None, all_sk) == []


# ---------- 格式化 ----------

def test_format_skills_prompt_empty():
    assert skills.format_skills_prompt([]) == ""


def test_format_skills_prompt_includes_body():
    sk = skills.Skill(
        name="x", description="一个 skill",
        triggers=["t"], body="## 步骤\n- A\n- B",
    )
    out = skills.format_skills_prompt([sk])
    assert "skill: x" in out
    assert "一个 skill" in out
    assert "## 步骤" in out
    assert "- A" in out


def test_format_skills_prompt_multiple():
    a = skills.Skill(name="a", triggers=[], body="AAA")
    b = skills.Skill(name="b", triggers=[], body="BBB")
    out = skills.format_skills_prompt([a, b])
    assert "skill: a" in out
    assert "skill: b" in out
    assert out.find("AAA") < out.find("BBB")


def test_load_and_format_e2e(tmp_path):
    """端到端：发现 + 匹配 + 格式化"""
    sk_dir = tmp_path / "skills"
    sk_dir.mkdir()
    (sk_dir / "rev.md").write_text(
        '---\nname: rev\ntriggers: ["review"]\n---\nREVIEW-BODY',
        encoding="utf-8",
    )
    frag, matched = skills.load_and_format("please review my code", str(tmp_path))
    assert "rev" in frag
    assert "REVIEW-BODY" in frag
    assert len(matched) == 1


# ---------- 集成：agent 入口 ----------

def test_agent_run_loads_active_skills(tmp_path, monkeypatch):
    """run() 入口应扫描 skills 并写入 _ACTIVE_SKILLS_PROMPT"""
    import config, agent, tools, state
    sk_dir = tmp_path / "skills"
    sk_dir.mkdir()
    (sk_dir / "audit-helper.md").write_text(
        '---\nname: audit-helper\ntriggers: ["审查"]\nmodes: ["audit"]\n---\n## 注意\n- 命名一致性',
        encoding="utf-8",
    )

    with state.scoped_session(tmp_path):
        agent._ACTIVE_SKILLS_PROMPT = ""
        # mock audit() 立即返回，避开 LLM
        orig_audit = agent.audit
        try:
            agent.audit = lambda req: {"success": True, "report": "ok",
                                        "task_complete_signal": None}
            agent.run("请审查我的代码", mode="audit")
        finally:
            agent.audit = orig_audit

        # _ACTIVE_SKILLS_PROMPT 应含 skill 的 body
        assert "audit-helper" in agent._ACTIVE_SKILLS_PROMPT
        assert "命名一致性" in agent._ACTIVE_SKILLS_PROMPT


def test_agent_run_no_skill_match_clears_prompt(tmp_path):
    """没命中任何 skill 时 _ACTIVE_SKILLS_PROMPT 应为空"""
    import agent, state

    with state.scoped_session(tmp_path):
        agent._ACTIVE_SKILLS_PROMPT = "残留"
        orig_audit = agent.audit
        try:
            agent.audit = lambda req: {"success": True, "report": "",
                                        "task_complete_signal": None}
            agent.run("做点事情", mode="audit")
        finally:
            agent.audit = orig_audit
        assert agent._ACTIVE_SKILLS_PROMPT == ""


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
