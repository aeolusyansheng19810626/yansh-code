# agent.py: _compact_messages + _make_compact_state + _maybe_compact_messages + 状态注入
# 行号 1358-1470
def _compact_messages(msgs, keep_recent_pairs: int = 2):
    """P2 #4-B2：把旧 message 历史压缩成单条 system 摘要，保留最近 N 个 pair 原文。

    切分：[system, user_initial] + [old_pairs...] + [recent_N_pairs]
    保留头部（system + user_initial）+ 最近 N 个 pair 原文，旧 pair 走 LLM summarize。

    若 pair 总数 <= keep_recent_pairs，直接返回原 msgs（不必 compact）。

    review minor m3 修：keep=0 禁止（会丢光最新一轮 LLM 状态导致跑偏）。
    review M1 修：拼接前确保 recent_pairs 起始合法（不是孤立 user 直挂 summary 后）。
    """
    # review m3：keep=0 是退化路径，禁止使用
    if keep_recent_pairs <= 0:
        raise ValueError("keep_recent_pairs 必须 >= 1，否则会丢光最新一轮 LLM 状态")

    if not msgs or len(msgs) < 4:
        return msgs

    head_count = 0
    if _msg_role(msgs[0]) == "system":
        head_count = 1
    if len(msgs) > head_count and _msg_role(msgs[head_count]) == "user":
        head_count += 1
    if head_count == 0:
        return msgs  # 形态异常，保守不压

    head = list(msgs[:head_count])
    rest = list(msgs[head_count:])

    pairs = _split_messages_into_pairs(rest)
    if len(pairs) <= keep_recent_pairs:
        return msgs

    old_pairs = pairs[:-keep_recent_pairs]
    recent_pairs = pairs[-keep_recent_pairs:]

    # review M1：recent_pairs 第一个 pair 起始角色必须是 assistant 或 user
    # （否则 [summary_system, tool_result, ...] 序列违反 OpenAI tool_result 必紧跟 assistant 约束）
    # 若不合法，把 old_pairs 末尾的 pair 推回 recent_pairs，直到 recent 起始合法
    while recent_pairs and old_pairs:
        first_role = _msg_role(recent_pairs[0][0])
        if first_role in ("assistant", "user"):
            break
        recent_pairs.insert(0, old_pairs.pop())

    old_text = _flatten_pairs_for_summary(old_pairs)
    try:
        summary = _summarize_old_history(old_text)
    except Exception as e:
        # summarize 失败 → 不压（不能丢 history 让 LLM 跑偏）
        console.print(f"[auto-compact] summarize 失败，保留原 messages：{e}", style="yellow")
        return msgs

    if not summary:
        return msgs

    summary_content = f"[历史摘要 - 旧对话已压缩] {summary}"
    # 注入状态文件（若存在）：框架自动维护的环境知识（python/pytest 命令白/黑名单）
    _state_path = Path(_get_workspace()) / ".yansh" / "agent_state.md"
    try:
        _state_content = _state_path.read_text(encoding="utf-8").strip()
        if _state_content:
            _MAX_STATE = 4000  # 硬截断：防止超大状态文件抵消 compact 收益
            if len(_state_content) > _MAX_STATE:
                _state_content = _state_content[:_MAX_STATE] + "\n[...已截断]"
            summary_content += f"\n\n[持久环境知识 .yansh/agent_state.md — 框架自动维护，跨 run 有效]\n{_state_content}"
    except Exception:
        pass
    summary_msg = {"role": "system", "content": summary_content}

    new_msgs = head + [summary_msg]
    for p in recent_pairs:
        new_msgs.extend(p)
    return new_msgs


def _make_compact_state() -> dict:
    """auto-compact 跨轮状态（threshold/keep/thrashing 计数/disabled）。
    code() 与 fix() 各自的 LLM loop 共用，避免重复内联逻辑。"""
    return {
        "threshold": int(_cfg("compact_threshold_tokens") or 40_000),
        "keep_pairs": int(_cfg("compact_keep_recent_pairs") or 2),
        "consecutive_over": 0,
        "max_consecutive": int(_cfg("compact_max_consecutive_over") or 4),
        "disabled": False,
    }


def _maybe_compact_messages(msgs, state: dict, label: str = "auto-compact"):
    """每轮 call_llm 前检测并压缩 messages。原地更新 state（thrashing 计数/disabled）。
    返回压缩后的 msgs（可能原样返回）。含 thrashing 保护：连续 N 次压缩无效则禁用本任务后续 compact。"""
    if state.get("disabled"):
        return msgs
    _est = _estimate_messages_tokens(msgs)
    if _est <= state["threshold"]:
        return msgs
    console.print(f"[{label}] msgs ~{_est} tokens > {state['threshold']}，触发压缩...", style="cyan")
    _new_msgs = _compact_messages(msgs, keep_recent_pairs=state["keep_pairs"])
    _new_est = _estimate_messages_tokens(_new_msgs)
    if _new_est < _est:
        msgs = _new_msgs
        console.print(f"[{label}] 已压缩 {_est} → {_new_est} tokens", style="green")
        if (_est - _new_est) / _est < 0.15:
            state["consecutive_over"] += 1
        else:
            state["consecutive_over"] = 0
    else:
        # 估值未降低/反增 —— 计入 thrash 连续计数（不写回较大的 _new_msgs）
        state["consecutive_over"] += 1
        console.print(
            f"[{label}] 未降低（{_est} → {_new_est}），计入 thrash "
            f"({state['consecutive_over']}/{state['max_consecutive']})",
            style="yellow",
