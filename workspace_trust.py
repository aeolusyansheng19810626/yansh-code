"""项目级配置 trust 检查（P0 安全修复）

防止恶意 repo 通过 .yansh/{mcp,hooks}.json 实现无确认 RCE：
  - clone 不可信 repo → 启动 yansh → 第一次输入触发 UserPromptSubmit hook → 任意命令执行

设计：
  - 默认拒绝项目级配置（mcp.json / hooks.json）；只加载 ~/.yansh 全局配置
  - 第一次见某 workspace 的项目配置时，交互模式弹 prompt 询问 trust
  - 用户答 y → 写入 ~/.yansh/trusted_workspaces.json，下次自动加载
  - 非交互模式（CI / 批处理）默认拒绝（用 env var YANSH_TRUST_PROJECT_CONFIG=always 显式 opt-in）
  - 测试用 env var bypass（tests/run_unit.py 设 always）

env var YANSH_TRUST_PROJECT_CONFIG：
  - "always"：总信任（CI / 测试 / 自己的开发机）
  - "never"：总拒绝
  - 不设 / "auto"：看 trust 文件 + 交互式问
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional

_TRUST_FILE_NAME = "trusted_workspaces.json"


def _trust_file() -> Path:
    return Path.home() / ".yansh" / _TRUST_FILE_NAME


def _trust_mode() -> str:
    return os.getenv("YANSH_TRUST_PROJECT_CONFIG", "auto").strip().lower()


def _resolve(workspace_dir: str) -> str:
    try:
        return str(Path(workspace_dir).resolve())
    except Exception:
        return str(workspace_dir)


def is_trusted(workspace_dir: Optional[str]) -> bool:
    """workspace 是否在 trust 白名单里。"""
    if not workspace_dir:
        return False
    f = _trust_file()
    if not f.exists():
        return False
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return False
    trusted = set(data.get("trusted", []))
    return _resolve(workspace_dir) in trusted


def mark_trusted(workspace_dir: str) -> None:
    """显式把 workspace 加进 trust 白名单（写盘）"""
    if not workspace_dir:
        return
    f = _trust_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if f.exists():
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    trusted = set(data.get("trusted", []))
    trusted.add(_resolve(workspace_dir))
    data["trusted"] = sorted(trusted)
    f.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                 encoding="utf-8")


def is_interactive() -> bool:
    """当前 stdin 是否是 tty（能 input prompt）"""
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except Exception:
        return False


def _prompt_user(workspace_dir: str, config_filename: str) -> bool:
    """交互式弹 trust 确认。返回 True 信任、False 拒绝。"""
    proj_path = Path(workspace_dir) / ".yansh" / config_filename
    print("", flush=True)
    print("=" * 70, flush=True)
    print("⚠️  [安全] 检测到项目级配置文件:", flush=True)
    print(f"      {proj_path}", flush=True)
    print(f"   workspace: {workspace_dir}", flush=True)
    print("", flush=True)
    print("项目级配置可能包含可执行命令（hook shell 命令 / MCP server 启动命令），", flush=True)
    print("加载即等同执行。克隆未知 repo 时请勿轻易信任。", flush=True)
    print("=" * 70, flush=True)
    try:
        ans = input("信任此 workspace 加载项目级配置？(y/N): ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        ans = ""
    if ans == "y":
        try:
            mark_trusted(workspace_dir)
            print(f"[trust] 已记入 {_trust_file()}", flush=True)
        except Exception as e:
            print(f"[trust] 写入失败：{e}（仅本次有效）", flush=True)
        return True
    print(f"[trust] 已拒绝；本次只加载 ~/.yansh/{config_filename}（如存在）", flush=True)
    return False


def check_or_prompt(workspace_dir: Optional[str], config_filename: str) -> bool:
    """加载项目级配置前调一次。返回 True 则允许加载、False 则跳过。

    config_filename: "mcp.json" / "hooks.json" —— 用来弹 prompt 时显示路径。
    """
    if not workspace_dir:
        return False
    proj_path = Path(workspace_dir) / ".yansh" / config_filename
    if not proj_path.exists():
        return False  # 没项目配置 → 无所谓 trust

    mode = _trust_mode()
    if mode == "always":
        return True
    if mode == "never":
        return False

    # auto 模式：看 trust 白名单 + 交互式 prompt
    if is_trusted(workspace_dir):
        return True
    if not is_interactive():
        # 非交互（CI / 批处理）默认拒绝——用户必须 env var 显式 opt-in
        try:
            print(
                f"[trust] 非交互模式拒绝项目级配置 {proj_path}；"
                f"如要加载请设 YANSH_TRUST_PROJECT_CONFIG=always",
                flush=True,
            )
        except Exception:
            pass
        return False
    return _prompt_user(workspace_dir, config_filename)
