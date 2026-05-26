"""子 Agent 执行器（P4-5：从 agent.py 拆出）

把"派子 agent / 跑子 agent loop / 子 agent 工具集与 system prompt"这一坨从
agent.py 抽到独立模块，让 agent.py 主流程更清爽，也方便后续给子 agent
单独加 token budget / per-subagent log 等扩展。

依赖关系：
  - 本模块顶层只依赖标准库 + tools_schema（避免循环 import）
  - agent.call_llm / agent._dispatch_tool_calls 等通过函数体内 lazy import
    访问——agent.py 顶部 import 本模块时不会触发循环
  - state.Session 通过 agent.py re-export 的别名访问 _SUBAGENT_STATS 等

向后兼容：
  - agent.py 的 _SUBAGENT_STATS / _subagent_state / _is_in_subagent /
    _set_in_subagent / _run_subagent / _subagent_handler / get_subagent_stats
    全部从本模块 re-export——既有调用方无需改动
"""
from __future__ import annotations

import threading
from typing import Optional

from console_shared import console

from tools_schema import TOOLS, READONLY_TOOL_NAMES


# ============================================================
# 状态：thread-local 防递归 + 全局累计 stats
# ============================================================

# 多个线程同时跑子 agent 时各自独立——_IN_SUBAGENT flag 必须 thread-local
_subagent_state = threading.local()

_SUBAGENT_STATS: dict = {
    "calls": 0,
    "total_steps": 0,
    "last_task": "",
    "last_summary": "",
    "last_role": "",
    "last_steps": 0,
    "last_success": False,
}
_SUBAGENT_STATS_LOCK = threading.Lock()

# 子 agent loop 上限——max_steps 参数会被 clamp 到 [1, _SUBAGENT_HARD_CAP]。
_SUBAGENT_HARD_CAP: int = 16
# 同时并发的子 agent 上限（thread pool size 上限）。
_SUBAGENT_CONCURRENCY_CAP: int = 4


def _is_in_subagent() -> bool:
    """当前线程是否处于子 agent 执行中（防递归）"""
    return bool(getattr(_subagent_state, "in_subagent", False))


def _set_in_subagent(value: bool) -> None:
    _subagent_state.in_subagent = bool(value)


# ============================================================
# system prompt 构建
# ============================================================

_SUBAGENT_ROLE = """[You are a Sub-Agent (an isolated work unit dispatched by a parent agent)]
You were dispatched to complete an independent sub-task. **Key constraints**:
- The parent agent cannot see your intermediate process — only the summary in your task_complete. So the summary must be concise, information-dense, and answer the parent's question directly
- You **cannot dispatch further sub-agents** (dispatch_subagent is disabled; recursion is locked)
- You must terminate with `task_complete(success, summary)` — do not exit silently (the loop will force-cut you)

[How to write the summary]
- Lead with a one-sentence conclusion: something the parent agent can use directly
- Then list key evidence / locations in clickable format like `agent.py:1620 plan_chat()`
- Don't replay the whole files you read — the parent wants your **digested** result
- If you don't know, say so plainly (success=False, summary='xxx 找不到/无法判断')

[Typical tasks]
- "Find out how X module is used" → give a list of callers + 1-2 typical usage examples
- "Find where Y function is" → give file path + line number + one-sentence description
- "Assess whether change Z affects W" → give the conclusion + list of involved files

Always respond in Chinese (用户的项目规则要求中文回复); summary 字段必须中文.
"""


def _subagent_tools_for_role(role: str) -> list:
    """根据 role 返回子 agent 可用的工具子集。物理隔离 dispatch_subagent 防递归。"""
    import agent as _a   # 用 agent._TOOLS_LOCK / _filter_tools
    with _a._TOOLS_LOCK:
        all_names = {t["function"]["name"] for t in TOOLS}
    blocked = {"dispatch_subagent", "update_plan_draft", "exit_plan_mode_signal"}
    if role in ("explorer", "auditor"):
        allowed = (READONLY_TOOL_NAMES - blocked)
    elif role == "general":
        allowed = (all_names - blocked)
    else:
        # 未知 role：保守只读
        allowed = (READONLY_TOOL_NAMES - blocked)
    return _a._filter_tools(allowed)


