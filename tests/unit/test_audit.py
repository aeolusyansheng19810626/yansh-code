"""审计模式单测：workspace_symbols 缓存 + 只读工具白名单"""
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))


def _setup_ws(tmp_path):
    """切到一个空 tmp workspace，返回 (config, tools)"""
    import config, tools
    config.set_workspace_dir(str(tmp_path))
    tools._reinit_paths()
    tools._AST_CACHE.clear()
    return config, tools


def test_workspace_symbols_lists_python_symbols(tmp_path):
    """recursive=True 复原旧 deep 全量行为"""
    _, tools = _setup_ws(tmp_path)
    (tmp_path / "a.py").write_text(
        "def foo():\n    pass\n\nclass Bar:\n    def baz(self):\n        pass\n",
        encoding="utf-8",
    )
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.py").write_text("def hello():\n    pass\n", encoding="utf-8")
    # 非 .py 应被忽略
    (tmp_path / "readme.md").write_text("# hi", encoding="utf-8")

    res = tools.workspace_symbols(recursive=True)
    assert res["mode"] == "deep"
    assert res["total_files"] == 2
    assert res["total_symbols"] == 4  # foo + Bar + baz + hello
    assert "a.py" in res["files"]
    assert "sub/b.py" in res["files"]
    names_a = [s["name"] for s in res["files"]["a.py"]]
    assert "foo" in names_a and "Bar" in names_a and "baz" in names_a


def test_workspace_symbols_cache_hits(tmp_path):
    _, tools = _setup_ws(tmp_path)
    (tmp_path / "x.py").write_text("def f():\n    pass\n", encoding="utf-8")

    tools.workspace_symbols(recursive=True)
    abs_x = (tmp_path / "x.py").resolve()
    assert str(abs_x) in tools._AST_CACHE
    mtime_first, syms_first = tools._AST_CACHE[str(abs_x)]

    # 第二次调用：mtime 不变 → 命中缓存（同一对象引用）
    tools.workspace_symbols(recursive=True)
    mtime_second, syms_second = tools._AST_CACHE[str(abs_x)]
    assert mtime_first == mtime_second
    assert syms_first is syms_second


def test_workspace_symbols_cache_invalidates_on_mtime(tmp_path):
    _, tools = _setup_ws(tmp_path)
    p = tmp_path / "y.py"
    p.write_text("def f():\n    pass\n", encoding="utf-8")
    tools.workspace_symbols(recursive=True)
    abs_p = str(p.resolve())
    syms_before = tools._AST_CACHE[abs_p][1]

    time.sleep(0.05)  # 确保 mtime 有变化
    p.write_text("def f():\n    pass\n\ndef g():\n    pass\n", encoding="utf-8")
    # 强制 mtime 推进（某些 FS 粒度 1s）
    new_mtime = time.time() + 1
    os.utime(p, (new_mtime, new_mtime))

    res = tools.workspace_symbols(recursive=True)
    syms_after = tools._AST_CACHE[abs_p][1]
    assert syms_before is not syms_after
    assert len(syms_after) == 2
    names = [s["name"] for s in res["files"]["y.py"]]
    assert "g" in names


# ============= P0 #1：分层索引新增用例 =============

