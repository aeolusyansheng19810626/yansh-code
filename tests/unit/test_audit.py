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
    _, tools = _setup_ws(tmp_path)
    (tmp_path / "a.py").write_text(
        "def foo():\n    pass\n\nclass Bar:\n    def baz(self):\n        pass\n",
        encoding="utf-8",
    )
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.py").write_text("def hello():\n    pass\n", encoding="utf-8")
    # 非 .py 应被忽略
    (tmp_path / "readme.md").write_text("# hi", encoding="utf-8")

    res = tools.workspace_symbols()
    assert res["total_files"] == 2
    assert res["total_symbols"] == 4  # foo + Bar + baz + hello
    assert "a.py" in res["files"]
    assert "sub/b.py" in res["files"]
    names_a = [s["name"] for s in res["files"]["a.py"]]
    assert "foo" in names_a and "Bar" in names_a and "baz" in names_a


def test_workspace_symbols_cache_hits(tmp_path):
    _, tools = _setup_ws(tmp_path)
    (tmp_path / "x.py").write_text("def f():\n    pass\n", encoding="utf-8")

    tools.workspace_symbols()
    abs_x = (tmp_path / "x.py").resolve()
    assert str(abs_x) in tools._AST_CACHE
    mtime_first, syms_first = tools._AST_CACHE[str(abs_x)]

    # 第二次调用：mtime 不变 → 命中缓存（同一对象引用）
    tools.workspace_symbols()
    mtime_second, syms_second = tools._AST_CACHE[str(abs_x)]
    assert mtime_first == mtime_second
    assert syms_first is syms_second


def test_workspace_symbols_cache_invalidates_on_mtime(tmp_path):
    _, tools = _setup_ws(tmp_path)
    p = tmp_path / "y.py"
    p.write_text("def f():\n    pass\n", encoding="utf-8")
    tools.workspace_symbols()
    abs_p = str(p.resolve())
    syms_before = tools._AST_CACHE[abs_p][1]

    time.sleep(0.05)  # 确保 mtime 有变化
    p.write_text("def f():\n    pass\n\ndef g():\n    pass\n", encoding="utf-8")
    # 强制 mtime 推进（某些 FS 粒度 1s）
    new_mtime = time.time() + 1
    os.utime(p, (new_mtime, new_mtime))

    res = tools.workspace_symbols()
    syms_after = tools._AST_CACHE[abs_p][1]
    assert syms_before is not syms_after
    assert len(syms_after) == 2
    names = [s["name"] for s in res["files"]["y.py"]]
    assert "g" in names


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
