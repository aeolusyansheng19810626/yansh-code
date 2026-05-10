import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import io
import base64
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest

import agent
from agent import set_batch_mode, _build_vision_content, _load_image_file
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


def _make_png_bytes(w=10, h=10):
    try:
        from PIL import Image
        img = Image.new("RGB", (w, h), color="blue")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except ImportError:
        return None


def _fake_llm_response(text="OK"):
    """构造一个最小化的 OpenAI ChatCompletion 响应对象。"""
    msg = MagicMock()
    msg.content = text
    msg.tool_calls = None
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    resp.usage = None
    return resp


load_project_config()
set_batch_mode(True, json_output=False)


def test_scene_32_vision_content_array():
    """场景32: chat() 中 @image 指令正确构造 vision content 数组发给 LLM"""
    print("\n=== 场景32: vision content 数组注入 ===")
    png_data = _make_png_bytes()
    if png_data is None:
        report("场景32-vision-content数组", False, "Pillow 未安装，跳过")
        return False

    captured_messages = []

    def fake_create(**kwargs):
        captured_messages.extend(kwargs.get("messages", []))
        return _fake_llm_response("图片内容是蓝色方块")

    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
            tf.write(png_data)
            tmp_path = tf.name

        with patch.object(agent.client.chat.completions, "create", side_effect=fake_create):
            agent.chat(f"分析这张图 @image {tmp_path}")

        Path(tmp_path).unlink(missing_ok=True)

        # 找到发给 LLM 的 user 消息
        user_msgs = [m for m in captured_messages if m.get("role") == "user"]
        ok_found = len(user_msgs) > 0
        if not ok_found:
            report("场景32-vision-content数组", False, "未捕获到 user 消息")
            return False

        user_content = user_msgs[-1]["content"]
        ok_list = isinstance(user_content, list)
        ok_img = ok_list and any(
            item.get("type") == "image_url" and "data:image/png;base64," in item.get("image_url", {}).get("url", "")
            for item in user_content
        )
        ok_text = ok_list and any(
            item.get("type") == "text" and "分析这张图" in item.get("text", "")
            for item in user_content
        )
        # 图片在文字之前
        types = [item.get("type") for item in user_content] if ok_list else []
        ok_order = types.index("image_url") < types.index("text") if ok_list and "image_url" in types and "text" in types else False

        ok = ok_list and ok_img and ok_text and ok_order
        report("场景32-vision-content数组", ok,
               f"list={ok_list} img={ok_img} text={ok_text} order={ok_order}")
        return ok
    except Exception as e:
        report("场景32-vision-content数组", False, str(e)[:120])
        return False


def test_scene_33_image_not_in_history():
    """场景33: 图片不写入对话历史（历史只保留纯文本）"""
    print("\n=== 场景33: 图片不持久化到历史 ===")
    png_data = _make_png_bytes()
    if png_data is None:
        report("场景33-图片不入历史", False, "Pillow 未安装，跳过")
        return False

    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
            tf.write(png_data)
            tmp_path = tf.name

        agent.conversation_history.clear()

        with patch.object(agent.client.chat.completions, "create",
                          return_value=_fake_llm_response("OK")):
            agent.chat(f"请分析 @image {tmp_path}")

        Path(tmp_path).unlink(missing_ok=True)

        # 历史中的 user 消息应为纯字符串，不含 base64
        user_msgs = [m for m in agent.conversation_history if m["role"] == "user"]
        ok_str = all(isinstance(m["content"], str) for m in user_msgs)
        ok_no_b64 = all("base64" not in m["content"] for m in user_msgs)

        ok = ok_str and ok_no_b64
        report("场景33-图片不入历史", ok,
               f"str_only={ok_str} no_base64={ok_no_b64}")
        return ok
    except Exception as e:
        report("场景33-图片不入历史", False, str(e)[:120])
        return False
    finally:
        agent.conversation_history.clear()


def test_scene_34_multiple_images():
    """场景34: 多张 @image 指令 → content 数组含所有图片"""
    print("\n=== 场景34: 多图片注入 ===")
    png_data = _make_png_bytes()
    if png_data is None:
        report("场景34-多图片注入", False, "Pillow 未安装，跳过")
        return False

    captured_messages = []

    def fake_create(**kwargs):
        captured_messages.extend(kwargs.get("messages", []))
        return _fake_llm_response("两张图分析完毕")

    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf1, \
             tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf2:
            tf1.write(png_data)
            tf2.write(png_data)
            path1, path2 = tf1.name, tf2.name

        with patch.object(agent.client.chat.completions, "create", side_effect=fake_create):
            agent.chat(f"对比两图 @image {path1} @image {path2}")

        Path(path1).unlink(missing_ok=True)
        Path(path2).unlink(missing_ok=True)

        user_msgs = [m for m in captured_messages if m.get("role") == "user"]
        user_content = user_msgs[-1]["content"] if user_msgs else []
        img_count = sum(1 for item in user_content if item.get("type") == "image_url") if isinstance(user_content, list) else 0

        ok = img_count == 2
        report("场景34-多图片注入", ok, f"img_count={img_count}")
        return ok
    except Exception as e:
        report("场景34-多图片注入", False, str(e)[:120])
        return False


def test_scene_35_no_image_plain_text():
    """场景35: 无图片时 content 保持为纯字符串，不降级为 list"""
    print("\n=== 场景35: 无图片时保持纯文本 ===")
    captured_messages = []

    def fake_create(**kwargs):
        captured_messages.extend(kwargs.get("messages", []))
        return _fake_llm_response("你好")

    try:
        with patch.object(agent.client.chat.completions, "create", side_effect=fake_create):
            agent.chat("你好")

        user_msgs = [m for m in captured_messages if m.get("role") == "user"]
        user_content = user_msgs[-1]["content"] if user_msgs else None
        ok = isinstance(user_content, str)
        report("场景35-无图片纯文本", ok, f"type={type(user_content).__name__}")
        return ok
    except Exception as e:
        report("场景35-无图片纯文本", False, str(e)[:120])
        return False
    finally:
        agent.conversation_history.clear()


if __name__ == "__main__":
    results = [
        test_scene_32_vision_content_array(),
        test_scene_33_image_not_in_history(),
        test_scene_34_multiple_images(),
        test_scene_35_no_image_plain_text(),
    ]
    passed = sum(results)
    total = len(results)
    print(f"\n{'='*60}")
    print(f"测试结果：{passed}/{total} 通过")
    print(f"{'='*60}")
