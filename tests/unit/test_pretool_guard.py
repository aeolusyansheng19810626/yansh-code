"""pre enforcement：内建 PreToolUse test-first 拦截（_pretool_test_first_guard）单元测试。"""
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import agent
import config


def _setup_pre(tmp_path, monkeypatch, max_block=3):
    config.set_workspace_dir(str(tmp_path))
    agent._reinit_paths()
    # 清空计数，防跨用例污染
    agent._pretool_block_counts.clear()
    monkeypatch.setitem(config._effective_config, "solo_test_enforcement", "pre")
    monkeypatch.setitem(config._effective_config, "solo_pretool_max_block", max_block)


# ── 基本 block / 放行 ──

def test_impl_no_test_blocks(tmp_path, monkeypatch):
    """无对应测试骨架时，写实现文件应被 block。"""
    _setup_pre(tmp_path, monkeypatch)
    r = agent._pretool_test_first_guard("write_file", {"filename": "lexer.py"})
    assert r is not None
    assert "_pretool_block" in r
    assert "test_lexer.py" in r["error"]


def test_impl_with_test_allows(tmp_path, monkeypatch):
    """tests/test_<stem>.py 存在时，写实现文件应放行。"""
    _setup_pre(tmp_path, monkeypatch)
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_lexer.py").write_text("def test_placeholder(): pass\n")
    r = agent._pretool_test_first_guard("write_file", {"filename": "lexer.py"})
    assert r is None


def test_impl_with_stem_test_allows(tmp_path, monkeypatch):
    """<stem>_test.py 形式的测试骨架同样算存在，应放行。"""
    _setup_pre(tmp_path, monkeypatch)
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "lexer_test.py").write_text("def test_placeholder(): pass\n")
    r = agent._pretool_test_first_guard("write_file", {"filename": "lexer.py"})
    assert r is None


def test_test_file_itself_allows(tmp_path, monkeypatch):
    """写测试文件本身（test_*.py）应放行，不自我拦截。"""
    _setup_pre(tmp_path, monkeypatch)
    r = agent._pretool_test_first_guard("write_file", {"filename": "tests/test_lexer.py"})
    assert r is None


def test_init_py_allows(tmp_path, monkeypatch):
    """__init__.py 豁免，不应被拦截。"""
    _setup_pre(tmp_path, monkeypatch)
    r = agent._pretool_test_first_guard("write_file", {"filename": "__init__.py"})
    assert r is None


def test_setup_py_allows(tmp_path, monkeypatch):
    """setup.py 豁免，不应被拦截。"""
    _setup_pre(tmp_path, monkeypatch)
    r = agent._pretool_test_first_guard("write_file", {"filename": "setup.py"})
    assert r is None


def test_non_py_allows(tmp_path, monkeypatch):
    """非 .py 文件（如 README.md）应放行。"""
    _setup_pre(tmp_path, monkeypatch)
    r = agent._pretool_test_first_guard("write_file", {"filename": "README.md"})
    assert r is None


def test_non_write_tool_allows(tmp_path, monkeypatch):
    """非写文件工具（如 execute_command）应放行。"""
    _setup_pre(tmp_path, monkeypatch)
    r = agent._pretool_test_first_guard("execute_command", {"command": "pytest"})
    assert r is None


# ── 路径字段：file_path vs filename ──

def test_apply_patch_file_path_field(tmp_path, monkeypatch):
    """apply_patch 用 file_path 字段，应正确 block。"""
    _setup_pre(tmp_path, monkeypatch)
    r = agent._pretool_test_first_guard("apply_patch", {"file_path": "parser.py"})
    assert r is not None
    assert "test_parser.py" in r["error"]


def test_replace_symbol_file_path_field(tmp_path, monkeypatch):
    """replace_symbol 用 file_path 字段，应正确 block。"""
    _setup_pre(tmp_path, monkeypatch)
    r = agent._pretool_test_first_guard("replace_symbol", {"file_path": "engine.py"})
    assert r is not None


# ── enforcement 模式 ──