def test_workspace_symbols_top_only_lists_top_level(tmp_path):
    """默认 top 模式：只列顶层文件 + 子目录摘要"""
    _, tools = _setup_ws(tmp_path)
    (tmp_path / "a.py").write_text("def foo(): pass\n", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.py").write_text("def hello(): pass\n", encoding="utf-8")

    res = tools.workspace_symbols()  # default top
    assert res["mode"] == "top"
    assert res["path"] == "."
    # 顶层文件只含 a.py，不含 sub/b.py
    assert "a.py" in res["files"]
    assert "sub/b.py" not in res["files"]
    # subdirs 含 sub
    assert "sub" in res["subdirs"]
    assert res["subdirs"]["sub"]["py_files"] == 1
    assert res["subdirs"]["sub"]["total_symbols"] == 1
    # 顶层计数只算顶层
    assert res["total_files"] == 1
    assert res["total_symbols"] == 1


def test_workspace_symbols_top_subdirs_count_recursive_files(tmp_path):
    """top 模式下 subdirs 计数应递归算子目录嵌套文件"""
    _, tools = _setup_ws(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("def f(): pass\n", encoding="utf-8")
    (tmp_path / "src" / "deep").mkdir()
    (tmp_path / "src" / "deep" / "b.py").write_text(
        "def g(): pass\nclass C: pass\n", encoding="utf-8"
    )

    res = tools.workspace_symbols()
    assert "src" in res["subdirs"]
    assert res["subdirs"]["src"]["py_files"] == 2  # a.py + deep/b.py
    assert res["subdirs"]["src"]["total_symbols"] == 3  # f + g + C


def test_workspace_symbols_path_drilldown(tmp_path):
    """传 path 下钻该目录顶层"""
    _, tools = _setup_ws(tmp_path)
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "a.py").write_text("def f(): pass\n", encoding="utf-8")
    (tmp_path / "tools" / "nested").mkdir()
    (tmp_path / "tools" / "nested" / "b.py").write_text("def g(): pass\n", encoding="utf-8")

    res = tools.workspace_symbols(path="tools")
    assert res["mode"] == "top"
    assert res["path"] == "tools"
    # 顶层应含 tools/a.py（rel 路径仍相对 workspace 根）
    assert "tools/a.py" in res["files"]
    # 不含 tools/nested/b.py（在 nested 子目录里）
    assert "tools/nested/b.py" not in res["files"]
    assert "nested" in res["subdirs"]


def test_workspace_symbols_top_skips_empty_subdirs(tmp_path):
    """无任何 .py 的子目录不进 subdirs（减小 prompt 噪音）"""
    _, tools = _setup_ws(tmp_path)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "readme.md").write_text("hi", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("def f(): pass\n", encoding="utf-8")

    res = tools.workspace_symbols()
    assert "src" in res["subdirs"]
    assert "docs" not in res["subdirs"]


def test_workspace_symbols_path_traversal_blocked(tmp_path):
    """path 越界被拦截"""
    _, tools = _setup_ws(tmp_path)
    res = tools.workspace_symbols(path="../evil")
    assert "error" in res
    assert res.get("error_kind") == "permission"


def test_workspace_symbols_path_not_found(tmp_path):
    """path 不存在返回 not_found"""
    _, tools = _setup_ws(tmp_path)
    res = tools.workspace_symbols(path="nonexistent")
    assert "error" in res
    assert res.get("error_kind") == "not_found"


def test_workspace_symbols_path_not_a_dir(tmp_path):
    """path 是文件不是目录 → invalid_args"""
    _, tools = _setup_ws(tmp_path)
    (tmp_path / "a.py").write_text("def f(): pass\n", encoding="utf-8")
    res = tools.workspace_symbols(path="a.py")
    assert "error" in res
    assert res.get("error_kind") == "invalid_args"


# ============= directory_summary =============

def test_directory_summary_basic(tmp_path):
    _, tools = _setup_ws(tmp_path)
    (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "agent.py").write_text("y = 2\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# hi", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[tool]\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()

    res = tools.directory_summary()
    assert res["path"] == "."
    assert res["file_count"] == 4  # main.py + agent.py + README.md + pyproject.toml
    assert res["subdir_count"] == 2
    assert res["by_extension"][".py"] == 2
    assert res["by_extension"][".md"] == 1
    assert "README.md" in res["key_files"]
    assert "pyproject.toml" in res["key_files"]
    assert "src/" in res["subdirs"]
    assert "tests/" in res["subdirs"]
    # files_sample 应含部分文件名
    assert "main.py" in res["files_sample"] or "agent.py" in res["files_sample"]


def test_directory_summary_with_path(tmp_path):
    _, tools = _setup_ws(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "src" / "b.py").write_text("y = 2\n", encoding="utf-8")

    res = tools.directory_summary(path="src")
    assert res["path"] == "src"
    assert res["file_count"] == 2
    assert res["by_extension"][".py"] == 2


def test_directory_summary_path_traversal_blocked(tmp_path):
    _, tools = _setup_ws(tmp_path)
    res = tools.directory_summary(path="../etc")
    assert "error" in res
    assert res.get("error_kind") == "permission"


def test_directory_summary_not_found(tmp_path):
    _, tools = _setup_ws(tmp_path)
    res = tools.directory_summary(path="nonexistent")
    assert "error" in res
    assert res.get("error_kind") == "not_found"


def test_directory_summary_not_a_dir(tmp_path):
    _, tools = _setup_ws(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    res = tools.directory_summary(path="a.py")
    assert "error" in res
    assert res.get("error_kind") == "invalid_args"


def test_directory_summary_files_sample_truncates(tmp_path):
    """文件多于 12 个时应截断并加 '... 还有 N 个文件' 提示"""
    _, tools = _setup_ws(tmp_path)
    for i in range(20):
        (tmp_path / f"f{i:02d}.py").write_text("x = 1\n", encoding="utf-8")
    res = tools.directory_summary()
    assert res["file_count"] == 20
    # 12 个文件名 + 1 个截断提示
    assert len(res["files_sample"]) == 13
    assert any("还有" in s for s in res["files_sample"])


def test_filter_tools_returns_only_readonly():
    import agent
    from tools_schema import READONLY_TOOL_NAMES
    filtered = agent._filter_tools(READONLY_TOOL_NAMES)
    names = {t["function"]["name"] for t in filtered}
    # 应该完全等于白名单（schema 中存在的工具）
    assert names <= READONLY_TOOL_NAMES
    # 关键写工具必须不在
    for forbidden in ("write_file", "replace_in_file", "execute_command",
                      "move_file", "apply_patch", "replace_symbol", "append_to_file"):
        assert forbidden not in names, f"{forbidden} 不应出现在只读白名单"
    # 关键读工具应在
    for must_have in ("read_file", "list_files", "workspace_symbols",
                      "search_in_files", "list_symbols"):
        assert must_have in names, f"{must_have} 应在只读白名单"


def test_dispatch_blocks_write_tool_in_audit_mode():
    import agent
    fake_tc = SimpleNamespace(
        id="call_1",
        function=SimpleNamespace(name="write_file", arguments='{"filename":"x.py","content":"x"}'),
    )
    out = agent._dispatch_tool_call(fake_tc, mode="audit", allow_hil=False, allow_confirm=False)
    assert "error" in out["result"]
    assert "audit 模式禁止" in out["result"]["error"]


def test_dispatch_allows_readonly_tool_in_audit_mode(tmp_path):
    """sanity check：审计模式下 read_file 这类只读工具应能正常分发"""
    _, tools = _setup_ws(tmp_path)
    (tmp_path / "z.py").write_text("hello", encoding="utf-8")
    import agent
    fake_tc = SimpleNamespace(
        id="call_2",
        function=SimpleNamespace(name="read_file", arguments='{"filename":"z.py"}'),
    )
    out = agent._dispatch_tool_call(fake_tc, mode="audit", allow_hil=False, allow_confirm=False)
    assert "error" not in out["result"], out
    assert out["result"].get("content") == "hello"


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
