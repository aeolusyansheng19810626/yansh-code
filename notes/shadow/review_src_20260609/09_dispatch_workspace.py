# 补充09: dispatch 返回结构(out[result]恒为dict?) + _get_workspace
# agent.py 1114-1130: _get_workspace
def _get_workspace() -> str:
    """每次调用都从 config 读取最新 WORKSPACE_DIR，确保 --cwd 生效后不使用旧值。
    本模块顶部 `from config import WORKSPACE_DIR` 是初次启动时的快照，--cwd 变更后该名不会同步。
    所有运行期路径拼接都应通过本函数读取。"""
    import config as _cfg_mod
    return _cfg_mod.WORKSPACE_DIR


def _reinit_paths():
    """--cwd 变更后重新初始化 agent / snapshot / task_log 中所有依赖 WORKSPACE_DIR 的模块级变量。"""
    global _YANSH_DIR, _LOG_DIR, _REPLAY_DIR, _HISTORY_FILE
    _wd = _get_workspace()
    _YANSH_DIR     = Path(_wd) / ".yansh"
    _LOG_DIR       = _YANSH_DIR / "logs"
    _REPLAY_DIR    = _YANSH_DIR / "replay"
    _HISTORY_FILE  = Path(_wd) / ".yansh_history.json"
    _snapshot_mod._reinit_paths()

# tools.py 184-188: _get_workspace
def _get_workspace() -> str:
    """每次调用都从 config 读取最新 WORKSPACE_DIR，确保 --cwd 生效后不使用旧值。"""
    import config as _cfg_mod
    return _cfg_mod.WORKSPACE_DIR


# agent.py 1567-1645: _dispatch_tool_call wrapper
def _dispatch_tool_call(tool_call, *, mode="auto", allow_hil=True, allow_confirm=True, snap=None) -> dict:
    """[P2 #11 wrapper] PreToolUse hook → 内部 dispatch → PostToolUse hook。

    跳过 hook 的场景：
    - 子 agent 内部（_is_in_subagent()）—— 父 agent 派工具时已触发，子 agent 不重复
    - hooks 模块全局禁用（批处理 --json 由 main.py 设置）
    """
    return _dispatch_tool_call_with_hooks(
        tool_call, mode=mode, allow_hil=allow_hil,
        allow_confirm=allow_confirm, snap=snap,
    )


def _dispatch_tool_call_with_hooks(tool_call, *, mode="auto", allow_hil=True,
                                   allow_confirm=True, snap=None) -> dict:
    """实际的 hook 包装实现。和 _dispatch_tool_call 是同一个东西，命名给单测能直接调。"""
    name = tool_call.function.name
    raw_args = tool_call.function.arguments or "{}"
    try:
        args_initial = json.loads(raw_args)
    except json.JSONDecodeError as e:
        return {"name": name, "args": {}, "id": tool_call.id,
                "result": {"error": f"Invalid JSON in arguments: {e}"}}

    # PreToolUse hook（子 agent 内 / 全局禁用时跳过）
    skip_hooks = _is_in_subagent() or _hooks_mod.is_disabled()
    if not skip_hooks:
        try:
            ws = _get_workspace()
            hr = _hooks_mod.run_hook_event(
                "PreToolUse",
                {"event": "PreToolUse", "tool_name": name,
                 "tool_input": args_initial, "cwd": ws},
                match_target=name,
                workspace_dir=ws,
            )
            if hr.get("ran", 0) > 0:
                if hr["decision"] == "block":
                    console.print(f"[hook] PreToolUse 阻止 {name}：{hr.get('reason', '')}",
                                  style="yellow", highlight=False)
                    return {"name": name, "args": args_initial, "id": tool_call.id,
                            "result": {"error": f"Hook 阻止 {name}：{hr.get('reason', '')}"}}
                # modify tool_input
                mod = hr.get("modify", {})
                if isinstance(mod.get("tool_input"), dict):
                    args_initial = mod["tool_input"]
                    console.print(f"[hook] PreToolUse 修改 {name} 输入", highlight=False)
                # hook 错误打印一次（不阻断）
                for err in hr.get("errors", []):
                    console.print(f"[hook] {err}", style="yellow", highlight=False)
        except Exception as e:
            console.print(f"[hook] PreToolUse 异常忽略：{e}", style="yellow", highlight=False)

    # 调内部 dispatch
    out = _dispatch_tool_call_inner(
        tool_call, args_initial, mode=mode, allow_hil=allow_hil,
        allow_confirm=allow_confirm, snap=snap,
    )

    # PostToolUse hook（同样跳过场景）
    if not skip_hooks:
        try:
            ws = _get_workspace()
            hr = _hooks_mod.run_hook_event(
                "PostToolUse",
                {"event": "PostToolUse", "tool_name": out["name"],
                 "tool_input": out["args"], "tool_output": out["result"], "cwd": ws},
                match_target=out["name"],
                workspace_dir=ws,
            )
            if hr.get("ran", 0) > 0:
                mod = hr.get("modify", {})
                if isinstance(mod.get("tool_output"), dict):
                    out["result"] = mod["tool_output"]
                    console.print(f"[hook] PostToolUse 修改 {out['name']} 输出", highlight=False)
                for err in hr.get("errors", []):
                    console.print(f"[hook] {err}", style="yellow", highlight=False)
        except Exception as e:
            console.print(f"[hook] PostToolUse 异常忽略：{e}", style="yellow", highlight=False)