def test_off_mode_returns_none(tmp_path, monkeypatch):
    """enforcement==off 时，guard 应直接放行（None）。"""
    _setup_pre(tmp_path, monkeypatch)
    monkeypatch.setitem(config._effective_config, "solo_test_enforcement", "off")
    r = agent._pretool_test_first_guard("write_file", {"filename": "lexer.py"})
    assert r is None


def test_gate_mode_returns_none(tmp_path, monkeypatch):
    """enforcement==gate 时，guard 不介入（None），由 gate 循环负责。"""
    _setup_pre(tmp_path, monkeypatch)
    monkeypatch.setitem(config._effective_config, "solo_test_enforcement", "gate")
    r = agent._pretool_test_first_guard("write_file", {"filename": "lexer.py"})
    assert r is None


# ── 兜底：连续达 max_block 后放行 ──

def test_max_block_releases(tmp_path, monkeypatch):
    """同一文件连续被拦截 max_block 次后，第 max_block+1 次应放行。"""
    _setup_pre(tmp_path, monkeypatch, max_block=2)
    # 第 1 次：block
    r1 = agent._pretool_test_first_guard("write_file", {"filename": "foo.py"})
    assert r1 is not None
    # 第 2 次：block
    r2 = agent._pretool_test_first_guard("write_file", {"filename": "foo.py"})
    assert r2 is not None
    # 第 3 次：达到上限，放行兜底
    r3 = agent._pretool_test_first_guard("write_file", {"filename": "foo.py"})
    assert r3 is None


def test_count_resets_after_test_created(tmp_path, monkeypatch):
    """agent 补建了测试骨架后，guard 不再 block，计数自然失效（骨架存在即放行）。"""
    _setup_pre(tmp_path, monkeypatch, max_block=5)
    # 先 block 一次
    r1 = agent._pretool_test_first_guard("write_file", {"filename": "bar.py"})
    assert r1 is not None
    # agent 补测试
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir(exist_ok=True)
    (tests_dir / "test_bar.py").write_text("def test_placeholder(): pass\n")
    # 现在放行
    r2 = agent._pretool_test_first_guard("write_file", {"filename": "bar.py"})
    assert r2 is None


def test_different_files_independent_counts(tmp_path, monkeypatch):
    """不同文件的 block 计数相互独立。"""
    _setup_pre(tmp_path, monkeypatch, max_block=1)
    r_a1 = agent._pretool_test_first_guard("write_file", {"filename": "a.py"})
    assert r_a1 is not None  # a.py 第1次 block
    r_b1 = agent._pretool_test_first_guard("write_file", {"filename": "b.py"})
    assert r_b1 is not None  # b.py 第1次 block（独立计数）
    r_a2 = agent._pretool_test_first_guard("write_file", {"filename": "a.py"})
    assert r_a2 is None  # a.py 达 max，放行
    r_b2 = agent._pretool_test_first_guard("write_file", {"filename": "b.py"})
    assert r_b2 is None  # b.py 达 max，放行


# ── dispatch 挂载验证 ──

def test_dispatch_block_returns_error_result(tmp_path, monkeypatch):
    """_dispatch_tool_call_with_hooks 在 pre 模式下 block 时，返回含 error 的 result。"""
    _setup_pre(tmp_path, monkeypatch)
    # 构造最小 tool_call
    tc = MagicMock()
    tc.id = "test-id"
    tc.function.name = "write_file"
    import json
    tc.function.arguments = json.dumps({"filename": "engine.py", "content": "x"})
    # patch _is_in_subagent 确保不跳过
    with patch.object(agent, "_is_in_subagent", return_value=False):
        out = agent._dispatch_tool_call_with_hooks(tc)
    assert out["result"].get("error") is not None
    assert "_pretool_block" in out["result"]


