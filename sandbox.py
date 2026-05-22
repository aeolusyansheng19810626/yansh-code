"""命令沙箱（P1 #6 opt-in）

设计取舍：
  yansh 一直运行在宿主机 shell——已有的"黑名单 + 未识别命令确认"是第一道防线。
  本模块加第二道：进程隔离。仅在 `--sandbox docker` 显式 opt-in 时生效；
  默认行为不变（向后兼容、零 docker 依赖）。

  当前只包装 `execute_command`，因为它是唯一"会跑任意代码"的工具——
  其他工具（read_file / write_file / replace_*）都是受控的 IO，无需隔离。

策略：
  enabled=False（默认）→ wrap_command(cmd, ws) 原样返回 cmd
  enabled=True 且 backend=docker → 包成
    docker run --rm -i -v <ws_abs>:/ws -w /ws <image> sh -c '<cmd>'
  挂载 rw（不是 ro）：测试常需要写中间文件；隔离的核心收益是"文件系统 / 进程 / 网络都被命名空间隔离"，
  而不是只读。如果将来需要 ro 模式，可加子选项。
"""
import os
import shlex
from dataclasses import dataclass
from typing import Optional

DEFAULT_IMAGE = "python:3.11-slim"


@dataclass
class SandboxConfig:
    enabled: bool = False
    backend: str = "docker"   # 当前仅支持 docker；预留位
    image: str = DEFAULT_IMAGE
    extra_args: tuple = ()    # 例如 ("--network=none",)


# 进程级单例。main.py 解析 CLI 后调 set_config 设置；
# tools.execute_command 调 wrap_command 时读
_config = SandboxConfig()


def set_config(cfg: SandboxConfig) -> None:
    global _config
    _config = cfg


def get_config() -> SandboxConfig:
    return _config


def parse_cli_arg(value: Optional[str]) -> SandboxConfig:
    """解析 CLI 的 --sandbox 值。
      None / ""        → 禁用
      "docker"          → 启用 docker，默认 image
      "docker:image"    → 启用 docker 指定 image
      "none"            → 禁用（显式）
    """
    if not value or value.lower() == "none":
        return SandboxConfig(enabled=False)
    parts = value.split(":", 1)
    backend = parts[0].lower()
    if backend != "docker":
        raise ValueError(f"未知 sandbox backend: {backend}（当前仅支持 docker）")
    image = parts[1] if len(parts) > 1 and parts[1] else DEFAULT_IMAGE
    return SandboxConfig(enabled=True, backend=backend, image=image)


def wrap_command(command: str, workspace_dir: str) -> str:
    """按当前配置返回实际要执行的 shell 命令。
    禁用时原样返回。"""
    cfg = _config
    if not cfg.enabled:
        return command
    if cfg.backend != "docker":
        return command  # 占位

    ws_abs = os.path.abspath(workspace_dir)
    # docker run 单行：用 sh -c '<cmd>'，对内部用户命令 shlex.quote 防注入
    inner = command
    extra = " ".join(cfg.extra_args)
    return (
        f"docker run --rm -i "
        f"-v {shlex.quote(ws_abs)}:/ws "
        f"-w /ws "
        f"{extra + ' ' if extra else ''}"
        f"{shlex.quote(cfg.image)} "
        f"sh -c {shlex.quote(inner)}"
    )


def is_enabled() -> bool:
    return _config.enabled


def describe() -> str:
    """给启动横幅用的人话描述"""
    if not _config.enabled:
        return "off"
    return f"{_config.backend}:{_config.image}"
