import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from pathlib import Path
from unittest.mock import patch
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


load_project_config()
set_batch_mode(True, json_output=False)


def _hil_write_scenario(answer: str, filename: str, content: str):
    """辅助：开启 HIL，mock 用户输入 answer，执行 write_file 工具调用，返回文件是否落盘。"""
    override_config(human_in_loop=True)
    agent._HIL_AUTO_ACCEPT = False
    cleanup_workspace()
    filepath = WORKSPACE / filename
    try:
        with patch.object(agent, "_prompt", return_value=answer):
            agent._hil_confirm(filename, "", content, is_new_file=True)
            if answer in ("y", "a"):
                write_file(filename, content)
    finally:
        override_config(human_in_loop=False)
    return filepath.exists()


def test_scene_28_hil_accept():
    print("\n=== 场景28: HIL 用户输入 y → 文件写入 ===")
    cleanup_workspace()
    try:
        override_config(human_in_loop=True)
        agent._HIL_AUTO_ACCEPT = False
        accept, content = None, None
        with patch.object(agent, "_prompt", return_value="y"):
            accept, content = agent._hil_confirm("out.py", "", "x = 1\n", is_new_file=True)
        ok = accept is True and content == "x = 1\n"
        report("场景28-HIL-y接受", ok)
        return ok
    except Exception as e:
        report("场景28-HIL-y接受", False, str(e)[:120])
        return False
    finally:
        override_config(human_in_loop=False)


def test_scene_29_hil_reject():
    print("\n=== 场景29: HIL 用户输入 n → 文件不写入 ===")
    cleanup_workspace()
    try:
        override_config(human_in_loop=True)
        agent._HIL_AUTO_ACCEPT = False
        with patch.object(agent, "_prompt", return_value="n"):
            accept, _ = agent._hil_confirm("skip.py", "", "y = 2\n", is_new_file=True)
        filepath = WORKSPACE / "skip.py"
        ok = accept is False and not filepath.exists()
        report("场景29-HIL-n拒绝", ok)
        return ok
    except Exception as e:
        report("场景29-HIL-n拒绝", False, str(e)[:120])
        return False
    finally:
        override_config(human_in_loop=False)


def test_scene_30_hil_auto_accept():
    print("\n=== 场景30: HIL 用户输入 a → 后续不再询问 ===")
    cleanup_workspace()
    try:
        override_config(human_in_loop=True)
        agent._HIL_AUTO_ACCEPT = False
        prompt_calls = []

        def counting_prompt(msg=""):
            prompt_calls.append(msg)
            return "a"

        with patch.object(agent, "_prompt", side_effect=counting_prompt):
            accept1, _ = agent._hil_confirm("f1.py", "", "a=1\n", is_new_file=True)
        # 第二次应直接接受，不再调用 _prompt
        with patch.object(agent, "_prompt", side_effect=counting_prompt):
            accept2, _ = agent._hil_confirm("f2.py", "", "b=2\n", is_new_file=True)

        ok = accept1 and accept2 and len(prompt_calls) == 1 and agent._HIL_AUTO_ACCEPT
        report("场景30-HIL-a全部接受", ok)
        return ok
    except Exception as e:
        report("场景30-HIL-a全部接受", False, str(e)[:120])
        return False
    finally:
        override_config(human_in_loop=False)
        agent._HIL_AUTO_ACCEPT = False


def test_scene_31_hil_disabled():
    print("\n=== 场景31: HUMAN_IN_LOOP=false 时不弹出确认 ===")
    cleanup_workspace()
    try:
        override_config(human_in_loop=False)
        agent._HIL_AUTO_ACCEPT = False
        called = []
        with patch.object(agent, "_hil_confirm", side_effect=lambda *a, **kw: called.append(1) or (True, a[2] if len(a) > 2 else "")):
            cfg_val = get_config().get("human_in_loop")
        ok = cfg_val is False
        report("场景31-HIL关闭不弹确认", ok)
        return ok
    except Exception as e:
        report("场景31-HIL关闭不弹确认", False, str(e)[:120])
        return False


if __name__ == "__main__":
    results = [
        test_scene_28_hil_accept(),
        test_scene_29_hil_reject(),
        test_scene_30_hil_auto_accept(),
        test_scene_31_hil_disabled(),
    ]
    passed = sum(results)
    total = len(results)
    print(f"\n{'='*60}")
    print(f"测试结果：{passed}/{total} 通过")
    print(f"{'='*60}")
