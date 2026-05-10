import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from pathlib import Path
from unittest.mock import MagicMock
import agent
import monitor
from agent import set_batch_mode, write_file
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

def test_scene_17_rules():
    print("\n=== 场景17: .agent_rules注入 ===")
    try:
        rule_file = WORKSPACE / ".agent_rules"
        rule_file.write_text("TEST_RULE: ALWAYS DO THIS", encoding="utf-8")
        captured_msgs = []
        orig = agent.call_llm
        def mock_llm(msgs, **kw):
            captured_msgs.extend(msgs); r = MagicMock()
            r.choices = [MagicMock()]; r.choices[0].message.content = '{"files": [], "test_command": ""}'
            return r
        agent.call_llm = mock_llm
        agent.plan("test rules")
        agent.call_llm = orig
        ok = "TEST_RULE: ALWAYS DO THIS" in (captured_msgs[0]["content"] if captured_msgs else "")
        report("场景17-.agent_rules注入", ok)
        return ok
    except Exception as e:
        report("场景17-.agent_rules注入", False, str(e)[:120])
        return False

def test_scene_18_review():
    print("\n=== 场景18: Review Agent ===")
    try:
        write_file("test_file.py", "def test(): pass")
        orig = agent.call_llm
        def mock_llm(msgs, **kw):
            m = MagicMock(); m.message.content = '{"approved": true, "issues": [], "suggestions": []}'
            r = MagicMock(); r.choices = [m]; return r
        agent.call_llm = mock_llm
        res = agent.review("test", ["test_file.py"])
        agent.call_llm = orig
        ok = res.get("approved") is True
        report("场景18-Review Agent", ok)
        return ok
    except Exception as e:
        report("场景18-Review Agent", False, str(e)[:120])
        return False

def test_scene_19_stats():
    print("\n=== 场景19: 日志统计/stats ===")
    try:
        from io import StringIO
        import sys
        old = sys.stdout; sys.stdout = StringIO()
        monitor.analyze_logs(agent._LOG_DIR)
        out = sys.stdout.getvalue(); sys.stdout = old
        ok = any(k in out for k in ["任务", "无有效", "无日志"])
        report("场景19-日志统计", ok)
        return ok
    except Exception as e:
        report("场景19-日志统计", False, str(e)[:120])
        return False

if __name__ == "__main__":
    results = [
        test_scene_17_rules(),
        test_scene_18_review(),
        test_scene_19_stats(),
    ]
    passed = sum(results)
    total = len(results)
    print(f"\n{'='*60}")
    print(f"测试结果：{passed}/{total} 通过")
    print(f"{'='*60}")
