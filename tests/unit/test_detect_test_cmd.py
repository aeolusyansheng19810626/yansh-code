"""Unit tests for #9: 测试命令自动发现（_detect_python_test_cmd / _detect_node_test_cmd）"""
import os
import sys
import json
import shutil
import tempfile
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from linter import _detect_python_test_cmd, _detect_node_test_cmd
from pathlib import Path


@pytest.fixture
def tmp_ws(tmp_path):
    """每个测试用独立临时目录作为 workspace"""
    return tmp_path


# ── Python 测试命令检测 ──────────────────────────────────────────────────────


def test_python_uv_lock(tmp_ws):
    """有 uv.lock 且 uv 在 PATH 中 → uv run pytest；无 uv 则退化"""
    (tmp_ws / "uv.lock").write_text("")
    cmd = _detect_python_test_cmd(tmp_ws)
    # uv 不一定在 CI 环境，只验证是 str
    assert isinstance(cmd, str)
    assert "pytest" in cmd


def test_python_poetry_lock(tmp_ws):
    """有 poetry.lock 且 poetry 在 PATH → poetry run pytest；无则退化"""
    (tmp_ws / "poetry.lock").write_text("")
    cmd = _detect_python_test_cmd(tmp_ws)
    assert isinstance(cmd, str)
    assert "pytest" in cmd


def test_python_pyproject_with_pytest_section(tmp_ws):
    """pyproject.toml 含 [tool.pytest 配置 → pytest"""
    (tmp_ws / "pyproject.toml").write_text("[tool.pytest.ini_options]\ntestpaths = [\"tests\"]\n")
    cmd = _detect_python_test_cmd(tmp_ws)
    assert cmd == "pytest"


def test_python_pyproject_pytest_section_variant(tmp_ws):
    """pyproject.toml 含 [pytest] → pytest"""
    (tmp_ws / "pyproject.toml").write_text("[pytest]\naddopts = -v\n")
    cmd = _detect_python_test_cmd(tmp_ws)
    assert cmd == "pytest"


def test_python_tox_ini(tmp_ws, monkeypatch):
    """有 tox.ini 且 tox 可用 → tox；tox 不可用则退化"""
    (tmp_ws / "tox.ini").write_text("[tox]\n")
    cmd = _detect_python_test_cmd(tmp_ws)
    assert isinstance(cmd, str)
    # tox 不一定装了，只验证格式
    assert "pytest" in cmd or cmd == "tox"


def test_python_makefile_with_test_target(tmp_ws):
    """Makefile 有 test: target → make test"""
    (tmp_ws / "Makefile").write_text("test:\n\tpytest tests/\n")
    cmd = _detect_python_test_cmd(tmp_ws)
    assert cmd == "make test"


def test_python_default_fallback(tmp_ws):
    """没有任何配置文件 → pytest 或 python -m pytest"""
    cmd = _detect_python_test_cmd(tmp_ws)
    assert "pytest" in cmd


def test_python_empty_pyproject(tmp_ws):
    """pyproject.toml 存在但无 pytest 配置 → 退化到默认"""
    (tmp_ws / "pyproject.toml").write_text("[tool.black]\nline-length = 88\n")
    cmd = _detect_python_test_cmd(tmp_ws)
    assert "pytest" in cmd


# ── Node.js 测试命令检测 ─────────────────────────────────────────────────────


def test_node_npm_test(tmp_ws):
    """package.json 有 scripts.test → npm test"""
    pkg = {"scripts": {"test": "jest"}}
    (tmp_ws / "package.json").write_text(json.dumps(pkg))
    cmd = _detect_node_test_cmd(tmp_ws)
    assert cmd == "npm test"


def test_node_yarn_lock(tmp_ws, monkeypatch):
    """有 yarn.lock 且 yarn 可用 → yarn test"""
    pkg = {"scripts": {"test": "jest"}}
    (tmp_ws / "package.json").write_text(json.dumps(pkg))
    (tmp_ws / "yarn.lock").write_text("")
    # yarn 不一定装了，只验证包含 test
    cmd = _detect_node_test_cmd(tmp_ws)
    assert "test" in cmd


def test_node_pnpm_lock(tmp_ws):
    """有 pnpm-lock.yaml 且 pnpm 可用 → pnpm test"""
    pkg = {"scripts": {"test": "vitest"}}
    (tmp_ws / "package.json").write_text(json.dumps(pkg))
    (tmp_ws / "pnpm-lock.yaml").write_text("")
    cmd = _detect_node_test_cmd(tmp_ws)
    assert "test" in cmd


