"""Unit tests for #9: 测试命令自动发现（_detect_python_test_cmd / _detect_node_test_cmd）"""
import os
import sys
import json
import shutil
import tempfile
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from agent import _detect_python_test_cmd, _detect_node_test_cmd
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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])