"""项目类型检测 + Linter 执行（纯函数；project_type 由调用方传入）"""
import shutil
import json as _json
from pathlib import Path

import config as _cfg_mod
from tools import execute_command


def run_linter_for(project_type: str | None) -> dict | None:
    """运行 Linter，有错误返回结果 dict（同 execute_command 输出格式），否则返回 None"""
    if not project_type:
        return None

    cmd = None
    if project_type == "Python":
        if shutil.which("ruff"):
            cmd = "ruff check ."
        elif shutil.which("mypy"):
            cmd = "mypy ."
        else:
            import sys as _sys
            cmd = f'"{_sys.executable}" -m ruff check .'
    elif project_type == "Node.js":
        cmd = "npm run lint --if-present"
    elif project_type == "Go":
        if shutil.which("go"):
            cmd = "go vet ./..."
    elif project_type == "Rust":
        if shutil.which("cargo"):
            cmd = "cargo clippy"
    elif project_type == "Java/Maven":
        if shutil.which("mvn"):
            cmd = "mvn checkstyle:check"

    if not cmd:
        return None
    result = execute_command(cmd)
    if result.get("returncode", 0) == 0:
        return None
    return result


def detect_project_type():
    """扫描 workspace 识别项目类型，返回 (type_str, test_cmd)"""
    ws = Path(_cfg_mod.WORKSPACE_DIR)
    if not ws.exists():
        return None, None
    all_names = {f.name for f in ws.rglob("*") if f.is_file()}

    if any(n in all_names for n in ("requirements.txt", "pyproject.toml", "setup.py", "setup.cfg")) \
            or any(n.endswith(".py") for n in all_names):
        return "Python", _detect_python_test_cmd(ws)

    if "package.json" in all_names:
        return "Node.js", _detect_node_test_cmd(ws)

    if "go.mod" in all_names:
        return "Go", "go test ./..."

    if "Cargo.toml" in all_names:
        return "Rust", "cargo test"

    if "pom.xml" in all_names:
        return "Java/Maven", "mvn test"

    return None, None


def _detect_python_test_cmd(ws: Path) -> str:
    pyproject = ws / "pyproject.toml"
    if pyproject.exists():
        try:
            content = pyproject.read_text(encoding="utf-8")
            if "[tool.uv]" in content or (ws / "uv.lock").exists():
                if shutil.which("uv"):
                    return "uv run pytest"
            if "[tool.poetry]" in content or (ws / "poetry.lock").exists():
                if shutil.which("poetry"):
                    return "poetry run pytest"
            if "[tool.pytest" in content or "[pytest]" in content:
                return "pytest"
        except Exception:
            pass

    if (ws / "tox.ini").exists() and shutil.which("tox"):
        return "tox"

    makefile = ws / "Makefile"
    if makefile.exists():
        try:
            mk = makefile.read_text(encoding="utf-8", errors="replace")
            if "test:" in mk or "test :" in mk:
                return "make test"
        except Exception:
            pass

    if shutil.which("pytest"):
        return "pytest"
    return "python -m pytest"


def _detect_node_test_cmd(ws: Path) -> str:
    pkg = ws / "package.json"
    if pkg.exists():
        try:
            data = _json.loads(pkg.read_text(encoding="utf-8"))
            scripts = data.get("scripts", {})
            if scripts.get("test"):
                if (ws / "yarn.lock").exists() and shutil.which("yarn"):
                    return "yarn test"
                if (ws / "pnpm-lock.yaml").exists() and shutil.which("pnpm"):
                    return "pnpm test"
                return "npm test"
        except Exception:
            pass
    return "npm test"
