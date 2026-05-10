import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from pathlib import Path
from unittest.mock import MagicMock
import agent
import tools
from agent import set_batch_mode
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
    if (WORKSPACE / "dir1").exists():
        import shutil
        shutil.rmtree(WORKSPACE / "dir1")

load_project_config()
set_batch_mode(True, json_output=False)

def test_scene_13_linter():
    print("\n=== 场景13: 多语言Linter ===")
    cleanup_workspace()
    try:
        bad_py = WORKSPACE / "bad.py"
        bad_py.write_text("import os\nimport sys\ndef foo():\n    pass\n", encoding="utf-8")
        agent._PROJECT_TYPE = "Python"
        result = agent.run_linter()
        ok = result and result.get("returncode") != 0
        report("场景13-多语言Linter", ok)
        return ok
    except Exception as e:
        report("场景13-多语言Linter", False, str(e)[:120])
        return False

def test_scene_14_search():
    print("\n=== 场景14: 网络搜索 ===")
    try:
        res = tools.search_docs("python requests")
        ok = "results" in res and res["results"] and "未找到" not in str(res["results"])
        report("场景14-网络搜索", ok)
        return ok
    except Exception as e:
        report("场景14-网络搜索", False, str(e)[:120])
        return False

def test_scene_15_fetch():
    print("\n=== 场景15: fetch_webpage ===")
    try:
        res = tools.fetch_webpage("https://httpbin.org/get")
        ok = "content" in res and res["content"]
        report("场景15-fetch_webpage", ok)
        return ok
    except Exception as e:
        report("场景15-fetch_webpage", False, str(e)[:120])
        return False

def test_scene_16_tree():
    print("\n=== 场景16: 目录树状图 ===")
    cleanup_workspace()
    try:
        (WORKSPACE / "dir1").mkdir(exist_ok=True)
        (WORKSPACE / "dir1" / "f1.txt").write_text("1")
        captured_msgs = []
        orig = agent.call_llm
        def mock_llm(msgs, **kw):
            captured_msgs.extend(msgs); r = MagicMock()
            r.choices = [MagicMock()]; r.choices[0].message.content = '{"files": [], "test_command": ""}'
            return r
        agent.call_llm = mock_llm
        agent.plan("test tree")
        agent.call_llm = orig
        sys_prompt = captured_msgs[0]["content"] if captured_msgs else ""
        ok = "dir1" in sys_prompt and "f1.txt" in sys_prompt
        report("场景16-目录树状图", ok)
        return ok
    except Exception as e:
        report("场景16-目录树状图", False, str(e)[:120])
        return False

if __name__ == "__main__":
    results = [
        test_scene_13_linter(),
        test_scene_14_search(),
        test_scene_15_fetch(),
        test_scene_16_tree(),
    ]
    passed = sum(bool(r) for r in results)
    total = len(results)
    print(f"\n{'='*60}")
    print(f"测试结果：{passed}/{total} 通过")
    print(f"{'='*60}")
