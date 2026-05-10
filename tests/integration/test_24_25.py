import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from pathlib import Path
import agent
import tools
from agent import set_batch_mode, write_file
from tools import find_references
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

load_project_config()
set_batch_mode(True, json_output=False)

def test_scene_24_find_ref():
    print("\n=== 场景24: find_references 符号引用查找 ===")
    cleanup_workspace()
    try:
        write_file("module.py", "def my_func(x): return x*2\nclass MyClass: pass")
        write_file("main.py", "from module import my_func\nr = my_func(10)")
        res = find_references("my_func")
        refs = str(res.get("references", []))
        ok = "main.py" in refs and "def my_func" not in refs
        res_err = find_references("my_func", "../")
        ok = ok and "error" in res_err
        report("场景24-find_references", ok)
        return ok
    except Exception as e:
        report("场景24-find_references", False, str(e)[:120])
        return False

def test_scene_25_token_stats():
    print("\n=== 场景25: token消耗统计 ===")
    try:
        from io import StringIO
        import sys
        agent.chat("你好")
        old = sys.stdout; sys.stdout = StringIO()
        agent.show_stats()
        out = sys.stdout.getvalue(); sys.stdout = old
        ok = any(k in out for k in ["Token", "prompt", "completion", "预估费用"])
        report("场景25-token消耗统计", ok)
        return ok
    except Exception as e:
        report("场景25-token消耗统计", False, str(e)[:120])
        return False

if __name__ == "__main__":
    results = [
        test_scene_24_find_ref(),
        test_scene_25_token_stats(),
    ]
    passed = sum(results)
    total = len(results)
    print(f"\n{'='*60}")
    print(f"测试结果：{passed}/{total} 通过")
    print(f"{'='*60}")