def test_node_no_test_script(tmp_ws):
    """package.json 无 scripts.test → npm test"""
    pkg = {"scripts": {"build": "tsc"}}
    (tmp_ws / "package.json").write_text(json.dumps(pkg))
    cmd = _detect_node_test_cmd(tmp_ws)
    assert cmd == "npm test"


def test_node_no_package_json(tmp_ws):
    """无 package.json → npm test"""
    cmd = _detect_node_test_cmd(tmp_ws)
    assert cmd == "npm test"


def test_node_malformed_package_json(tmp_ws):
    """package.json 损坏 → 静默回退 npm test"""
    (tmp_ws / "package.json").write_text("{ broken json }")
    cmd = _detect_node_test_cmd(tmp_ws)
    assert cmd == "npm test"


# ── P1.3: scope 参数 ─────────────────────────────────────────────────────────


def test_python_scope_appends_files(tmp_ws):
    """scope=['tests/unit/test_a.py'] → pytest 后接路径"""
    (tmp_ws / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    cmd = _detect_python_test_cmd(tmp_ws, scope=["tests/unit/test_a.py"])
    assert cmd == "pytest tests/unit/test_a.py"


def test_python_scope_multiple_files_joined_with_space(tmp_ws):
    """scope=['a.py', 'b.py'] → 单个 pytest 跑多个文件"""
    (tmp_ws / "pyproject.toml").write_text("[pytest]\n")
    cmd = _detect_python_test_cmd(tmp_ws, scope=["tests/test_a.py", "tests/test_b.py"])
    assert cmd == "pytest tests/test_a.py tests/test_b.py"


def test_python_scope_none_keeps_full_suite(tmp_ws):
    """scope=None（默认）→ 行为不变，不带任何文件参数"""
    (tmp_ws / "pyproject.toml").write_text("[pytest]\n")
    cmd = _detect_python_test_cmd(tmp_ws)
    assert cmd == "pytest"


def test_python_scope_empty_list_keeps_full_suite(tmp_ws):
    """scope=[] 视同 None → pytest 全套"""
    (tmp_ws / "pyproject.toml").write_text("[pytest]\n")
    cmd = _detect_python_test_cmd(tmp_ws, scope=[])
    assert cmd == "pytest"


def test_python_scope_ignored_for_tox(tmp_ws):
    """tox 走包装器，scope 不应注入"""
    if not shutil.which("tox"):
        pytest.skip("tox not in PATH")
    (tmp_ws / "tox.ini").write_text("[testenv]\ndeps=pytest\ncommands=pytest\n")
    cmd = _detect_python_test_cmd(tmp_ws, scope=["tests/test_a.py"])
    assert cmd == "tox"  # scope 不附加


def test_python_scope_ignored_for_make_test(tmp_ws):
    """make test 走包装器，scope 不应注入"""
    (tmp_ws / "Makefile").write_text("test:\n\tpython -m pytest\n")
    # 让 detect 不命中 pyproject.toml 路径
    cmd = _detect_python_test_cmd(tmp_ws, scope=["tests/test_a.py"])
    assert cmd == "make test"


def test_python_scope_works_with_uv(tmp_ws):
    """有 uv.lock 且 scope 给定 → uv run pytest <scope>（如果 uv 在 PATH）"""
    if not shutil.which("uv"):
        pytest.skip("uv not in PATH")
    (tmp_ws / "uv.lock").write_text("")
    cmd = _detect_python_test_cmd(tmp_ws, scope=["tests/test_a.py"])
    assert cmd == "uv run pytest tests/test_a.py"


# ── P1.3: agent._infer_test_scope ───────────────────────────────────────────


def test_infer_test_scope_finds_corresponding_test(tmp_path, monkeypatch):
    """改 tools.py → 找到 tests/unit/test_tools.py（同 stem）"""
    import agent
    import config as _cfg
    monkeypatch.setattr(_cfg, "WORKSPACE_DIR", str(tmp_path))
    (tmp_path / "tools.py").write_text("def foo(): pass\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "unit").mkdir()
    (tmp_path / "tests" / "unit" / "test_tools.py").write_text("def test_foo(): pass\n")
    plan_files = [{"filename": "tools.py", "description": "add foo"}]
    scope = agent._infer_test_scope(plan_files)
    assert scope == ["tests/unit/test_tools.py"]


def test_infer_test_scope_returns_empty_when_no_match(tmp_path, monkeypatch):
    """改一个文件，但 tests/ 下没对应的 → 返回 []"""
    import agent
    import config as _cfg
    monkeypatch.setattr(_cfg, "WORKSPACE_DIR", str(tmp_path))
    (tmp_path / "obscure.py").write_text("x=1\n")
    (tmp_path / "tests").mkdir()
    plan_files = [{"filename": "obscure.py", "description": "..."}]
    assert agent._infer_test_scope(plan_files) == []


def test_infer_test_scope_test_file_passes_through(tmp_path, monkeypatch):
    """改 tests/test_x.py 本身 → 直接进 scope"""
    import agent
    import config as _cfg
    monkeypatch.setattr(_cfg, "WORKSPACE_DIR", str(tmp_path))
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("")
    plan_files = [{"filename": "tests/test_x.py", "description": "..."}]
    scope = agent._infer_test_scope(plan_files)
    assert "tests/test_x.py" in scope


def test_infer_test_scope_dedupe_when_src_and_test_both_in_plan(tmp_path, monkeypatch):
    """plan 里同时有 tools.py + tests/test_tools.py → scope 只出现一次"""
    import agent
    import config as _cfg
    monkeypatch.setattr(_cfg, "WORKSPACE_DIR", str(tmp_path))
    (tmp_path / "tools.py").write_text("")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_tools.py").write_text("")
    plan_files = [
        {"filename": "tools.py", "description": "..."},
        {"filename": "tests/test_tools.py", "description": "..."},
    ]
    scope = agent._infer_test_scope(plan_files)
    assert scope.count("tests/test_tools.py") == 1


def test_infer_test_scope_empty_plan_returns_empty(tmp_path, monkeypatch):
    import agent
    import config as _cfg
    monkeypatch.setattr(_cfg, "WORKSPACE_DIR", str(tmp_path))
    assert agent._infer_test_scope([]) == []


# ── P1.3: agent._apply_test_scope_override ──────────────────────────────────


def test_apply_test_scope_override_pytest_command_rewritten(tmp_path, monkeypatch):
    """LLM plan 给 'pytest' 全套 + 改了 tools.py → 重写为 pytest tests/unit/test_tools.py"""
    import agent
    import config as _cfg
    monkeypatch.setattr(_cfg, "WORKSPACE_DIR", str(tmp_path))
    (tmp_path / "pyproject.toml").write_text("[pytest]\n")
    (tmp_path / "tools.py").write_text("")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "unit").mkdir()
    (tmp_path / "tests" / "unit" / "test_tools.py").write_text("")
    plan_result = {
        "files": [{"filename": "tools.py", "description": "..."}],
        "test_command": "pytest",
    }
    agent._apply_test_scope_override(plan_result)
    assert plan_result["test_command"] == "pytest tests/unit/test_tools.py"


def test_apply_test_scope_override_skip_non_pytest(tmp_path, monkeypatch):
    """LLM plan 给 'make test' → 不应被 P1.3 覆盖（包装器尊重 LLM 意图）"""
    import agent
    import config as _cfg
    monkeypatch.setattr(_cfg, "WORKSPACE_DIR", str(tmp_path))
    (tmp_path / "tools.py").write_text("")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_tools.py").write_text("")
    plan_result = {
        "files": [{"filename": "tools.py", "description": "..."}],
        "test_command": "make test",
    }
    agent._apply_test_scope_override(plan_result)
    assert plan_result["test_command"] == "make test"  # 未变


def test_apply_test_scope_override_skip_empty_scope(tmp_path, monkeypatch):
    """plan 里改 obscure.py 但没对应测试 → 不重写"""
    import agent
    import config as _cfg
    monkeypatch.setattr(_cfg, "WORKSPACE_DIR", str(tmp_path))
    (tmp_path / "pyproject.toml").write_text("[pytest]\n")
    (tmp_path / "obscure.py").write_text("")
    (tmp_path / "tests").mkdir()
    plan_result = {
        "files": [{"filename": "obscure.py", "description": "..."}],
        "test_command": "pytest",
    }
    agent._apply_test_scope_override(plan_result)
    assert plan_result["test_command"] == "pytest"  # 未变


def test_apply_test_scope_override_skip_empty_command(tmp_path, monkeypatch):
    """LLM 给空 test_command → 不重写"""
    import agent
    import config as _cfg
    monkeypatch.setattr(_cfg, "WORKSPACE_DIR", str(tmp_path))
    (tmp_path / "tools.py").write_text("")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_tools.py").write_text("")
    plan_result = {
        "files": [{"filename": "tools.py", "description": "..."}],
        "test_command": "",
    }
    agent._apply_test_scope_override(plan_result)
    assert plan_result["test_command"] == ""


if __name__ == "__main__":
    pytest.main([__file__, "-v"])