def test_dispatch_allows_in_subagent(tmp_path, monkeypatch):
    """子 agent 内 _is_in_subagent()==True 时，guard 应跳过（不 block）。"""
    _setup_pre(tmp_path, monkeypatch)
    tc = MagicMock()
    tc.id = "test-id"
    tc.function.name = "write_file"
    import json
    tc.function.arguments = json.dumps({"filename": "engine.py", "content": "x"})
    with patch.object(agent, "_is_in_subagent", return_value=True):
        # 在 subagent 内，guard 跳过，会落入正常 dispatch（可能因缺 ws 文件 error，但不是 _pretool_block）
        out = agent._dispatch_tool_call_with_hooks(tc)
    assert "_pretool_block" not in out.get("result", {})


# ── P0：block 标记 _pretool_block（不计入空转） ──

def test_block_result_has_pretool_block_marker(tmp_path, monkeypatch):
    """block result 必须含 _pretool_block=True，以便 no_progress 判断豁免。"""
    _setup_pre(tmp_path, monkeypatch)
    r = agent._pretool_test_first_guard("write_file", {"filename": "lexer.py"})
    assert r is not None
    assert r.get("_pretool_block") is True


# ── P1：apply_patch 缺 file_path 时从 patch_text 推断 ──

def test_apply_patch_without_file_path_infers_from_patch(tmp_path, monkeypatch):
    """apply_patch 缺 file_path 时，应从 patch_text 的 +++ b/... 行推断路径并 block。"""
    _setup_pre(tmp_path, monkeypatch)
    patch_text = "--- a/parser.py\n+++ b/parser.py\n@@ -1 +1 @@\n+x\n"
    r = agent._pretool_test_first_guard("apply_patch", {"patch_text": patch_text})
    assert r is not None
    assert "test_parser.py" in r["error"]


def test_apply_patch_with_file_path_blocks(tmp_path, monkeypatch):
    """apply_patch 有 file_path 时，正常 block 实现文件。"""
    _setup_pre(tmp_path, monkeypatch)
    r = agent._pretool_test_first_guard("apply_patch", {"file_path": "parser.py", "patch_text": ""})
    assert r is not None


def test_apply_patch_no_path_infer_allows(tmp_path, monkeypatch):
    """apply_patch patch_text 也推断不出路径时，放行（不崩溃）。"""
    _setup_pre(tmp_path, monkeypatch)
    r = agent._pretool_test_first_guard("apply_patch", {"patch_text": "no plus plus plus line"})
    assert r is None


# ── P2：move_file dst 覆盖 ──

def test_move_file_dst_impl_blocks(tmp_path, monkeypatch):
    """move_file 目的端是实现文件时应 block。"""
    _setup_pre(tmp_path, monkeypatch)
    r = agent._pretool_test_first_guard("move_file", {"src": "tmp_lexer.py", "dst": "lexer.py"})
    assert r is not None


def test_move_file_dst_test_allows(tmp_path, monkeypatch):
    """move_file 目的端是测试文件应放行。"""
    _setup_pre(tmp_path, monkeypatch)
    r = agent._pretool_test_first_guard("move_file", {"src": "tmp.py", "dst": "tests/test_lexer.py"})
    assert r is None


# ── 豁免补全：conftest.py 和嵌套路径 ──

def test_conftest_py_allows(tmp_path, monkeypatch):
    """conftest.py 豁免，不应被拦截。"""
    _setup_pre(tmp_path, monkeypatch)
    r = agent._pretool_test_first_guard("write_file", {"filename": "conftest.py"})
    assert r is None


def test_nested_impl_file_blocks(tmp_path, monkeypatch):
    """src/pkg/lexer.py 这类嵌套路径的实现文件应 block。"""
    _setup_pre(tmp_path, monkeypatch)
    r = agent._pretool_test_first_guard("write_file", {"filename": "src/pkg/lexer.py"})
    assert r is not None


def test_nested_impl_with_nested_test_allows(tmp_path, monkeypatch):
    """tests/ 下任意层级的 test_lexer.py 都算存在（rglob），应放行。"""
    _setup_pre(tmp_path, monkeypatch)
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "unit" / "test_lexer.py").write_text("def test_x(): pass\n")
    r = agent._pretool_test_first_guard("write_file", {"filename": "src/pkg/lexer.py"})
    assert r is None
