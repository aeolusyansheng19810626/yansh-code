"""
Integration tests for yansh-code
运行方式：从项目根目录执行 python tests/test_integration.py
"""

import sys
import os
import json
import subprocess
import shutil
from pathlib import Path
from unittest.mock import MagicMock

# 确保工作目录是项目根目录
ROOT = Path(__file__).parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

import agent
import tools
from agent import (
    set_batch_mode, run, save_history, load_history,
    add_to_history, maybe_compress_history,
    create_snapshot, restore_snapshot, cleanup_snapshot,
    init_task_log, finish_task_log, get_last_task_log,
)
from tools import execute_command, read_file, write_file, replace_symbol, list_symbols
from config import load_project_config, override_config, get_config

WORKSPACE = ROOT / "workspace"
PASS_COUNT = 0
FAIL_COUNT = 0
RESULTS = []


def report(name: str, ok: bool, reason: str = ""):
    global PASS_COUNT, FAIL_COUNT
    if ok:
        print(f"[PASS] {name}")
        PASS_COUNT += 1
    else:
        print(f"[FAIL: {reason}] {name}")
        FAIL_COUNT += 1
    RESULTS.append((name, ok, reason))


def _backup_history():
    hist = agent._HISTORY_FILE
    return (hist, hist.read_bytes() if hist.exists() else None)


def _restore_history(backup):
    hist_path, data = backup
    agent.conversation_history = []
    if data is not None:
        hist_path.write_bytes(data)
    elif hist_path.exists():
        hist_path.unlink()


def cleanup_workspace():
    """每个场景开始前清理 workspace 中测试产生的 .py 文件（保留 .yansh/ 目录结构）"""
    # workspace 根目录的 .py 文件
    for f in WORKSPACE.glob("*.py"):
        f.unlink()
    # workspace/tests/ 下的 .py 文件
    tests_dir = WORKSPACE / "tests"
    if tests_dir.exists():
        for f in tests_dir.glob("*.py"):
            f.unlink()


# ── 初始化 ────────────────────────────────────────────────────────────────────
print("=" * 60)
print("yansh-code Integration Tests")
print("=" * 60)

load_project_config()
# 所有 agent.run() 调用均使用批处理模式（自动确认），console 保持 stdout
set_batch_mode(True, json_output=False)


# ─── 场景1: auto 模式创建新文件 ───────────────────────────────────────────────
print("\n=== 场景1: auto模式创建新文件 ===")
cleanup_workspace()
hello_path = WORKSPACE / "hello.py"

try:
    result = run("创建hello.py打印hello", mode="auto")
    exists = hello_path.exists()
    if not exists:
        report("场景1-auto创建文件", False, "hello.py 未创建")
    else:
        content = hello_path.read_text(encoding="utf-8")
        has_print = "print" in content.lower()
        report("场景1-auto创建文件", has_print,
               "文件存在但不含 print" if not has_print else "")
except Exception as e:
    report("场景1-auto创建文件", False, str(e)[:120])


# ─── 场景2: plan 模式只出计划 ─────────────────────────────────────────────────
print("\n=== 场景2: plan模式只出计划 ===")
cleanup_workspace()
plan_path = WORKSPACE / "plan_test.py"

try:
    result = run("创建plan_test.py", mode="plan")
    success = result.get("success", False)
    not_created = not plan_path.exists()

    log = get_last_task_log()
    has_plan = bool(log.get("plan"))

    if not success:
        report("场景2-plan返回success", False, "success=False")
    elif not not_created:
        report("场景2-plan不创建文件", False, "plan_test.py 被意外创建")
    else:
        report("场景2-plan模式", has_plan or success,
               "计划字段为空" if not has_plan else "")
except Exception as e:
    report("场景2-plan模式", False, str(e)[:120])


# ─── 场景3: code 模式跳过确认 ────────────────────────────────────────────────
print("\n=== 场景3: code模式跳过确认 ===")
cleanup_workspace()
code_path = WORKSPACE / "code_test.py"

try:
    result = run("创建code_test.py打印ok", mode="code")
    exists = code_path.exists()
    report("场景3-code模式创建文件", exists,
           "code_test.py 未创建" if not exists else "")
except Exception as e:
    report("场景3-code模式", False, str(e)[:120])


# ─── 场景4: 危险命令拦截 ──────────────────────────────────────────────────────
print("\n=== 场景4: 危险命令拦截 ===")
cleanup_workspace()
try:
    result = execute_command("rm -rf /")
    has_error = "error" in result
    if not has_error:
        report("场景4-危险命令拦截", False, f"未返回 error 字段: {result}")
    else:
        has_block = "安全拦截" in result["error"]
        report("场景4-危险命令拦截", has_block,
               f"error 不含'安全拦截': {result['error'][:80]}" if not has_block else "")
except Exception as e:
    report("场景4-危险命令拦截", False, str(e)[:120])


