"""P4-1 frontmatter.parse 单元测试。

之前 skills.py / memory.py 各写一份解析。本测试覆盖通用 frontmatter
能解析两个模块都需要的特性：
  - 标量 key: value（去引号）
  - list [a, b, "c d"]
  - 一级嵌套 metadata: {type: x}
  - 边界（无 frontmatter / 注释 / 空 value）
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import frontmatter as fm


def test_no_frontmatter_returns_text_unchanged():
    text = "纯正文，没有 frontmatter\n第二行"
    meta, body = fm.parse(text)
    assert meta == {}
    assert body == text


def test_basic_scalar():
    text = """---
name: foo
description: 一行
---
正文"""
    meta, body = fm.parse(text)
    assert meta["name"] == "foo"
    assert meta["description"] == "一行"
    assert body.strip() == "正文"


def test_quoted_scalar():
    text = """---
name: 'with-quote'
description: "带引号"
---
body"""
    meta, _ = fm.parse(text)
    assert meta["name"] == "with-quote"
    assert meta["description"] == "带引号"


def test_list_simple():
    text = """---
triggers: [test, build, run]
---
b"""
    meta, _ = fm.parse(text)
    assert meta["triggers"] == ["test", "build", "run"]


def test_list_with_quoted_items_and_internal_commas():
    text = """---
items: ["a, b", 'c d', plain]
---
b"""
    meta, _ = fm.parse(text)
    assert meta["items"] == ["a, b", "c d", "plain"]


def test_empty_list():
    text = """---
modes: []
---
b"""
    meta, _ = fm.parse(text)
    assert meta["modes"] == []


def test_nested_metadata_one_level():
    text = """---
name: x
metadata:
  type: user
  source: local
---
body"""
    meta, _ = fm.parse(text)
    assert meta["name"] == "x"
    assert meta["metadata"] == {"type": "user", "source": "local"}


def test_nested_metadata_then_top_level_resumes():
    """嵌套块结束后回到顶级"""
    text = """---
metadata:
  type: project
name: after
---
body"""
    meta, _ = fm.parse(text)
    assert meta["metadata"] == {"type": "project"}
    assert meta["name"] == "after"


def test_comment_lines_skipped():
    text = """---
# 这是注释
name: x
---
b"""
    meta, _ = fm.parse(text)
    assert meta == {"name": "x"}


def test_skills_compatibility():
    """模拟 skills.md 的真实输入"""
    text = """---
name: run-tests
description: 跑单元测试
triggers: [test, pytest, "unit test"]
modes: [code, fix]
---
跑测试时优先用 pytest..."""
    meta, body = fm.parse(text)
    assert meta["name"] == "run-tests"
    assert meta["triggers"] == ["test", "pytest", "unit test"]
    assert meta["modes"] == ["code", "fix"]
    assert "pytest" in body


def test_memory_compatibility():
    """模拟 memory.md 真实输入（含嵌套 metadata.type）"""
    text = """---
name: project-uses-pytest
description: 本项目用 pytest
metadata:
  type: project
---
正文 ...
"""
    meta, body = fm.parse(text)
    assert meta["name"] == "project-uses-pytest"
    assert meta["description"] == "本项目用 pytest"
    assert meta["metadata"]["type"] == "project"
    assert "正文" in body


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
