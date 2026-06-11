# agent.py: _solo_drive + solo
# 行号 3957-4200
def _solo_drive(messages, tools, compact_state, *, soft_limit, start_tokens,
                budget_state, no_progress_state):
    """solo 主驱动循环。原地 append messages；budget_state / no_progress_state 跨 gate 回灌持续累积。
    返回 {"early_exit", "success", "summary", "rounds_used"}.
    """
    from subagent import _WRITE_TOOLS
    rounds_used = 0
    silent_prompted = False  # 沉默退出兜底：本次 drive 内只追问一次

    while no_progress_state["total_rounds"] < soft_limit:
        rounds_used += 1
        no_progress_state["total_rounds"] += 1
        if interrupt.is_interrupted():
            raise interrupt.Interrupted()

        # 每轮 call_llm 前 auto-compact，防连续 context O(N²) 膨胀全量重发
        messages[:] = _maybe_compact_messages(messages, compact_state, label="solo-compact")

        # token 预算软提醒（只一次）
        if not budget_state["warned"]:
            used = get_session_total_tokens() - start_tokens
            if used > _SOLO_TOKEN_BUDGET:
                console.print(f"[预算] solo token 增量 {used} > {_SOLO_TOKEN_BUDGET}，提醒 LLM 收敛",
                              style="yellow", highlight=False)
                messages.append({
                    "role": "system",
                    "content": (
                        f"You have used {used} tokens (budget {_SOLO_TOKEN_BUDGET}). "
                        "Converge: finish remaining files and tests, run them, then task_complete. "
                        "Do not start large new explorations."
                    ),
                })
                budget_state["warned"] = True

        response = call_llm(messages, tools=tools, tool_choice="auto")
        msg = response.choices[0].message
        messages.append({
            "role": "assistant",
            "content": msg.content,
            "tool_calls": [tc.model_dump() for tc in msg.tool_calls] if msg.tool_calls else None,
        })

        if msg.tool_calls:
            _rn = no_progress_state["total_rounds"]
            console.print(f"solo 轮 {_rn}: {len(msg.tool_calls)} 次工具调用")
            outs = _dispatch_tool_calls(
                msg.tool_calls, mode="code", allow_hil=True, allow_confirm=False,
                snap=_CURRENT_SNAPSHOT, messages=messages, console_label=f"solo 轮 {_rn}",
            )
            # sentinel：LLM 主动声明任务结束
            for out in outs:
                if out["result"].get("_task_complete"):
                    success = bool(out["result"].get("success"))
                    summary = out["result"].get("summary", "")
                    console.print(f"solo task_complete（{'成功' if success else '放弃'}）：{summary}",
                                  style=None if success else "yellow", highlight=False)
                    return {"early_exit": True, "success": success,
                            "summary": summary, "rounds_used": rounds_used}
            # agent 级 no_progress：本轮是否有「正当进展」（区别于逐文件 no_progress）。
            # 端到端模式下，跑真实入口验证（execute_command）也是进展，不算空转——
            # R10 实测：写完全部模块后用 execute_command 连跑验证 12 轮被误熔断。
            # 真正的空转 = 纯 read/search/list/git 多轮无写无跑（R9 的探索死循环）。
            productive = any(
                tc.function.name in _WRITE_TOOLS or tc.function.name == "execute_command"
                for tc in msg.tool_calls
            )
            if productive:
                no_progress_state["streak"] = 0
            else:
                no_progress_state["streak"] += 1
                if no_progress_state["streak"] >= 2 * _SOLO_NO_PROGRESS_CAP:
                    console.print(f"[solo] 连续 {no_progress_state['streak']} 轮无写编辑/无运行，熔断",
                                  style="yellow", highlight=False)
                    return {"early_exit": False, "success": False,
                            "summary": "连续多轮无写编辑且未运行任何命令，疑似探索死循环，熔断", "rounds_used": rounds_used}
                if no_progress_state["streak"] == _SOLO_NO_PROGRESS_CAP:
                    messages.append({
                        "role": "system",
                        "content": (
                            f"You have gone {_SOLO_NO_PROGRESS_CAP} rounds doing only read/search/list "
                            "(no file edits, no command runs). If you are stuck exploring, commit to writing the "
                            "next file now using write_file, or run your entry point to verify. "
                            "If genuinely blocked, task_complete(success=false, summary=...)."
                        ),
                    })
        else:
            # 沉默退出兜底：第一次没调工具 → 追问一次
            if not silent_prompted:
                silent_prompted = True
                console.print("[兜底] solo 未调工具，追问一次要求显式 task_complete", style="yellow", highlight=False)
                messages.append({
                    "role": "system",
                    "content": (
                        "You did not call any tool this turn — by protocol you must terminate explicitly with task_complete(success, summary). "
                        "If everything is done and tests pass, task_complete(success=true, summary=...); "
                        "if you cannot continue, task_complete(success=false, summary=...)."
                    ),
                })
                continue
            console.print("solo 结束（沉默退出，已追问过一次）")
            return {"early_exit": False, "success": False,
                    "summary": "沉默退出", "rounds_used": rounds_used}

    console.print(f"[警告] solo 已达 {soft_limit} 轮上限，强制退出", style="yellow", highlight=False)
    return {"early_exit": False, "success": False,
            "summary": f"达到 {soft_limit} 轮上限", "rounds_used": rounds_used}


