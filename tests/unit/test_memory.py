"""P2 #12 跨 Session 持久记忆单元测试。

覆盖：
  - frontmatter 解析（含 metadata.type 嵌套）
  - parse_memory_file 各种边界
  - discover_memories 双路径 + 项目级覆盖全局
  - save_memory / delete_memory 写入 + 索引更新
  - find_memory 项目级优先
  - load_memory_index 拼接 + 无 memory 返空
  - tools.save_memory / recall_memory 入口
  - agent 路由 + readonly 白名单
"""
import os
import sys
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import memory


# ---------- frontmatter 解析 ----------

def test_parse_frontmatter_basic():
    text = """---
name: foo
description: 一句话索引
metadata:
  type: user
---

正文内容"""
    meta, body = memory._parse_frontmatter(text)
    assert meta["name"] == "foo"
    assert meta["description"] == "一句话索引"
    assert meta["metadata"]["type"] == "user"
    assert body.strip() == "正文内容"


def test_parse_frontmatter_no_frontmatter():
    text = "纯正文，没有 frontmatter"
    meta, body = memory._parse_frontmatter(text)
    assert meta == {}
    assert body == text


def test_parse_frontmatter_quoted_values():
    text = """---
name: 'with-quotes'
description: "带引号的描述"
metadata:
  type: 'project'
---
body"""
    meta, _ = memory._parse_frontmatter(text)
    assert meta["name"] == "with-quotes"
    assert meta["description"] == "带引号的描述"
    assert meta["metadata"]["type"] == "project"


def test_parse_memory_file_complete(tmp_path):
    f = tmp_path / "test.md"
    f.write_text("""---
name: test-mem
description: 测试用
metadata:
  type: feedback
---

正文""", encoding="utf-8")
    mem = memory.parse_memory_file(str(f), scope="project")
    assert mem is not None
    assert mem.name == "test-mem"
    assert mem.type == "feedback"
    assert mem.description == "测试用"
    assert mem.body.strip() == "正文"
    assert mem.scope == "project"


def test_parse_memory_file_missing_returns_none(tmp_path):
    mem = memory.parse_memory_file(str(tmp_path / "nonexistent.md"))
    assert mem is None


def test_parse_memory_file_invalid_type_falls_back_to_project(tmp_path):
    f = tmp_path / "x.md"
    f.write_text("""---
name: x
metadata:
  type: nonsense
---
""", encoding="utf-8")
    mem = memory.parse_memory_file(str(f))
    assert mem.type == "project"   # invalid type → fallback


def test_parse_memory_file_no_frontmatter_uses_filename(tmp_path):
    f = tmp_path / "no-fm.md"
    f.write_text("just text", encoding="utf-8")
    mem = memory.parse_memory_file(str(f))
    assert mem.name == "no-fm"   # filename 当 name


# ---------- discover_memories 双路径 ----------

def test_discover_memories_project_only(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "fake_home")
    proj_dir = tmp_path / "ws" / ".yansh" / "memory"
    proj_dir.mkdir(parents=True)
    (proj_dir / "alpha.md").write_text("""---
name: alpha
description: 项目 alpha
metadata:
  type: project
---
""", encoding="utf-8")
    mems = memory.discover_memories(str(tmp_path / "ws"))
    assert len(mems) == 1
    assert mems[0].name == "alpha"
    assert mems[0].scope == "project"


def test_discover_memories_global_only(tmp_path, monkeypatch):
    home = tmp_path / "home"
    g_dir = home / ".yansh" / "memory"
    g_dir.mkdir(parents=True)
    (g_dir / "g1.md").write_text("""---
name: g1
metadata:
  type: user
---
""", encoding="utf-8")
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    mems = memory.discover_memories(str(tmp_path / "ws_no_dir"))
    assert len(mems) == 1
    assert mems[0].name == "g1"
    assert mems[0].scope == "global"


def test_discover_memories_project_overrides_global(tmp_path, monkeypatch):
    """同 name 时项目级覆盖全局"""
    home = tmp_path / "home"
    g_dir = home / ".yansh" / "memory"
    g_dir.mkdir(parents=True)
    (g_dir / "shared.md").write_text("""---
name: shared
description: 全局版
metadata:
  type: user
---
""", encoding="utf-8")
    monkeypatch.setattr("pathlib.Path.home", lambda: home)

    proj_dir = tmp_path / "ws" / ".yansh" / "memory"
    proj_dir.mkdir(parents=True)
    (proj_dir / "shared.md").write_text("""---
name: shared
description: 项目版
metadata:
  type: project
---
""", encoding="utf-8")

    mems = memory.discover_memories(str(tmp_path / "ws"))
    assert len(mems) == 1
    assert mems[0].description == "项目版"
    assert mems[0].scope == "project"


