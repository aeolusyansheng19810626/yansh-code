# 补充08: gate scope 数据流链 — 钉 gate 是否落空
# agent.py 1482-1565: _infer_test_scope + _apply_test_scope_override
def _infer_test_scope(plan_files, exclude: set | None = None) -> list[str]:
    """P1.3：根据 plan 列出的修改文件推断本次任务相关的测试文件路径列表。

    规则：
      - 修改的源文件 X.py（非 test_*.py）→ 找 tests/ 全树下同名 test_<basename>.py
      - 修改的文件本身就是 test_*.py / *_test.py → 直接加进 scope
      - 找不到任何对应测试 → 返回 []（调用方应回退到全套）
      - exclude：越权新建的测试文件集合（posix 相对路径），从 scope 中排除

    返回的路径相对 workspace（pytest 原样接受）。
    """
    if not plan_files:
        return []
    ws = Path(_get_workspace())
    tests_root = ws / "tests"

    # 收集 plan 里所有非空 filename
    filenames = []
    for f in plan_files:
        fn = f.get("filename") if isinstance(f, dict) else f
        if fn and isinstance(fn, str):
            filenames.append(fn)
    if not filenames:
        return []

    scope: list[str] = []
    seen: set[str] = set()

    # 预扫 tests/ 下的所有 test_*.py 加速查找（按 stem 索引）
    test_files_by_stem: dict[str, list[str]] = {}
    if tests_root.is_dir():
        for p in tests_root.rglob("test_*.py"):
            rel = p.relative_to(ws).as_posix()
            test_files_by_stem.setdefault(p.stem, []).append(rel)

    for fn in filenames:
        bn = Path(fn).name  # e.g. "tools.py", "test_tools.py"
        stem = Path(fn).stem
        # 1. 已经是测试文件 → 直接加
        if bn.startswith("test_") or bn.endswith("_test.py"):
            rel = Path(fn).as_posix()
            if rel not in seen:
                scope.append(rel)
                seen.add(rel)
            continue
        # 2. 源文件 → 找同名 test_<stem>.py（越权新建的测试文件在此处排除）
        target_stem = f"test_{stem}"
        for rel in test_files_by_stem.get(target_stem, []):
            if exclude and rel in exclude:
                continue  # 越权新建，跳过
            if rel not in seen:
                scope.append(rel)
                seen.add(rel)
    return scope


def _apply_test_scope_override(plan_result: dict, exclude: set | None = None) -> None:
    """P1.3：原地重写 plan_result['test_command']——基于 plan_result['files'] 推断
    相关测试 scope，命中后用 _detect_python_test_cmd(scope=...) 重新构造命令。

    exclude：越权新建的测试文件集合，传入后从 scope 中排除。

    跳过覆盖的情况：
      - LLM 给的 test_command 不是 pytest 系（如 make test / tox / 自定义脚本）
      - scope 推断为空（找不到任何对应测试 → 保留 LLM 原命令以免误删）
    """
    scope = _infer_test_scope(plan_result.get("files", []), exclude=exclude)
    orig_cmd = (plan_result.get("test_command") or "").strip()
    # R6 保险丝：端到端 smoke test 是发现 CLI 调用链断裂的唯一信号，必须每轮运行。
    # 若 ws 存在 tests/test_smoke.py 但 scope 未含（architect 漏列 / scope 收窄），强制并入。
    smoke_rel = "tests/test_smoke.py"
    if (Path(_get_workspace()) / "tests" / "test_smoke.py").is_file() and smoke_rel not in scope:
        scope.append(smoke_rel)
    if not scope or not orig_cmd or "pytest" not in orig_cmd:
        return
    scoped_cmd = _detect_python_test_cmd(Path(_get_workspace()), scope=scope)
    if "pytest" not in scoped_cmd:
        return  # 防御：detect 落到了 tox / make 路径
    console.print(
        f"[scope] 推断 {len(scope)} 个相关测试，覆盖 test_command: {scoped_cmd}",
        highlight=False,
    )
    plan_result["test_command"] = scoped_cmd


# linter.py 77-138: _detect_python_test_cmd
def _detect_python_test_cmd(ws: Path, scope: list[str] | None = None) -> str:
    """Return the test command for a Python project.

    P1.3: when `scope` is a non-empty list of test file paths, return a pytest
    command that targets only those files (e.g. `pytest tests/unit/test_tools.py`).
    `scope=None` keeps the original behaviour (run the full suite).
    `scope=[]` is treated the same as None (no inferred scope → fall back to full).
    Tox / make-test / poetry-run / uv-run runners are unchanged when scope is set —
    they still use full suite because their wrappers don't accept arbitrary args
    and the cost of mis-routing is higher than the cost of running full suite.
    """
    scope_arg = " " + " ".join(scope) if scope else ""

    pyproject = ws / "pyproject.toml"
    if pyproject.exists():
        try:
            content = pyproject.read_text(encoding="utf-8")
            if "[tool.uv]" in content or (ws / "uv.lock").exists():
                if shutil.which("uv"):
                    return "uv run pytest" + scope_arg
            if "[tool.poetry]" in content or (ws / "poetry.lock").exists():
                if shutil.which("poetry"):
                    return "poetry run pytest" + scope_arg
            if "[tool.pytest" in content or "[pytest]" in content:
                return "pytest" + scope_arg
        except Exception:
            pass

    if (ws / "tox.ini").exists() and shutil.which("tox"):
        # tox 包装器对参数不敏感且与 testenv 配置耦合，scope 不注入
        return "tox"

    makefile = ws / "Makefile"
    if makefile.exists():
        try:
            mk = makefile.read_text(encoding="utf-8", errors="replace")
            if "test:" in mk or "test :" in mk:
                # make test 同样是包装器，scope 不注入
                return "make test"
        except Exception:
            pass

    if shutil.which("pytest"):
        return "pytest" + scope_arg
    return "python -m pytest" + scope_arg


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

# task_log.py 173-210: snapshot_files_modified
def snapshot_files_modified() -> list:
    """返回 _task_files_modified 的快照副本（线程安全）"""
    with _log_lock:
        return list(_task_files_modified)


def snapshot_tool_calls() -> list:
    """返回 _task_tool_calls 的快照副本（线程安全）"""
    with _log_lock:
        return list(_task_tool_calls)