def _build_subagent_system_prompt(role: str) -> str:
    """子 agent 的 system prompt：role 简介 + workspace 顶层结构 + memory 索引"""
    import agent as _a
    from tools import workspace_symbols
    sp = _SUBAGENT_ROLE + _a._get_project_rules()
    if role == "general":
        sp += "\n\nYour role=general: you can write files and run commands — same permissions as the main agent. Use with care: editing code without parent's knowledge desynchronizes parent's understanding."
    else:
        sp += f"\n\nYour role={role}: read-only tools only — no file writes, no command execution."
    # 注入轻量顶层符号索引（top 模式，不递归）
    try:
        ws = workspace_symbols()
        if isinstance(ws, dict) and "error" not in ws:
            files_map = ws.get("files", {})
            subdirs_map = ws.get("subdirs", {})
            lines = []
            for p, syms in sorted(files_map.items()):
                head = ", ".join(s["name"] for s in syms[:20])
                extra = f" +{len(syms) - 20}" if len(syms) > 20 else ""
                lines.append(f"  {p}: {head}{extra}")
            if subdirs_map:
                lines.append("")
                lines.append("Sub-directories (drill in with workspace_symbols(path='...') or directory_summary(path='...')):")
                for d, info in sorted(subdirs_map.items()):
                    lines.append(f"  {d}/  ({info['py_files']} .py files / {info['total_symbols']} symbols)")
            sp += (
                f"\n\nWorkspace top-level structure ({ws.get('total_files', 0)} top-level files / "
                f"{ws.get('total_symbols', 0)} top-level symbols):\n" + "\n".join(lines)
            )
    except Exception:
        pass
    # P2 #12 Memory：子 agent 也能看到索引并 recall（READONLY 白名单含 save/recall_memory）
    try:
        import memory as _mem_mod
        mem_idx = _mem_mod.load_memory_index(_a._get_workspace())
        if mem_idx:
            sp += mem_idx
    except Exception:
        pass
    return sp


# ============================================================
# 主循环
# ============================================================

# 写工具清单——P4-4 用来从 outs 里识别 general role 子 agent 改了哪些文件
_WRITE_TOOLS = {
    "write_file", "replace_in_file", "apply_patch",
    "replace_symbol", "append_to_file", "delete_file",
    "move_file",
}


# P2.2：按 role 切便宜模型
# explorer/auditor 跑只读探索，sonnet 4.6 是浪费 → haiku 4.5（价格 ~1/3）
# general 子 agent 可能写代码，保持父 agent 第一档模型
_SUBAGENT_HAIKU_MODEL = "claude-haiku-4-5"  # ICA gateway ID 格式（无 -YYYYMMDD 后缀）


def _subagent_model_for_role(role: str) -> str | None:
    """返回该 role 应该用的模型 override；None 表示走父 cascade。

    explorer / auditor → haiku 4.5（只读，便宜模型够用）
    general → None（写代码场景需要 sonnet/opus 级别能力）
    """
    if role in ("explorer", "auditor"):
        return _SUBAGENT_HAIKU_MODEL
    return None


