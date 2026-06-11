"""Fix A：_infer_test_scope 存在性过滤回归测试。
对应 gate churn 假阴性修复（2026-06-11）。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import agent
import config


def _setup_ws(tmp_path):
    config.set_workspace_dir(str(tmp_path))
    agent._reinit_paths()


# ── Fix A：已删文件不进 scope ──

def test_infer_test_scope_skips_gone_test_file(tmp_path):
    """台账记录了已删测试文件时，scope 不应含该路径（pytest collected 0 自造假阴性）。"""
    _setup_ws(tmp_path)
    # 只建 test_alpha.py，不建 test_gone.py
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_alpha.py").write_text("def test_ok(): pass\n", encoding="utf-8")

    plan_files = [
        {"filename": "tests/test_alpha.py"},
        {"filename": "tests/test_gone.py"},   # 文件不存在
    ]
    scope = agent._infer_test_scope(plan_files)

    assert "tests/test_alpha.py" in scope
    assert "tests/test_gone.py" not in scope


def test_infer_test_scope_includes_existing_test_file(tmp_path):
    """存在的测试文件仍正常加入 scope。"""
    _setup_ws(tmp_path)
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_beta.py").write_text("def test_ok(): pass\n", encoding="utf-8")

    plan_files = [{"filename": "tests/test_beta.py"}]
    scope = agent._infer_test_scope(plan_files)

    assert "tests/test_beta.py" in scope


# ── Fix A 不影响规则2（源文件 → 映射到 test_<stem>.py）──

def test_infer_test_scope_rule2_src_to_test_mapping(tmp_path):
    """源文件 src/foo.py → tests/test_foo.py 存在时，规则2映射仍正常命中。"""
    _setup_ws(tmp_path)
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_foo.py").write_text("def test_foo(): pass\n", encoding="utf-8")
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "foo.py").write_text("def foo(): pass\n", encoding="utf-8")

    plan_files = [{"filename": "src/foo.py"}]
    scope = agent._infer_test_scope(plan_files)

    assert "tests/test_foo.py" in scope


def test_infer_test_scope_rule2_missing_test_not_added(tmp_path):
    """源文件有对应测试文件但测试文件不存在时，不加入 scope（规则2存在性保证）。"""
    _setup_ws(tmp_path)
    # 不建 tests/test_bar.py
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "bar.py").write_text("def bar(): pass\n", encoding="utf-8")

    plan_files = [{"filename": "src/bar.py"}]
    scope = agent._infer_test_scope(plan_files)

    assert "tests/test_bar.py" not in scope