# ─── 场景5: 路径越界拦截 ──────────────────────────────────────────────────────
print("\n=== 场景5: 路径越界拦截 ===")
cleanup_workspace()
try:
    result = read_file("../secret.txt")
    has_error = "error" in result
    report("场景5-路径越界拦截", has_error,
           f"未返回 error: {result}" if not has_error else "")
except Exception as e:
    report("场景5-路径越界拦截", False, str(e)[:120])


# ─── 场景6: replace_symbol 替换函数 ──────────────────────────────────────────
print("\n=== 场景6: replace_symbol替换函数 ===")
cleanup_workspace()
target_path = WORKSPACE / "target.py"
try:
    write_file("target.py",
               "def my_func():\n    return 0\n\ndef other_func():\n    pass\n")

    result = replace_symbol(
        "my_func",
        "def my_func():\n    return 42",
        "target.py",
    )
    if "error" in result:
        report("场景6-replace_symbol", False, result["error"][:80])
    else:
        content = target_path.read_text(encoding="utf-8")
        has_42 = "return 42" in content
        has_other = "def other_func" in content
        ok = has_42 and has_other
        reason = ""
        if not has_42:
            reason = "return 42 未写入"
        elif not has_other:
            reason = "other_func() 被意外删除"
        report("场景6-replace_symbol", ok, reason)
except Exception as e:
    report("场景6-replace_symbol", False, str(e)[:120])
finally:
    if target_path.exists():
        target_path.unlink()


# ─── 场景7: 自动压缩触发 ──────────────────────────────────────────────────────
print("\n=== 场景7: 自动压缩触发 ===")
cleanup_workspace()
history_backup = _backup_history()
original_history = agent.conversation_history[:]

# 用 mock 替代 LLM 压缩调用，专注测试压缩逻辑本身
original_create = agent.client.chat.completions.create
try:
    mock_summary = (
        "【已完成任务】\n- mock压缩测试\n\n"
        "【关键文件】\n- mock.py\n\n"
        "【未解决问题】\n- 无"
    )
    mock_msg = MagicMock()
    mock_msg.content = mock_summary
    mock_choice = MagicMock()
    mock_choice.message = mock_msg
    mock_resp = MagicMock()
    mock_resp.choices = [mock_choice]
    agent.client.chat.completions.create = MagicMock(return_value=mock_resp)

    agent.conversation_history = []
    # 降低阈值，减少每轮保留数量
    override_config(compress_threshold=300, keep_recent_turns=1)

    # 写入 4 轮，总字符超过 300
    for i in range(4):
        add_to_history(
            f"用户消息{i}: " + "测试内容" * 10,
            f"助手回复{i}: " + "测试内容" * 10,
        )
    total_before = sum(len(m["content"]) for m in agent.conversation_history)
    len_before = len(agent.conversation_history)

    maybe_compress_history()

    len_after = len(agent.conversation_history)
    compressed = len_after < len_before
    has_summary = any(
        "【已完成任务】" in m.get("content", "")
        for m in agent.conversation_history
    )
    ok = compressed and has_summary
    report("场景7-自动压缩触发", ok,
           f"压缩前{len_before}条→后{len_after}条 total={total_before}chars has_summary={has_summary}"
           if not ok else "")
except Exception as e:
    report("场景7-自动压缩触发", False, str(e)[:120])
finally:
    agent.client.chat.completions.create = original_create
    _restore_history(history_backup)
    agent.conversation_history = original_history[:]
    override_config(compress_threshold=6000, keep_recent_turns=3)


# ─── 场景8: 会话持久化 ───────────────────────────────────────────────────────
print("\n=== 场景8: 会话持久化 ===")
cleanup_workspace()
history_backup = _backup_history()
original_history = agent.conversation_history[:]

try:
    agent.conversation_history = []
    add_to_history("持久化问题1", "持久化回答1")
    add_to_history("持久化问题2", "持久化回答2")
    count_before = len(agent.conversation_history)  # 应为 4

    save_history()
    agent.conversation_history = []

    restored_rounds = load_history()
    count_after = len(agent.conversation_history)

    ok = count_after == count_before and restored_rounds == count_before // 2
    report("场景8-会话持久化", ok,
           f"期望{count_before}条/{count_before // 2}轮，"
           f"恢复{count_after}条/{restored_rounds}轮" if not ok else "")
except Exception as e:
    report("场景8-会话持久化", False, str(e)[:120])
finally:
    _restore_history(history_backup)
    agent.conversation_history = original_history[:]


# ─── 场景9: 任务回滚 ─────────────────────────────────────────────────────────
print("\n=== 场景9: 任务回滚 ===")
cleanup_workspace()
rollback_path = WORKSPACE / "rollback_test.py"
try:
    original_content = "# original\ndef foo():\n    return 1\n"
    write_file("rollback_test.py", original_content)

    snap = create_snapshot(["rollback_test.py"])
    if snap is None:
        report("场景9-创建快照", False, "create_snapshot 返回 None")
    else:
        write_file("rollback_test.py", "# modified\ndef foo():\n    return 999\n")
        modified = rollback_path.read_text(encoding="utf-8")

        n = restore_snapshot(snap)
        restored = rollback_path.read_text(encoding="utf-8")
        cleanup_snapshot(snap)

        ok = restored == original_content
        report("场景9-任务回滚", ok,
               f"回滚后内容不符，n={n}, content={repr(restored[:40])}"
               if not ok else "")
