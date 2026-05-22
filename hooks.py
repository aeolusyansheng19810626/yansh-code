"""Hooks 系统（P2 #11 最小版）

让用户用 shell 命令 hook 进 yansh 的关键事件，对齐 Claude Code 的 settings.json hooks。

事件（4 种）：
  - PreToolUse        工具调用前（可 block / 改 tool_input）
  - PostToolUse       工具调用后（可改 tool_output；block 无意义已发生）
  - UserPromptSubmit  用户输入提交后（可 block / 加 system_message）
  - Stop              任务完成（task_complete 后；可加 system_message）

配置（mcp 同款双路径，项目级优先）：
  <workspace>/.yansh/hooks.json
  ~/.yansh/hooks.json

格式（兼容 Claude Code）：
  {
    "hooks": {
      "PreToolUse": [
        {"matcher": "write_file",
         "hooks": [{"type": "command", "command": "node check.js", "timeout": 10}]}
      ],
      "PostToolUse": [...],
      "UserPromptSubmit": [...],
      "Stop": [...]
    }
  }

Hook 子进程协议：
  stdin  ← {"event": "...", "tool_name": "...", "tool_input": {...}, "cwd": "...",
            "tool_output": {...}, "user_input": "...", "task_summary": "..."}
  stdout → {} 表示 allow（默认）
         → {"decision": "block", "reason": "..."} 阻止
         → {"modify": {"tool_input": {...}, "tool_output": {...}}} 改输入/输出
         → {"system_message": "..."} 给 LLM 加 context

聚合规则：
  - 多个匹配 hook 串行跑（按配置顺序）
  - 任意一个 block → 整体 block
  - modify 累积（后面的看到前面改完的）
  - system_message 全部收集，调用方决定怎么注入

跳过场景：
  - 批处理 --json 模式（hooks 可能交互/慢，不适合自动化）
  - 子 agent 内部（避免重复触发；父 agent 派工具时已触发）
  - hook 本身超时 / 非法 JSON / 命令不存在 → 默认 allow（不卡死主流程）

不做（留待下一波）：
  - matcher 正则
  - hook 优先级 / 互斥
  - 异步 hook（fire-and-forget）
  - hook 之间的依赖
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Optional


_VALID_EVENTS = ("PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop")
_DEFAULT_TIMEOUT_SEC = 10
_HOOKS_DISABLED = False  # 测试 / 批处理可临时关掉


def set_disabled(disabled: bool) -> None:
    """批处理 --json / 测试场景临时关 hooks。模块级开关。"""
    global _HOOKS_DISABLED
    _HOOKS_DISABLED = bool(disabled)


def is_disabled() -> bool:
    return _HOOKS_DISABLED


def _config_paths(workspace_dir: Optional[str] = None) -> list:
    paths = []
    if workspace_dir:
        paths.append(Path(workspace_dir) / ".yansh" / "hooks.json")
    paths.append(Path.home() / ".yansh" / "hooks.json")
    return paths


def load_config(workspace_dir: Optional[str] = None) -> dict:
    """加载 hooks.json（项目级 trust 通过才用项目级；否则 fallback 到 ~/.yansh）。

    P0 安全：hook 命令是 shell=True 直执行，恶意 repo 提交 .yansh/hooks.json 即可
    在用户首次输入时 RCE。默认拒绝项目级配置；交互模式首次见时 trust 后才加载。
    """
    import workspace_trust as _wt
    home_path = Path.home() / ".yansh" / "hooks.json"
    if workspace_dir:
        proj_path = Path(workspace_dir) / ".yansh" / "hooks.json"
        if proj_path.exists() and _wt.check_or_prompt(workspace_dir, "hooks.json"):
            try:
                return json.loads(proj_path.read_text(encoding="utf-8"))
            except Exception as e:
                return {"_error": f"解析失败 {proj_path}: {e}"}
    if home_path.exists():
        try:
            return json.loads(home_path.read_text(encoding="utf-8"))
        except Exception as e:
            return {"_error": f"解析失败 {home_path}: {e}"}
    return {}


def _matches(matcher: Optional[str], target: Optional[str]) -> bool:
    """matcher 匹配规则：
    - matcher 缺失 / 空字符串 / "*" → 匹配所有
    - 否则要求精确字符串相等
    """
    if not matcher or matcher == "*":
        return True
    return matcher == target


def _find_matching_hooks(cfg: dict, event: str,
                         match_target: Optional[str] = None) -> list:
    """从 hooks.json 里挑出当前事件 + matcher 命中的 hook 命令清单"""
    out = []
    rules = cfg.get("hooks", {}).get(event, [])
    if not isinstance(rules, list):
        return []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        if not _matches(rule.get("matcher"), match_target):
            continue
        for h in rule.get("hooks", []):
            if not isinstance(h, dict):
                continue
            if h.get("type", "command") != "command":
                continue
            cmd = h.get("command")
            if not cmd:
                continue
            out.append({
                "command": cmd,
                "timeout": int(h.get("timeout", _DEFAULT_TIMEOUT_SEC)),
            })
    return out


def _run_one_hook(hook: dict, payload: dict, cwd: Optional[str] = None) -> dict:
    """跑单个 hook 子进程。失败/超时返回 {} 表示 allow（不卡死主流程）。

    实现细节：用 Popen 手动管理（subprocess.run 在 shell=True + timeout 下
    Windows 上 kill 不到孙进程，会等到孙进程自然退出；这里超时后用平台特定
    手段 kill 整个进程树/组）。
    """
    import sys as _sys
    cmd = hook["command"]
    timeout = max(1, int(hook.get("timeout", _DEFAULT_TIMEOUT_SEC)))
    stdin_text = json.dumps(payload, ensure_ascii=False)

    popen_kwargs = dict(
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=True,
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    # 平台相关：开新进程组/任务对象，这样超时时能一并 kill 孙进程
    if _sys.platform == "win32":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True   # POSIX：开新 session 便于 killpg

    try:
        proc = subprocess.Popen(cmd, **popen_kwargs)
    except Exception as e:
        return {"_hook_error": f"hook 启动失败: {e}"}

    try:
        stdout, stderr = proc.communicate(input=stdin_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        # 平台相关 kill
        try:
            if _sys.platform == "win32":
                # taskkill /T /F 杀进程树（包括 cmd.exe 派生的孙进程）
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True, timeout=3,
                )
            else:
                import os as _os
                import signal as _signal
                _os.killpg(_os.getpgid(proc.pid), _signal.SIGKILL)
        except Exception:
            pass
        # wait 一下避免僵尸
        try:
            proc.communicate(timeout=2)
        except Exception:
            pass
        return {"_hook_error": f"hook 超时 {timeout}s: {cmd}"}
    except Exception as e:
        try:
            proc.kill()
        except Exception:
            pass
        return {"_hook_error": f"hook IO 异常: {e}"}

    out = (stdout or "").strip()
    if not out:
        return {}   # 空输出 = allow
    try:
        parsed = json.loads(out)
    except Exception:
        return {"_hook_error": f"hook 输出非法 JSON: {out[:200]}"}
    if not isinstance(parsed, dict):
        return {"_hook_error": f"hook 输出非 object: {parsed!r}"}
    return parsed


def run_hook_event(event: str, payload: dict,
                   match_target: Optional[str] = None,
                   workspace_dir: Optional[str] = None) -> dict:
    """触发某个事件的所有匹配 hooks，聚合结果。

    返回聚合 dict：
      - "decision": "allow"|"block"
      - "reason": str (block 时填)
      - "modify": dict (累积 modify 后的 tool_input/tool_output 等)
      - "system_messages": list[str] (所有 hook 给的 system_message)
      - "errors": list[str] (失败 hook 的错误信息——给用户看，不影响决策)
      - "ran": int (实际跑了几个 hook)

    任何一个 hook 决策 block → 整体 block，剩余 hook 不跑（节省）。
    hook 自身失败 / 超时 → 当作 allow，错误进 errors。
    """
    if _HOOKS_DISABLED:
        return {"decision": "allow", "ran": 0, "reason": "hooks disabled",
                "modify": {}, "system_messages": [], "errors": []}
    if event not in _VALID_EVENTS:
        return {"decision": "allow", "ran": 0,
                "reason": f"unknown event {event}",
                "modify": {}, "system_messages": [], "errors": []}

    cfg = load_config(workspace_dir)
    if "_error" in cfg:
        return {"decision": "allow", "ran": 0,
                "modify": {}, "system_messages": [],
                "errors": [cfg["_error"]]}

    matched = _find_matching_hooks(cfg, event, match_target)
    if not matched:
        return {"decision": "allow", "ran": 0,
                "modify": {}, "system_messages": [], "errors": []}

    aggregated_modify: dict = {}
    system_messages: list = []
    errors: list = []
    ran = 0
    decision = "allow"
    block_reason = ""

    # 把 modify 当作"链式更新"：后续 hook 看到前一个改过的输入
    current_payload = dict(payload)

    for h in matched:
        ran += 1
        result = _run_one_hook(h, current_payload, cwd=workspace_dir)
        err = result.get("_hook_error")
        if err:
            errors.append(err)
            continue
        # decision
        d = result.get("decision")
        if d == "block":
            decision = "block"
            block_reason = result.get("reason", "（hook 未给原因）")
            break   # 早退：剩余 hook 不跑
        # modify 累积
        mod = result.get("modify")
        if isinstance(mod, dict):
            for k, v in mod.items():
                aggregated_modify[k] = v
                current_payload[k] = v
        # system_message
        sm = result.get("system_message")
        if isinstance(sm, str) and sm:
            system_messages.append(sm)

    return {
        "decision": decision,
        "reason": block_reason,
        "modify": aggregated_modify,
        "system_messages": system_messages,
        "errors": errors,
        "ran": ran,
    }


def list_configured(workspace_dir: Optional[str] = None) -> dict:
    """给 /hooks 命令用：列出当前配置（不跑），返回 {event: [{matcher, command, timeout}, ...]}"""
    cfg = load_config(workspace_dir)
    if "_error" in cfg:
        return {"_error": cfg["_error"]}
    out = {}
    for ev in _VALID_EVENTS:
        items = []
        for rule in cfg.get("hooks", {}).get(ev, []) or []:
            if not isinstance(rule, dict):
                continue
            matcher = rule.get("matcher", "*")
            for h in rule.get("hooks", []) or []:
                if not isinstance(h, dict):
                    continue
                items.append({
                    "matcher": matcher,
                    "command": h.get("command", "")[:80],
                    "timeout": h.get("timeout", _DEFAULT_TIMEOUT_SEC),
                })
        if items:
            out[ev] = items
    return out
