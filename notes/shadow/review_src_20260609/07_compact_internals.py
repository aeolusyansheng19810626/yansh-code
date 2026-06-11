# 补充07: compact 内部（pair 配对 / summarize prompt）— 钉 M1 边界 + 摘要是否保签名
# agent.py 1281-1356
def _split_messages_into_pairs(rest):
    """P2 #4-B2：把 head 之后的 messages 按 assistant 边界切成 pair。

    每个 pair = 一条 assistant message + 紧随的 tool messages（配对完整）。
    若 rest 起始处有零散 user message（无 assistant 在前），单独成 pair。
    保证不破坏 tool_use/tool_result 配对。
    """
    pairs = []
    current = []
    for m in rest:
        role = _msg_role(m)
        if role == "assistant":
            if current:
                pairs.append(current)
            current = [m]
        else:
            if current:
                current.append(m)
            else:
                pairs.append([m])
    if current:
        pairs.append(current)
    return pairs


def _flatten_pairs_for_summary(pairs) -> str:
    """把若干 pair 拼成一段对话文本给 summarizer LLM 看"""
    chunks = []
    for p in pairs:
        for m in p:
            role = _msg_role(m) or "?"
            if isinstance(m, dict):
                content = m.get("content", "")
                tool_calls = m.get("tool_calls")
            else:
                content = getattr(m, "content", "") or ""
                tool_calls = getattr(m, "tool_calls", None)
            chunks.append(f"[{role}] {content}")
            if tool_calls:
                for tc in tool_calls:
                    if isinstance(tc, dict):
                        fn = tc.get("function", {}).get("name", "?")
                        args = tc.get("function", {}).get("arguments", "")
                    else:
                        fn = getattr(getattr(tc, "function", None), "name", "?")
                        args = getattr(getattr(tc, "function", None), "arguments", "")
                    chunks.append(f"  [tool_call] {fn}({args})")
    return "\n".join(chunks)


_SUMMARIZE_SYSTEM = (
    "你是对话历史压缩助手。下面是一段 coder agent 的历史对话（含工具调用与结果）。"
    "请用简洁中文概括：①已调过哪些工具/读了哪些文件 ②关键发现/读到的内容要点 "
    "③已经做了哪些代码改动 ④当前进展和未完成的任务 ⑤遇到的报错/卡点 "
    "⑥已验证可用的运行环境事实（如：哪个 python/pytest 命令可用、工作目录路径、"
    "哪些命令前缀会失败）。"
    "**强制项：第 ③ 点必须列出每个被改动的文件名 + 改动函数/区域**（如 'tools.py: read_file 函数'），"
    "不能泛化为'修改了 X.py 的某些函数'。"
    "**强制项：第 ⑥ 点必须逐字保留已验证成功的 shell 命令原文**（如 `py -3.11 -X utf8 -m pytest`），"
    "不可泛化或省略，这是后续轮次直接复用的关键信息。"
    "控制在 900 字内，重要事实不要丢。不要加客套话，直接给结构化要点。"
)


def _summarize_old_history(text: str) -> str:
    """调一次轻量 LLM 把旧对话压缩成摘要。

    review M3 修：stream=False 防止 summarize 内容流式打印到终端污染 UX。
    """
    summarize_msgs = [
        {"role": "system", "content": _SUMMARIZE_SYSTEM},
        {"role": "user", "content": text},
    ]
    resp = call_llm(summarize_msgs, tools=None, stream=False)
    return (resp.choices[0].message.content or "").strip()


# agent.py 1466-1479: _maybe_compact_messages 尾部 thrash disabled（补 01 文件被截断处）
        state["consecutive_over"] += 1
        console.print(
            f"[{label}] 未降低（{_est} → {_new_est}），计入 thrash "
            f"({state['consecutive_over']}/{state['max_consecutive']})",
            style="yellow",
        )
    if state["consecutive_over"] >= state["max_consecutive"]:
        console.print(
            f"[{label}] thrashing 保护：连续 {state['max_consecutive']} 次压缩无效，"
            f"禁用后续 compact 继续执行（est={_est}）",
            style="yellow",
        )
        state["disabled"] = True
    return msgs
