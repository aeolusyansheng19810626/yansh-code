import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import json
import subprocess
from pathlib import Path
import agent
from agent import set_batch_mode, run, init_task_log, finish_task_log
from config import load_project_config

ROOT = Path(__file__).parent.parent.parent
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

def cleanup_workspace():
    for f in WORKSPACE.glob("*.py"):
        f.unlink()
    tests_dir = WORKSPACE / "tests"
    if tests_dir.exists():
        for f in tests_dir.glob("*.py"):
            f.unlink()

load_project_config()
set_batch_mode(True, json_output=False)

def test_scene_10_logs():
    print("\n=== 场景10: 日志记录 ===")
    cleanup_workspace()
    log_dir = WORKSPACE / ".yansh" / "logs"
    try:
        files_before = set(log_dir.glob("*.jsonl")) if log_dir.exists() else set()
        init_task_log("集成测试任务", "test")
        finish_task_log(True, 1)
        files_after = set(log_dir.glob("*.jsonl")) if log_dir.exists() else set()
        new_files = files_after - files_before
        ok = len(new_files) > 0
        if ok:
            log_file = sorted(new_files)[-1]
            data = json.loads(log_file.read_text(encoding="utf-8"))
            ok = "requirement" in data and "test_result" in data
        report("场景10-日志记录", ok)
        return ok
    except Exception as e:
        report("场景10-日志记录", False, str(e)[:120])
        return False

def test_scene_11_batch_mode():
    print("\n=== 场景11: 批处理模式 ===")
    cleanup_workspace()
    try:
        proc = subprocess.run(
            [sys.executable, "main.py", "创建batch_test.py打印batch", "--mode", "code", "--json"],
            capture_output=True, timeout=300, cwd=str(ROOT),
            env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
        )
        stdout = proc.stdout.decode("utf-8", errors="replace").strip()
        json_line = next((l for l in reversed(stdout.splitlines()) if l.strip().startswith("{")), None)
        ok = False
        if json_line:
            data = json.loads(json_line)
            ok = data.get("test_result") == "pass" or data.get("success") is True
        report("场景11-批处理模式", ok, "JSON 解析失败或 test_result!=pass" if not ok else "")
        return ok
    except Exception as e:
        report("场景11-批处理模式", False, str(e)[:300])
        return False

def test_scene_12_auto_test():
    print("\n=== 场景12: 自动生成测试 ===")
    cleanup_workspace()
    tests_ws = WORKSPACE / "tests"
    try:
        run("创建calculator.py含add函数实现两数相加", mode="code")
        generated_tests = list(tests_ws.glob("test_*.py")) if tests_ws.exists() else []
        ok = len(generated_tests) > 0
        report("场景12-自动生成测试", ok)
        return ok
    except Exception as e:
        report("场景12-自动生成测试", False, str(e)[:120])
        return False

if __name__ == "__main__":
    results = [
        test_scene_10_logs(),
        test_scene_11_batch_mode(),
        test_scene_12_auto_test(),
    ]
    passed = sum(results)
    total = len(results)
    print(f"\n{'='*60}")
    print(f"测试结果：{passed}/{total} 通过")
    print(f"{'='*60}")
