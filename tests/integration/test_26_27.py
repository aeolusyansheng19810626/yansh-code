import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import json
import shutil
from pathlib import Path
import agent
from agent import set_batch_mode, write_file
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

def run_agent_input(text):
    from io import StringIO
    import sys
    old_stdout = sys.stdout; sys.stdout = StringIO()
    try:
        if text.startswith("/"):
            parts = text[1:].split()
            if not parts: return ""
            cmd = parts[0]
            if cmd == "stats": agent.show_stats()
            elif cmd == "replay" and len(parts) > 1 and parts[1] == "list": agent.list_replays()
        else:
            if any(k in text for k in ["任务", "创建", "修改", "实现"]): agent.run(text, mode="auto")
            else: agent.chat(text)
        return sys.stdout.getvalue()
    finally: sys.stdout = old_stdout

load_project_config()
set_batch_mode(True, json_output=False)

def test_scene_26_add_file():
    print("\n=== 场景26: @add_file上下文注入 ===")
    cleanup_workspace()
    try:
        from tools import _validate_path
        write_file("utils.py", "def helper(x): return x + 1")
        write_file("config.py", "DEBUG = True")

        reply_s = agent.chat("请总结 @utils.py")
        ok_s = "helper" in reply_s or "utils.py" in reply_s

        reply_m = agent.chat("对比 @utils.py 和 @config.py")
        ok_m = ("helper" in reply_m or "utils.py" in reply_m) and ("DEBUG" in reply_m or "config.py" in reply_m)

        _, err = _validate_path("../../etc/passwd")
        ok_e = err is not None

        ok = ok_s and ok_m and ok_e
        report("场景26-@add_file上下文注入", ok,
               f"single={ok_s} multi={ok_m} traversal_blocked={ok_e}")
        return ok
    except Exception as e:
        report("场景26-@add_file上下文注入", False, str(e)[:120])
        return False

def test_scene_27_replay():
    print("\n=== 场景27: 失败案例回放包 ===")
    cleanup_workspace()
    replay_dir = WORKSPACE / ".yansh" / "replay"
    if replay_dir.exists(): shutil.rmtree(str(replay_dir))
    try:
        old_max = get_config().get("max_attempts", 3)
        override_config(max_attempts=1)
        run_agent_input("任务：创建一个必然失败的任务，测试命令设为 'python -c \"import sys; sys.exit(1)\"'")
        override_config(max_attempts=old_max)
        packs = list(replay_dir.glob("replay_*"))
        ok_p = len(packs) > 0
        ok_l = False
        if ok_p:
            p = packs[0]
            has_f = (p / "conversation.json").exists() and (p / "meta.json").exists() and (p / "workspace_snapshot").exists()
            list_out = run_agent_input("/replay list")
            ok_l = "replay_" in list_out
            ok = has_f and ok_l
        else: ok = False
        report("场景27-失败案例回放包", ok)
        return ok
    except Exception as e:
        report("场景27-失败案例回放包", False, str(e)[:120])
        return False

if __name__ == "__main__":
    results = [
        test_scene_26_add_file(),
        test_scene_27_replay(),
    ]
    passed = sum(results)
    total = len(results)
    print(f"\n{'='*60}")
    print(f"测试结果：{passed}/{total} 通过")
    print(f"{'='*60}")
