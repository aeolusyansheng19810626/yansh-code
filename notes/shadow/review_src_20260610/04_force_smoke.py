def _force_include_smoke(scope: list, ws) -> list:
    """P0-4：若 ws 存在 tests/test_smoke.py 且 scope 未含，强制并入。
    两路共用（plan 路径 _apply_test_scope_override + solo gate）。
    返回新列表（不原地改传入参数）。"""
    smoke = "tests/test_smoke.py"
    if (Path(ws) / "tests" / "test_smoke.py").is_file() and smoke not in scope:
        scope = list(scope) + [smoke]
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
