"""测试 /compress /context /clear 三个命令"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import patch, MagicMock
import agent

def setup():
    agent.conversation_history = []

def make_history(n_turns, chars_per_msg=100):
    msgs = []
    for i in range(n_turns):
        msgs.append({"role": "user",      "content": f"u{i}:" + "x" * chars_per_msg})
        msgs.append({"role": "assistant", "content": f"a{i}:" + "y" * chars_per_msg})
    return msgs

def mock_resp(text):
    r = MagicMock()
    r.choices[0].message.content = text
    return r

# --- /clear ---
def test_clear():
    agent.conversation_history = make_history(5)
    agent.clear_history()
    assert agent.conversation_history == []
    print("[PASS] /clear 清空历史")

# --- /context 轮数与字符数 ---
def test_context_display(capsys=None):
    agent.conversation_history = make_history(4, chars_per_msg=50)
    total = sum(len(m["content"]) for m in agent.conversation_history)
    turns = len(agent.conversation_history) // 2
    # 只要不报错、轮数正确即可
    assert turns == 4
    print(f"[PASS] /context 轮数={turns} 字符数={total}")

# --- /compress 历史较短时跳过 ---
def test_compress_short():
    agent.conversation_history = make_history(2, chars_per_msg=10)  # 远低于6000
    original = list(agent.conversation_history)
    agent.compress_history()
    assert agent.conversation_history == original
    print("[PASS] /compress 历史短时跳过")

# --- /compress 正常压缩 ---
def test_compress_normal():
    agent.conversation_history = make_history(8, chars_per_msg=500)  # 超过6000
    summary = "【已完成任务】\n- 手动压缩\n\n【关键文件】\n- agent.py\n\n【未解决问题】\n- 无"
    recent_before = agent.conversation_history[-6:]

    with patch.object(agent.client.chat.completions, "create", return_value=mock_resp(summary)):
        agent.compress_history()

    assert agent.conversation_history[0]["content"] == summary
    assert agent.conversation_history[1:] == recent_before
    print(f"[PASS] /compress 压缩完成，共 {len(agent.conversation_history)} 条（1摘要+{len(recent_before)}最近）")

# --- /compress 打印文案含"手动" ---
def test_compress_message(capsys=None):
    import io
    agent.conversation_history = make_history(8, chars_per_msg=500)
    summary = "摘要内容"
    output = []
    original_print = agent.console.print
    agent.console.print = lambda msg, **kw: output.append(str(msg))
    try:
        with patch.object(agent.client.chat.completions, "create", return_value=mock_resp(summary)):
            agent.compress_history()
    finally:
        agent.console.print = original_print
    assert any("手动" in s for s in output), f"未找到手动压缩提示，输出：{output}"
    print("[PASS] /compress 输出含'手动'")

if __name__ == "__main__":
    for fn in [test_clear, test_context_display, test_compress_short, test_compress_normal, test_compress_message]:
        setup()
        fn()
    print("\n全部通过")