def test_discover_memories_skips_index_md(tmp_path, monkeypatch):
    """MEMORY.md 索引文件不应被当 memory 解析"""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "fake_home")
    proj_dir = tmp_path / "ws" / ".yansh" / "memory"
    proj_dir.mkdir(parents=True)
    (proj_dir / "MEMORY.md").write_text("# 索引\n", encoding="utf-8")
    (proj_dir / "real.md").write_text("""---
name: real
metadata:
  type: project
---
""", encoding="utf-8")
    mems = memory.discover_memories(str(tmp_path / "ws"))
    names = [m.name for m in mems]
    assert "real" in names
    assert "MEMORY" not in names


# ---------- save_memory / delete_memory ----------

def test_save_memory_writes_file_and_index(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "fake_home")
    r = memory.save_memory(
        name="user-prefers-vim",
        type="user",
        description="用户用 vim",
        body="详细解释...",
        scope="project",
        workspace_dir=str(tmp_path / "ws"),
    )
    assert "saved" in r
    f = tmp_path / "ws" / ".yansh" / "memory" / "user-prefers-vim.md"
    assert f.exists()
    text = f.read_text(encoding="utf-8")
    assert "name: user-prefers-vim" in text
    assert "type: user" in text
    assert "用户用 vim" in text
    # 索引也写了
    idx = tmp_path / "ws" / ".yansh" / "memory" / "MEMORY.md"
    assert idx.exists()
    assert "user-prefers-vim" in idx.read_text(encoding="utf-8")


def test_save_memory_invalid_type_returns_error(tmp_path):
    r = memory.save_memory(name="x", type="invalid", description="d", body="b",
                            scope="project", workspace_dir=str(tmp_path))
    assert "error" in r


def test_save_memory_invalid_scope_returns_error(tmp_path):
    r = memory.save_memory(name="x", type="user", description="d", body="b",
                            scope="elsewhere", workspace_dir=str(tmp_path))
    assert "error" in r


def test_save_memory_slugifies_name(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "fake_home")
    r = memory.save_memory(
        name="Some Name With Spaces!", type="user",
        description="d", body="b", scope="project",
        workspace_dir=str(tmp_path / "ws"),
    )
    assert r["name"] == "some-name-with-spaces"
    f = tmp_path / "ws" / ".yansh" / "memory" / "some-name-with-spaces.md"
    assert f.exists()


def test_save_memory_overwrites_existing(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "fake_home")
    ws = str(tmp_path / "ws")
    memory.save_memory("dup", "user", "v1", "body 1", scope="project", workspace_dir=ws)
    memory.save_memory("dup", "user", "v2", "body 2", scope="project", workspace_dir=ws)
    mem = memory.find_memory("dup", workspace_dir=ws)
    assert mem.description == "v2"
    assert "body 2" in mem.body
    assert "body 1" not in mem.body


