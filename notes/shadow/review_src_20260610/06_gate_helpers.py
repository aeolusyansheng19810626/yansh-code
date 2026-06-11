def test(test_command, timeout_sec=None):
    """执行测试命令。timeout_sec：None 则用配置 test_gate_timeout_sec（默认 300s）。
    注意：agent 主循环内自己跑的 execute_command 仍走默认 30s，互不干扰。"""
    if not test_command or not test_command.strip():
        console.print("警告：无测试命令，跳过测试")
        return {"returncode": 0, "stdout": "", "stderr": ""}
    _timeout = timeout_sec or int(_cfg("test_gate_timeout_sec") or 300)
    console.print(f"执行测试：{test_command}（超时 {_timeout}s）")
    return execute_command(test_command, _timeout_sec=_timeout)


def judge(test_result):
    """判断测试是否通过"""
    return test_result.get("returncode") == 0


def _classify_test_failure(tr: dict) -> tuple:
    """P0-2：把测试失败分三类，回灌时让 agent 知道该怎么处理。返回 (kind, hint)。"""
    if tr.get("error_kind") == "timeout":
        return ("timeout", "测试被超时强杀——可能用例太慢或死循环/死锁，不要当成断言失败盲改业务逻辑；考虑缩小本轮 scope 或排查阻塞点。")
    out = (tr.get("stderr") or "") + (tr.get("stdout") or "")
    rc = tr.get("returncode")
    if rc == 2 or "ModuleNotFoundError" in out or "ImportError" in out or "collected 0 items" in out:
        return ("uncollectable", "测试无法收集/运行（导入或崩溃），先修可运行性与 import 链，再谈断言。")
    return ("assertion", "断言失败，正常定位修复。")


def _clip(s: str, head: int = 1500, tail: int = 2000) -> str:
    """P0-5：保头尾截断，防回灌总量过大。"""
    if len(s) <= head + tail:
        return s
    return s[:head] + f"\n[... {len(s)-head-tail} chars omitted ...]\n" + s[-tail:]


def _build_gate_feedback(test_cmd: str, tr: dict, kind: str, hint: str) -> str:
    """P0-5：构造确定性 gate 回灌 payload。stdout/stderr 两路都给，不二选一；保头尾截断。"""
    rc = tr.get("returncode")
    stdout = tr.get("stdout") or ""
    stderr = tr.get("stderr") or ""
    # pytest 失败详情常在 stdout；运行时 traceback 常在 stderr。两路都给，不二选一。
    parts = [f"外部 test gate 运行 `{test_cmd}` 失败（returncode={rc}，类型={kind}）。", hint]
    if stdout.strip():
        parts.append("---- STDOUT（截断保头尾）----\n" + _clip(stdout))
    if stderr.strip():
        parts.append("---- STDERR（截断保头尾）----\n" + _clip(stderr))
    if not stdout.strip() and not stderr.strip():
        parts.append("（测试无输出）")
    parts.append("请在当前 context 内定位并修复，跑绿后再 task_complete。禁止弱化断言来骗过测试。")
    return "\n".join(parts)


def _parse_pytest_failures(text: str) -> set:
    """从 pytest 输出抽 FAILED 行的 test id（形如 'tests/x.py::test_y'）。
    pytest 短摘要行格式：`FAILED <nodeid> - <msg...>` 或 `FAILED <nodeid>`。
    解析失败返回空集，不抛异常。"""
    if not text:
        return set()
    import re as _re
    out = set()
    # 容忍 colorize / 行首空格；匹配到 ' - ' 或行尾结束
    for m in _re.finditer(r"^FAILED\s+(\S+?)(?:\s+-\s|$)", text, flags=_re.MULTILINE):
        out.add(m.group(1))
    return out


def _capture_baseline_failures(test_command: str) -> set:
    """code() 前跑一次 test_command 捕获 baseline failures。
    best-effort：任何异常返回 empty set，不影响主流程。"""
    if not test_command or not test_command.strip():
        return set()