def _run_subagent(task: str, role: str = "explorer", max_steps: int = 8) -> dict:
    """跑一个子 agent，返回 {"success": bool, "summary": str, "steps": int, "role": str}.

    上下文完全隔离：独立 messages list，独立 sentinel 检测；不污染父 agent。
    防递归：进入时设 thread-local in_subagent=True，子 agent 看不到 dispatch_subagent 工具（双重保险）。
    并发：多个线程同时跑 _run_subagent 互不干扰（threading.local + stats 锁）。
    """
    # lazy import 避免循环 (agent 顶部 import subagent)
    import agent as _a
    import interrupt
    from llm_client import call_llm

    if _is_in_subagent():
        return {"success": False,
                "summary": "子 agent 不能再派子 agent（递归被禁用）。请在父 agent 直接做。",
                "steps": 0, "role": role}

    # P3 #6.1: 入口记当前线程 read_cache 累计——出口算 delta 回传给父 agent，
    # 让并发 worker 线程（threading.local 隔离）的 read 命中数能合并到主线程汇总
    _entry_total, _entry_hits, _ = _a._read_cache_stats()

    role = role if role in ("explorer", "general", "auditor") else "explorer"
    max_steps_clamped = max(1, min(int(max_steps or 8), _SUBAGENT_HARD_CAP))
    tools_subset = _subagent_tools_for_role(role)
    sys_prompt = _build_subagent_system_prompt(role)

    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": str(task or "")},
    ]

    # mode 决定 _dispatch_tool_call 内是否拦截写工具
    dispatch_mode = "audit" if role in ("explorer", "auditor") else "auto"

    summary = ""
    success = False
    steps = 0
    silent_prompted = False
    # P4-4：跟踪 general 子 agent 实际改了哪些文件——返回时塞进 summary，
    # 防 Lost Update（父 agent 不知子 agent 改了 X 后用旧 context 覆盖 X）
    files_modified_locally: list = []

    _set_in_subagent(True)
    try:
        while steps < max_steps_clamped:
            steps += 1
            if interrupt.is_interrupted():
                summary = summary or "（被中断）"
                break

            response = call_llm(messages, tools=tools_subset, tool_choice="auto",
                                stream=False, model_override=_subagent_model_for_role(role))
            msg = response.choices[0].message
            messages.append({
                "role": "assistant",
                "content": msg.content,
                "tool_calls": [tc.model_dump() for tc in msg.tool_calls] if msg.tool_calls else None,
            })

            if msg.tool_calls:
                # 子 agent 内部本就看不到 dispatch_subagent 工具（工具集物理过滤），
                # 所以并发逻辑实际不会触发——但用 helper 保持一致性
                outs = _a._dispatch_tool_calls(
                    msg.tool_calls, mode=dispatch_mode,
                    allow_hil=False, allow_confirm=False, snap=None,
                    messages=messages, console_label="",
                )
                done = False
                for out in outs:
                    if out["name"] == "task_complete":
                        success = bool(out["result"].get("success"))
                        summary = str(out["result"].get("summary") or "")
                        done = True
                    elif out["name"] in _WRITE_TOOLS and "success" in (out.get("result") or {}):
                        # 真写成功才记
                        a = out.get("args") or {}
                        fn = a.get("filename") or a.get("file_path") or a.get("dst") or ""
                        if fn and fn not in files_modified_locally:
                            files_modified_locally.append(fn)
                if done:
                    break
            else:
                # 沉默退出：兜底追问一次（同 plan_chat 套路）
                if not silent_prompted and not msg.content:
                    silent_prompted = True
                    messages.append({
                        "role": "system",
                        "content": "你这一轮没调任何工具也没输出文本。请用 task_complete(success, summary) 收尾。",
                    })
                    continue
                # 有 content 但没 task_complete：把 content 当 summary 兜底退出
                if msg.content and not summary:
                    summary = msg.content
                break
    finally:
        _set_in_subagent(False)

    if not summary:
        summary = "（子 agent 未声明 task_complete，已截断）"

    # P4-4：general 子 agent 改了文件 → 在 summary 末尾追加清单
    if role == "general" and files_modified_locally:
        summary += (
            "\n\n[系统提示] 此子任务修改了文件: "
            + ", ".join(files_modified_locally)
            + "；父 agent 后续操作前请重新 read_file 这些文件以刷新认知，"
            + "避免基于旧 context 生成的代码覆盖子任务的修改。"
        )

    # 更新 stats（截断防止历史污染日志）；并发场景需加锁保证计数器原子
    with _SUBAGENT_STATS_LOCK:
        _SUBAGENT_STATS["calls"] += 1
        _SUBAGENT_STATS["total_steps"] += steps
        _SUBAGENT_STATS["last_task"] = (task or "")[:200]
        _SUBAGENT_STATS["last_summary"] = summary[:500]
        _SUBAGENT_STATS["last_role"] = role
        _SUBAGENT_STATS["last_steps"] = steps
        _SUBAGENT_STATS["last_success"] = success

    # P3 #6.1: 算 delta 回传——主线程 _dispatch_tool_calls 只在并发分支合并
    _exit_total, _exit_hits, _ = _a._read_cache_stats()
    _read_cache_delta = (_exit_total - _entry_total, _exit_hits - _entry_hits)

    return {"success": success, "summary": summary, "steps": steps, "role": role,
            "_read_cache_delta": _read_cache_delta}


def _subagent_handler(task: Optional[str] = None, role: str = "explorer",
                      max_steps: int = 8) -> dict:
    """LLM 调 dispatch_subagent 时的 readonly_handlers 入口。
    返回轻量 dict 给父 agent 当作 tool result（不暴露子 agent 内部 messages）。"""
    import tools as _tools_mod
    if not task:
        return _tools_mod._err("invalid_args", "dispatch_subagent 需要 task 参数")
    res = _run_subagent(task, role=role, max_steps=max_steps)
    console.print(
        f"[subagent/{res['role']}] {res['steps']} 步 → "
        f"{'✓' if res['success'] else '✗'} {(res['summary'] or '')[:80]}",
        highlight=False,
    )
    return {
        "success": res["success"],
        "summary": res["summary"],
        "steps": res["steps"],
        "role": res["role"],
        # P3 #6.1: 旁路 metadata，让父 agent 主线程合并 worker 线程 read_cache delta；
        # 下划线前缀 LLM 通常忽略，且 schema 未声明此字段
        "_read_cache_delta": res.get("_read_cache_delta", (0, 0)),
    }


def get_subagent_stats() -> dict:
    """返回累计 stats 的浅拷贝，给 main.py /subagent stats 用。"""
    return dict(_SUBAGENT_STATS)