def test_save_memory_global_scope(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    r = memory.save_memory("g1", "reference", "全局参考", "body",
                            scope="global", workspace_dir=str(tmp_path))
    assert "saved" in r
    f = home / ".yansh" / "memory" / "g1.md"
    assert f.exists()


def test_delete_memory(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "fake_home")
    ws = str(tmp_path / "ws")
    memory.save_memory("to-del", "user", "x", "y", scope="project", workspace_dir=ws)
    f = tmp_path / "ws" / ".yansh" / "memory" / "to-del.md"
    assert f.exists()
    r = memory.delete_memory("to-del", scope="project", workspace_dir=ws)
    assert "deleted" in r
    assert not f.exists()


def test_delete_memory_nonexistent(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "fake_home")
    r = memory.delete_memory("ghost", scope="project",
                              workspace_dir=str(tmp_path / "ws"))
    assert "error" in r


def test_delete_clears_index_when_empty(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "fake_home")
    ws = str(tmp_path / "ws")
    memory.save_memory("only", "user", "x", "y", scope="project", workspace_dir=ws)
    idx = tmp_path / "ws" / ".yansh" / "memory" / "MEMORY.md"
    assert idx.exists()
    memory.delete_memory("only", scope="project", workspace_dir=ws)
    assert not idx.exists()   # 没 memory 了索引也清


# ---------- find_memory ----------

def test_find_memory_project_priority(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    ws = str(tmp_path / "ws")
    memory.save_memory("conflict", "user", "全局", "g body",
                        scope="global", workspace_dir=ws)
    memory.save_memory("conflict", "project", "项目", "p body",
                        scope="project", workspace_dir=ws)
    mem = memory.find_memory("conflict", workspace_dir=ws)
    assert mem.scope == "project"
    assert "p body" in mem.body


def test_find_memory_missing_returns_none(tmp_path):
    assert memory.find_memory("ghost", workspace_dir=str(tmp_path)) is None


# ---------- load_memory_index ----------

def test_load_memory_index_empty_returns_empty_string(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "fake_home")
    out = memory.load_memory_index(str(tmp_path / "ws"))
    assert out == ""


def test_load_memory_index_includes_descriptions(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "fake_home")
    ws = str(tmp_path / "ws")
    memory.save_memory("alpha", "user", "alpha 描述", "body a",
                        scope="project", workspace_dir=ws)
    memory.save_memory("beta", "project", "beta 描述", "body b",
                        scope="project", workspace_dir=ws)
    idx = memory.load_memory_index(ws)
    assert "alpha 描述" in idx
    assert "beta 描述" in idx
    assert "recall_memory" in idx   # 提示文字含调用方法
    assert "save_memory" in idx


# ---------- tools.save_memory / recall_memory ----------

def test_tools_save_and_recall(tmp_path, monkeypatch):
    """tools.save_memory / recall_memory 调到 memory 模块"""
    import tools as _tools
    import config as _config_mod
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "fake_home")
    monkeypatch.setattr(_config_mod, "WORKSPACE_DIR", str(tmp_path / "ws"))

    r = _tools.save_memory(name="hello", type="user",
                           description="d", body="b body")
    assert "saved" in r

    r2 = _tools.recall_memory(name="hello")
    assert r2["name"] == "hello"
    assert "b body" in r2["body"]


def test_tools_recall_memory_not_found(tmp_path, monkeypatch):
    import tools as _tools
    import config as _config_mod
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "fake_home")
    monkeypatch.setattr(_config_mod, "WORKSPACE_DIR", str(tmp_path / "ws"))
    r = _tools.recall_memory(name="ghost")
    assert "error" in r


# ---------- agent 集成 ----------

def test_save_memory_in_readonly_whitelist():
    """save_memory / recall_memory 应在 READONLY_TOOL_NAMES 里"""
    from tools_schema import READONLY_TOOL_NAMES
    assert "save_memory" in READONLY_TOOL_NAMES
    assert "recall_memory" in READONLY_TOOL_NAMES


def test_save_memory_schema_present():
    from tools_schema import TOOLS
    names = {t["function"]["name"] for t in TOOLS}
    assert "save_memory" in names
    assert "recall_memory" in names


def test_save_memory_schema_has_type_enum():
    from tools_schema import TOOLS
    schema = next(t for t in TOOLS if t["function"]["name"] == "save_memory")
    type_param = schema["function"]["parameters"]["properties"]["type"]
    assert set(type_param["enum"]) == {"user", "feedback", "project", "reference"}


def test_agent_dispatch_save_memory(tmp_path, monkeypatch):
    """LLM 调 save_memory tool_call → _dispatch_tool_call 路由到 readonly_handlers"""
    import agent
    import config as _config_mod
    from unittest.mock import MagicMock

    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "fake_home")
    monkeypatch.setattr(_config_mod, "WORKSPACE_DIR", str(tmp_path / "ws"))

    tc = MagicMock()
    tc.id = "c1"
    tc.function.name = "save_memory"
    tc.function.arguments = json.dumps({
        "name": "test", "type": "user",
        "description": "desc", "body": "body content",
    })
    out = agent._dispatch_tool_call(tc, mode="audit",
                                      allow_hil=False, allow_confirm=False)
    assert "saved" in out["result"]
    # 真落了盘
    f = tmp_path / "ws" / ".yansh" / "memory" / "test.md"
    assert f.exists()


# ---------- list_all ----------

def test_list_all_includes_scope(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    ws = str(tmp_path / "ws")
    memory.save_memory("p", "project", "p 描述", "body",
                        scope="project", workspace_dir=ws)
    memory.save_memory("g", "user", "g 描述", "body",
                        scope="global", workspace_dir=ws)
    items = memory.list_all(ws)
    by_scope = {it["name"]: it["scope"] for it in items}
    assert by_scope["p"] == "project"
    assert by_scope["g"] == "global"


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
