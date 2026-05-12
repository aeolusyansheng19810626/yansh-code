"""回归测试：review 异常、fix 错误截断、call_llm 退避、batch strict、
replace_in_file 报错提示等改动的行为验证。"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from pathlib import Path
from unittest.mock import MagicMock
import agent
import tools
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

load_project_config()
set_batch_mode(True, json_output=False)


def test_scene_36_review_non_json():
    """review() 在 LLM 返回非 JSON 时应 approved=False + review_error"""
    print("\n=== 场景36: Review 非 JSON 不再假通过 ===")
    try:
        write_file("scene36.py", "def f(): pass")
        orig = agent.call_llm

        def mock_llm(msgs, **kw):
            m = MagicMock(); m.message.content = "not a json string"
            r = MagicMock(); r.choices = [m]; return r

        agent.call_llm = mock_llm
        res = agent.review("test", ["scene36.py"])
        agent.call_llm = orig

        ok = (res.get("approved") is False
              and any("review_error" in i for i in res.get("issues", [])))
        report("场景36-Review 非 JSON", ok, str(res)[:120])
        return ok
    except Exception as e:
        report("场景36-Review 非 JSON", False, str(e)[:120])
        return False


def test_scene_37_review_exception():
    """review() 内部抛异常（如网络错误）也应降级为 review_error"""
    print("\n=== 场景37: Review 异常降级 ===")
    try:
        write_file("scene37.py", "def f(): pass")
        orig = agent.call_llm

        def mock_llm(msgs, **kw):
            raise RuntimeError("simulated network error")

        agent.call_llm = mock_llm
        res = agent.review("test", ["scene37.py"])
        agent.call_llm = orig

        ok = (res.get("approved") is False
              and any("review_error" in i for i in res.get("issues", [])))
        report("场景37-Review 异常降级", ok, str(res)[:120])
        return ok
    except Exception as e:
        report("场景37-Review 异常降级", False, str(e)[:120])
        return False


def test_scene_38_fix_head_tail_truncation():
    """fix() 处理超长 stderr 时应保留首尾并省略中间"""
    print("\n=== 场景38: fix() head+tail 截断 ===")
    try:
        head_marker = "HEAD_MARKER_START"
        tail_marker = "TAIL_MARKER_END"
        middle = "X" * 5000
        long_stderr = head_marker + middle + tail_marker

        captured = {}
        orig = agent.call_llm

        def mock_llm(msgs, **kw):
            captured["msgs"] = msgs
            m = MagicMock(); m.tool_calls = None; m.content = ""
            r = MagicMock(); r.choices = [MagicMock(message=m)]; return r

        agent.call_llm = mock_llm
        agent.fix({"returncode": 1, "stderr": long_stderr, "stdout": ""},
                  {"files": [], "test_command": ""})
        agent.call_llm = orig

        user_content = captured["msgs"][1]["content"]
        ok = (head_marker in user_content
              and tail_marker in user_content
              and "中间省略" in user_content
              and len(user_content) < len(long_stderr))
        report("场景38-fix 头尾截断", ok, f"content_len={len(user_content)}")
        return ok
    except Exception as e:
        report("场景38-fix 头尾截断", False, str(e)[:120])
        return False


def test_scene_39_transient_error_detection():
    """call_llm 的 _is_transient_error 应识别 429/5xx/连接错误"""
    print("\n=== 场景39: 瞬时错误识别 ===")
    try:
        class FakeErr(Exception):
            def __init__(self, status): self.status_code = status

        ok_429 = agent._is_transient_error(FakeErr(429))
        ok_503 = agent._is_transient_error(FakeErr(503))
        not_400 = agent._is_transient_error(FakeErr(400))
        not_generic = agent._is_transient_error(ValueError("bad input"))

        ok = ok_429 and ok_503 and not not_400 and not not_generic
        report("场景39-瞬时错误识别", ok,
               f"429={ok_429} 503={ok_503} 400={not_400} generic={not_generic}")
        return ok
    except Exception as e:
        report("场景39-瞬时错误识别", False, str(e)[:120])
        return False


def test_scene_40_batch_strict_blocks_confirm_cmd():
    """批处理 strict 模式下应拒绝执行需确认级命令（pip install）"""
    print("\n=== 场景40: batch strict 拒绝确认级命令 ===")
    try:
        set_batch_mode(True, json_output=False, strict=True)
        res = tools.execute_command("pip install requests")
        set_batch_mode(True, json_output=False, strict=False)

        ok = ("error" in res
              and "批处理严格模式" in res["error"]
              and res.get("returncode") == -1)
        report("场景40-batch strict 拒绝", ok, str(res)[:120])
        return ok
    except Exception as e:
        set_batch_mode(True, json_output=False, strict=False)
        report("场景40-batch strict 拒绝", False, str(e)[:120])
        return False


def test_scene_41_replace_in_file_hint():
    """replace_in_file 多匹配时错误信息应包含改进建议"""
    print("\n=== 场景41: replace_in_file 多匹配提示 ===")
    try:
        write_file("scene41.txt", "aaa aaa aaa")
        res = tools.replace_in_file("scene41.txt", "aaa", "bbb")
        err = res.get("error", "")
        ok = ("3 处匹配" in err
              and ("上下文" in err or "replace_symbol" in err))
        report("场景41-多匹配提示", ok, err[:120])
        return ok
    except Exception as e:
        report("场景41-多匹配提示", False, str(e)[:120])
        return False


def test_scene_42_call_llm_timeout_passed():
    """call_llm 应向 OpenAI SDK 传递 timeout 参数"""
    print("\n=== 场景42: call_llm 传递 timeout ===")
    try:
        captured = {}
        orig_create = agent.client.chat.completions.create

        def fake_create(**kwargs):
            captured.update(kwargs)
            r = MagicMock(); r.choices = [MagicMock(message=MagicMock(content="{}"))]
            r.usage = MagicMock(prompt_tokens=1, completion_tokens=1)
            return r

        agent.client.chat.completions.create = fake_create
        agent.call_llm([{"role": "user", "content": "ping"}])
        agent.client.chat.completions.create = orig_create

        ok = captured.get("timeout") == agent.LLM_TIMEOUT_SEC
        report("场景42-timeout 传递", ok, f"timeout={captured.get('timeout')}")
        return ok
    except Exception as e:
        report("场景42-timeout 传递", False, str(e)[:120])
        return False


if __name__ == "__main__":
    results = [
        test_scene_36_review_non_json(),
        test_scene_37_review_exception(),
        test_scene_38_fix_head_tail_truncation(),
        test_scene_39_transient_error_detection(),
        test_scene_40_batch_strict_blocks_confirm_cmd(),
        test_scene_41_replace_in_file_hint(),
        test_scene_42_call_llm_timeout_passed(),
    ]
    passed = sum(results)
    total = len(results)
    print(f"\n{'='*60}")
    print(f"测试结果：{passed}/{total} 通过")
    print(f"{'='*60}")
    # cleanup
    for name in ("scene36.py", "scene37.py", "scene41.txt"):
        p = WORKSPACE / name
        if p.exists():
            p.unlink()
    sys.exit(0 if passed == total else 1)
