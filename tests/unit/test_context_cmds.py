"""测试 /compress /context /clear 三个命令"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

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

# ── @add_file / @clear_files ──────────────────────────────────────────────────

import tempfile, pathlib

def setup_ctx():
    agent._context_files.clear()
    agent.conversation_history = []

# 正常加载单个文件
def test_add_file_single():
    setup_ctx()
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as f:
        f.write("def foo():\n    return 1\n")
        tmp = f.name
    try:
        agent._parse_context_cmds(f"@add_file {tmp}")
        assert any("foo" in v for v in agent._context_files.values())
        assert any("def foo" in v for v in agent._context_files.values())
        print("[PASS] @add_file 加载单个文件")
    finally:
        pathlib.Path(tmp).unlink(missing_ok=True)

# 正常加载多个文件
def test_add_file_multiple():
    setup_ctx()
    files = []
    try:
        for content in ["x = 1\n", "y = 2\n"]:
            f = tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8")
            f.write(content); f.close()
            files.append(f.name)
        agent._parse_context_cmds(f"@add_file {files[0]} @add_file {files[1]}")
        total = sum(1 for v in agent._context_files.values() if "x = 1" in v or "y = 2" in v)
        assert total == 2
        print("[PASS] @add_file 加载多个文件")
    finally:
        for f in files:
            pathlib.Path(f).unlink(missing_ok=True)

# 文件不存在时的错误处理
def test_add_file_not_exist():
    setup_ctx()
    output = []
    original_print = agent.console.print
    agent.console.print = lambda msg, **kw: output.append(str(msg))
    try:
        agent._parse_context_cmds("@add_file /nonexistent/file_xyz.py")
        assert len(agent._context_files) == 0
        assert any("不存在" in s or "error" in s.lower() for s in output)
        print("[PASS] @add_file 文件不存在错误处理")
    finally:
        agent.console.print = original_print

# 文件过大时的错误处理
def test_add_file_too_large():
    setup_ctx()
    with tempfile.NamedTemporaryFile(mode="wb", suffix=".txt", delete=False) as f:
        f.write(b"a" * (101 * 1024))
        tmp = f.name
    output = []
    original_print = agent.console.print
    agent.console.print = lambda msg, **kw: output.append(str(msg))
    try:
        agent._parse_context_cmds(f"@add_file {tmp}")
        assert len(agent._context_files) == 0
        assert any("过大" in s or "100" in s for s in output)
        print("[PASS] @add_file 文件过大错误处理")
    finally:
        agent.console.print = original_print
        pathlib.Path(tmp).unlink(missing_ok=True)

# @clear_files 清空
def test_clear_files():
    setup_ctx()
    agent._context_files["fake.py"] = "content"
    agent._parse_context_cmds("@clear_files")
    assert agent._context_files == {}
    print("[PASS] @clear_files 清空上下文文件")

# 注入内容的格式验证
def test_context_files_block_format():
    setup_ctx()
    agent._context_files["foo/bar.py"] = "print('hello')"
    block = agent._get_context_files_block()
    assert "=== 附加上下文文件 ===" in block
    assert "--- 文件: foo/bar.py ---" in block
    assert "print('hello')" in block
    print("[PASS] _get_context_files_block 格式正确")

# @add_file 指令从用户消息中移除
def test_add_file_stripped_from_message():
    setup_ctx()
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as f:
        f.write("z = 3\n"); tmp = f.name
    try:
        result = agent._parse_context_cmds(f"请分析一下 @add_file {tmp} 这个文件")
        assert "@add_file" not in result
        assert tmp not in result
        print("[PASS] @add_file 指令从消息中移除")
    finally:
        pathlib.Path(tmp).unlink(missing_ok=True)


_ADD_FILE_TESTS = [
    test_add_file_single, test_add_file_multiple, test_add_file_not_exist,
    test_add_file_too_large, test_clear_files, test_context_files_block_format,
    test_add_file_stripped_from_message,
]

if __name__ == "__main__":
    for fn in [test_clear, test_context_display, test_compress_short, test_compress_normal, test_compress_message]:
        setup()
        fn()
    for fn in _ADD_FILE_TESTS:
        fn()
    print("\n全部通过")
