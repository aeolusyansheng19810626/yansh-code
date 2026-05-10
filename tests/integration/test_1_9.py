import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import json
import subprocess
import shutil
import glob
from pathlib import Path
from unittest.mock import MagicMock

import agent
import tools
from agent import (
    set_batch_mode, run, save_history, load_history,
    add_to_history, maybe_compress_history,
    create_snapshot, restore_snapshot, cleanup_snapshot,
    init_task_log, finish_task_log, get_last_task_log,
)
from tools import (
    execute_command, read_file, write_file, replace_symbol,
    list_symbols, apply_patch, list_files, search_in_files, append_to_file,
    find_references
)
from config import load_project_config, override_config, get_config

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

load_project_config()
set_batch_mode(True, json_output=False)

def test_scene_1_auto_create():
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
            report("场景1-auto创建文件", has_print, "文件存在但不含 print" if not has_print else "")
        return exists and (not exists or "print" in content.lower())
    except Exception as e:
        report("场景1-auto创建文件", False, str(e)[:120])
        return False

def test_scene_2_plan():
    print("\n=== 场景2: plan模式只出计划 ===")
    cleanup_workspace()
    plan_path = WORKSPACE / "plan_test.py"
    try:
        result = run("创建plan_test.py", mode="plan")
        success = result.get("success", False)
        not_created = not plan_path.exists()
        log = get_last_task_log()
        has_plan = bool(log.get("plan"))
        ok = success and not_created and has_plan
        report("场景2-plan模式", ok, "计划字段为空或文件被创建" if not ok else "")
        return ok
    except Exception as e:
        report("场景2-plan模式", False, str(e)[:120])
        return False

def test_scene_3_code():
    print("\n=== 场景3: code模式跳过确认 ===")
    cleanup_workspace()
    code_path = WORKSPACE / "code_test.py"
    try:
        result = run("创建code_test.py打印ok", mode="code")
        exists = code_path.exists()
        report("场景3-code模式创建文件", exists, "code_test.py 未创建" if not exists else "")
        return exists
    except Exception as e:
        report("场景3-code模式", False, str(e)[:120])
        return False

def test_scene_4_danger_cmd():
    print("\n=== 场景4: 危险命令拦截 ===")
    try:
        result = execute_command("rm -rf /")
        has_block = "error" in result and "安全拦截" in result["error"]
        report("场景4-危险命令拦截", has_block, f"拦截失败: {result}" if not has_block else "")
        return has_block
    except Exception as e:
        report("场景4-危险命令拦截", False, str(e)[:120])
        return False

def test_scene_5_path_traversal():
    print("\n=== 场景5: 路径越界拦截 ===")
    try:
        result = read_file("../secret.txt")
        ok = "error" in result
        report("场景5-路径越界拦截", ok, f"拦截失败: {result}" if not ok else "")
        return ok
    except Exception as e:
        report("场景5-路径越界拦截", False, str(e)[:120])
        return False

def test_scene_6_replace_symbol():
    print("\n=== 场景6: replace_symbol替换函数 ===")
    cleanup_workspace()
    target_path = WORKSPACE / "target.py"
    try:
        write_file("target.py", "def my_func():\n    return 0\n\ndef other_func():\n    pass\n")
        result = replace_symbol("my_func", "def my_func():\n    return 42", "target.py")
        content = target_path.read_text(encoding="utf-8")
        ok = "return 42" in content and "def other_func" in content
        report("场景6-replace_symbol", ok, "替换失败或意外修改" if not ok else "")
        return ok
    except Exception as e:
        report("场景6-replace_symbol", False, str(e)[:120])
        return False

def test_scene_7_auto_compress():
    print("\n=== 场景7: 自动压缩触发 ===")
    history_backup = _backup_history()
    original_history = agent.conversation_history[:]
    original_create = agent.client.chat.completions.create
    try:
        mock_summary = "【已完成任务】\n- mock压缩测试\n\n【关键文件】\n- mock.py\n\n【未解决问题】\n- 无"
        mock_msg = MagicMock(); mock_msg.content = mock_summary
        mock_choice = MagicMock(); mock_choice.message = mock_msg
        mock_resp = MagicMock(); mock_resp.choices = [mock_choice]
        agent.client.chat.completions.create = MagicMock(return_value=mock_resp)
        agent.conversation_history = []
        override_config(compress_threshold=300, keep_recent_turns=1)
        for i in range(4):
            add_to_history(f"用户{i}: " + "x"*40, f"助手{i}: " + "y"*40)
        maybe_compress_history()
        ok = len(agent.conversation_history) < 8 and any("【已完成任务】" in m["content"] for m in agent.conversation_history)
        report("场景7-自动压缩触发", ok)
        return ok
    except Exception as e:
        report("场景7-自动压缩触发", False, str(e)[:120])
        return False
    finally:
        agent.client.chat.completions.create = original_create
        _restore_history(history_backup)
        agent.conversation_history = original_history[:]
        override_config(compress_threshold=6000, keep_recent_turns=3)

def test_scene_8_persistence():
    print("\n=== 场景8: 会话持久化 ===")
    history_backup = _backup_history()
    original_history = agent.conversation_history[:]
    try:
        agent.conversation_history = []
        add_to_history("p1", "a1"); add_to_history("p2", "a2")
        save_history()
        agent.conversation_history = []
        load_history()
        ok = len(agent.conversation_history) == 4
        report("场景8-会话持久化", ok)
        return ok
    except Exception as e:
        report("场景8-会话持久化", False, str(e)[:120])
        return False
    finally:
        _restore_history(history_backup)
        agent.conversation_history = original_history[:]

def test_scene_9_rollback():
    print("\n=== 场景9: 任务回滚 ===")
    cleanup_workspace()
    rollback_path = WORKSPACE / "rollback_test.py"
    try:
        write_file("rollback_test.py", "v1")
        snap = create_snapshot(["rollback_test.py"])
        write_file("rollback_test.py", "v2")
        restore_snapshot(snap)
        cleanup_snapshot(snap)
        ok = rollback_path.read_text(encoding="utf-8") == "v1"
        report("场景9-任务回滚", ok)
        return ok
    except Exception as e:
        report("场景9-任务回滚", False, str(e)[:120])
        return False

if __name__ == "__main__":
    results = [
        test_scene_1_auto_create(),
        test_scene_2_plan(),
        test_scene_3_code(),
        test_scene_4_danger_cmd(),
        test_scene_5_path_traversal(),
        test_scene_6_replace_symbol(),
        test_scene_7_auto_compress(),
        test_scene_8_persistence(),
        test_scene_9_rollback(),
    ]
    passed = sum(results)
    total = len(results)
    print(f"\n{'='*60}")
    print(f"测试结果：{passed}/{total} 通过")
    print(f"{'='*60}")