except Exception as e:
    report("场景9-任务回滚", False, str(e)[:120])
finally:
    if rollback_path.exists():
        rollback_path.unlink()


# ─── 场景10: 日志记录 ────────────────────────────────────────────────────────
print("\n=== 场景10: 日志记录 ===")
cleanup_workspace()
log_dir = WORKSPACE / ".yansh" / "logs"
try:
    files_before = set(log_dir.glob("*.jsonl")) if log_dir.exists() else set()

    init_task_log("集成测试任务", "test")
    finish_task_log(True, 1)

    files_after = set(log_dir.glob("*.jsonl")) if log_dir.exists() else set()
    new_files = files_after - files_before

    if not new_files:
        report("场景10-日志记录", False, "未生成 .jsonl 文件")
    else:
        log_file = sorted(new_files)[-1]
        try:
            data = json.loads(log_file.read_text(encoding="utf-8"))
            valid = isinstance(data, dict) and "requirement" in data and "test_result" in data
            report("场景10-日志记录", valid,
                   f"JSON 结构不合法，keys={list(data.keys())}" if not valid else "")
        except json.JSONDecodeError as e:
            report("场景10-日志记录", False, f"JSON 解析失败: {e}")
except Exception as e:
    report("场景10-日志记录", False, str(e)[:120])


# ─── 场景11: 批处理模式（--json subprocess）──────────────────────────────────
print("\n=== 场景11: 批处理模式 ===")
cleanup_workspace()
batch_path = WORKSPACE / "batch_test.py"

try:
    proc = subprocess.run(
        [sys.executable, "main.py",
         "--task", "创建batch_test.py打印batch",
         "--mode", "code",
         "--json"],
        capture_output=True,
        timeout=300,
        cwd=str(ROOT),
        env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
    )
    stdout = proc.stdout.decode("utf-8", errors="replace").strip() if proc.stdout else ""
    stderr_tail = proc.stderr.decode("utf-8", errors="replace")[-300:] if proc.stderr else ""

    if not stdout:
        report("场景11-批处理模式", False,
               f"stdout 为空 (returncode={proc.returncode}) stderr末尾: {stderr_tail}")
    else:
        # stdout 可能有多行，取最后一个非空行（JSON）
        json_line = next(
            (l for l in reversed(stdout.splitlines()) if l.strip().startswith("{")),
            None,
        )
        if json_line is None:
            report("场景11-批处理模式", False,
                   f"stdout 中未找到 JSON: {stdout[:100]}")
        else:
            try:
                data = json.loads(json_line)
                ok = data.get("success") is True
                report("场景11-批处理模式", ok,
                       f"success={data.get('success')}, error={data.get('error')}"
                       if not ok else "")
            except json.JSONDecodeError as e:
                report("场景11-批处理模式", False,
                       f"JSON 解析失败: {e} | raw: {json_line[:100]}")
except subprocess.TimeoutExpired:
    report("场景11-批处理模式", False, "执行超时（300s）")
except Exception as e:
    report("场景11-批处理模式", False, str(e)[:120])


# ─── 场景12: 自动生成测试 ────────────────────────────────────────────────────
print("\n=== 场景12: 自动生成测试 ===")
cleanup_workspace()
tests_ws = WORKSPACE / "tests"
calc_path = WORKSPACE / "calculator.py"

try:
    result = run("创建calculator.py含add函数实现两数相加", mode="code")

    # 检查 workspace/tests/ 下是否生成了 test_*.py
    generated_tests = list(tests_ws.glob("test_*.py")) if tests_ws.exists() else []
    ok = len(generated_tests) > 0
    if ok:
        names = [f.name for f in generated_tests]
        report("场景12-自动生成测试", True)
        print(f"  生成的测试文件: {names}")
    else:
        # 可能 LLM 把测试文件名起错了，检查是否有 calculator 相关
        any_py = list(tests_ws.glob("*.py")) if tests_ws.exists() else []
        report("场景12-自动生成测试", False,
               f"tests/ 下无 test_*.py (workspace/tests 内容: {[f.name for f in any_py]})")
except Exception as e:
    report("场景12-自动生成测试", False, str(e)[:120])


# ─── 汇总 ────────────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
total = PASS_COUNT + FAIL_COUNT
pct = 100 * PASS_COUNT // total if total else 0
print(f"测试结果：{PASS_COUNT}/{total} 通过 ({pct}%)")
print("=" * 60)
for name, ok, reason in RESULTS:
    if ok:
        print(f"  ✓ {name}")
    else:
        print(f"  ✗ {name}  →  {reason}")
print()