def solo(requirement, model_override=None):
    """单一连续 context 端到端 agent：自主规划→读写跑修→自测，外部 test gate 回灌复核。
    返回 {"success", "test_result", "task_complete_signal"?}（与 audit 分支对齐，保证 batch --json）。
    """
    console.print("[Agent: Solo]", highlight=False)

    # system prompt：role + 项目规则 + workspace 顶层符号索引（借 audit 的注入逻辑）
    ws_symbols_result = workspace_symbols()
    if "error" in ws_symbols_result:
        symbols_brief = f"(workspace_symbols failed: {ws_symbols_result['error']})"
    else:
        files_map = ws_symbols_result.get("files", {})
        subdirs_map = ws_symbols_result.get("subdirs", {})
        lines = []
        for path, syms in sorted(files_map.items()):
            head = ", ".join(f"{s['name']}({s['type'][0]}:L{s['line']})" for s in syms[:30])
            extra = f" +{len(syms)-30}" if len(syms) > 30 else ""
            lines.append(f"  {path}: {head}{extra}")
        if subdirs_map:
            lines.append("")
            lines.append("Sub-directories (drill in with workspace_symbols(path='<dir>')):")
            for d, info in sorted(subdirs_map.items()):
                lines.append(f"  {d}/  ({info['py_files']} .py files / {info['total_symbols']} symbols)")
        symbols_brief = (
            f"Workspace top-level symbol index ({ws_symbols_result['total_files']} files / "
            f"{ws_symbols_result['total_symbols']} symbols):\n" + "\n".join(lines)
        )

    sys_prompt = f"{_SOLO_ROLE}{_get_project_rules()}\n\n{symbols_brief}"
    sys_prompt = _append_active_prompts(sys_prompt)

    # 任务开始时注入持久环境知识（框架自动维护，跨 run 复用）
    _state_path = Path(_get_workspace()) / ".yansh" / "agent_state.md"
    try:
        _state_content = _state_path.read_text(encoding="utf-8").strip()
        if _state_content:
            _MAX_STATE = 4000
            if len(_state_content) > _MAX_STATE:
                _state_content = _state_content[:_MAX_STATE] + "\n[...已截断]"
            sys_prompt += f"\n\n[持久环境知识 .yansh/agent_state.md — 框架自动维护，跨 run 有效]\n{_state_content}"
    except Exception:
        pass

    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": f"Task: {requirement}"},
    ]

    tools = _solo_tools()

    # 快照：solo 不预知文件清单，用空快照；写工具按需增量备份（供 /revert）
    global _CURRENT_SNAPSHOT
    _gc_old_snapshots(keep=10)
    _CURRENT_SNAPSHOT = create_snapshot([])

    compact_state = _make_compact_state()
    start_tokens = get_session_total_tokens()
    budget_state = {"warned": False}
    no_progress_state = {"streak": 0, "total_rounds": 0}

    signal = _solo_drive(messages, tools, compact_state, soft_limit=_SOLO_SOFT_LIMIT,
                         start_tokens=start_tokens, budget_state=budget_state,
                         no_progress_state=no_progress_state)

    # 外部 test gate 回灌：files_modified 推断 scope → 跑测试 → 红则把 stderr append 进同一 messages 继续驱动修复
    test_result = {"returncode": 0, "stdout": "", "stderr": ""}
    gate_round = 0
    while gate_round < _SOLO_GATE_MAX_ROUNDS:
        modified = _task_log_mod.snapshot_files_modified()
        scope = _infer_test_scope([{"filename": f} for f in modified])
        _ws_path = Path(_get_workspace())
        test_cmd = (_detect_python_test_cmd(_ws_path, scope=scope)
                    or _detect_python_test_cmd(_ws_path))
        if not test_cmd:
            console.print("[solo gate] 无可用测试命令，跳过外部复核", style="yellow", highlight=False)
            break
        test_result = test(test_cmd)
        if judge(test_result):
            console.print(f"[solo gate] 测试通过（{test_cmd}）", style="green", highlight=False)
            signal["success"] = True
            break
        gate_round += 1
        if no_progress_state["total_rounds"] >= _SOLO_SOFT_LIMIT:
            console.print("[solo gate] 主 loop 轮次已耗尽，无法继续回灌修复", style="yellow", highlight=False)
            signal["success"] = False
            break
        raw = test_result.get("stderr") or test_result.get("stdout") or "测试失败（无输出）"
        err_excerpt = raw[-4000:] if len(raw) > 4000 else raw
        console.print(f"[solo gate] 测试失败，回灌驱动修复（gate 轮 {gate_round}）", style="yellow", highlight=False)
        messages.append({
            "role": "user",
            "content": (
                f"外部 test gate 运行 `{test_cmd}` 失败。错误输出（末尾 4000 字）：\n```\n{err_excerpt}\n```\n"
                "请在当前 context 内定位并修复，跑绿后再 task_complete。禁止弱化断言来骗过测试。"
            ),
        })
        signal = _solo_drive(messages, tools, compact_state, soft_limit=_SOLO_SOFT_LIMIT,
                             start_tokens=start_tokens, budget_state=budget_state,
                             no_progress_state=no_progress_state)
    else:
        console.print(f"[solo gate] 回灌已达 {_SOLO_GATE_MAX_ROUNDS} 轮上限", style="yellow", highlight=False)
        signal["success"] = bool(judge(test_result))

    success = bool(signal.get("success"))
    return {
        "success": success,
        "test_result": test_result,
        "task_complete_signal": {
            "early_exit": signal.get("early_exit", False),
            "success": success,
            "summary": signal.get("summary", ""),
        },
    }


def test(test_command):
    """执行测试命令"""
    if not test_command or not test_command.strip():
        console.print("警告：无测试命令，跳过测试")
        return {"returncode": 0, "stdout": "", "stderr": ""}
    
    console.print(f"执行测试：{test_command}")
    return execute_command(test_command)

def judge(test_result):
    """判断测试是否通过"""
    return test_result.get("returncode") == 0


def _parse_pytest_failures(text: str) -> set:
    """从 pytest 输出抽 FAILED 行的 test id（形如 'tests/x.py::test_y'）。
    pytest 短摘要行格式：`FAILED <nodeid> - <msg...>` 或 `FAILED <nodeid>`。
    解析失败返回空集，不抛异常。"""
    if not text:
        return set()
    import re as _re
