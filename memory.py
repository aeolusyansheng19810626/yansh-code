"""跨 Session 持久记忆系统（P2 #12 最小版）

跟 Claude Code memory 系统同款架构：
  - 每条 memory 一个 .md 文件，frontmatter 标元数据
  - MEMORY.md 索引每个目录一条，session 启动注入 system prompt
  - LLM 看到索引后按需调 recall_memory(name) 读完整内容
  - 4 种类型：user / feedback / project / reference

存储路径（双路径，项目级 + 全局）：
  <workspace>/.yansh/memory/<slug>.md      项目级（跟代码库走、可提交）
  <workspace>/.yansh/memory/MEMORY.md      项目级索引
  ~/.yansh/memory/<slug>.md                全局（跨项目、用户偏好）
  ~/.yansh/memory/MEMORY.md                全局索引

文件格式：
  ---
  name: <kebab-case>
  description: <一句话索引——决定调取相关性>
  metadata:
    type: user | feedback | project | reference
  ---
  <body>

不做（留待下一波）：
  - LLM 智能调取（当前依赖索引文字 + LLM 自主 recall_memory，复用 P2 #8 思路可后续加）
  - 跨 memory 的 [[link]] 解析
  - memory 过期 / 失效自动检测
  - 写时去重检测（同 slug 直接覆盖）
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


VALID_TYPES = ("user", "feedback", "project", "reference")


@dataclass
class Memory:
    name: str
    description: str = ""
    type: str = "project"
    body: str = ""
    scope: str = "project"   # project | global
    source_path: Optional[str] = None


# P4 重构：frontmatter 解析抽到 frontmatter.py，本模块透传以保留向后兼容。
from frontmatter import parse as _parse_frontmatter


def parse_memory_file(filepath: str, scope: str = "project") -> Optional[Memory]:
    """读取一个 .md 解析为 Memory。失败返回 None（坏文件不应崩主流程）"""
    try:
        text = Path(filepath).read_text(encoding="utf-8")
    except Exception:
        return None
    meta, body = _parse_frontmatter(text)
    name = str(meta.get("name") or Path(filepath).stem)
    desc = str(meta.get("description") or "")
    md = meta.get("metadata") or {}
    mtype = str(md.get("type") or "project")
    if mtype not in VALID_TYPES:
        mtype = "project"
    return Memory(
        name=name,
        description=desc,
        type=mtype,
        body=body or "",
        scope=scope,
        source_path=str(filepath),
    )


def _project_dir(workspace_dir: Optional[str]) -> Optional[Path]:
    if not workspace_dir:
        return None
    return Path(workspace_dir) / ".yansh" / "memory"


def _global_dir() -> Path:
    return Path.home() / ".yansh" / "memory"


def discover_memories(workspace_dir: Optional[str] = None) -> list:
    """扫两个目录返回所有 Memory。同名 slug 项目级覆盖全局。"""
    out: dict = {}
    # 全局先扫——同名时项目级覆盖
    for d, scope in ((_global_dir(), "global"),
                     (_project_dir(workspace_dir), "project")):
        if d is None or not d.exists():
            continue
        for f in sorted(d.glob("*.md")):
            if f.name == "MEMORY.md":
                continue   # 索引文件不当 memory
            mem = parse_memory_file(str(f), scope=scope)
            if mem is not None:
                out[mem.name] = mem
    return list(out.values())


def find_memory(name: str, workspace_dir: Optional[str] = None) -> Optional[Memory]:
    """按 name 找一条 memory（项目级优先）。

    P1 安全：name 必须先 _slugify——否则 recall_memory("../../README") 会读
    workspace 下任意 .md 文件。即便 slugify 之后，仍用 resolve() + is_relative_to
    再校验一次（防 slugify 后还能逃脱 target_dir 的边界情况）。
    """
    name = str(name).strip()
    if not name:
        return None
    slug = _slugify(name)
    for d, scope in ((_project_dir(workspace_dir), "project"),
                     (_global_dir(), "global")):
        if d is None or not d.exists():
            continue
        f = d / f"{slug}.md"
        # 双校验：resolve 后必须在 target_dir 之内（防 symlink / slugify 边界）
        try:
            f_resolved = f.resolve()
            d_resolved = d.resolve()
            if not str(f_resolved).startswith(str(d_resolved)):
                continue
        except Exception:
            continue
        if f.exists():
            return parse_memory_file(str(f), scope=scope)
    return None


def _slugify(name: str) -> str:
    """name → 安全的文件 slug：只保留字母数字下划线连字符"""
    s = re.sub(r"[^\w\-]+", "-", str(name).strip())
    s = re.sub(r"-+", "-", s).strip("-_")
    return s.lower() or "memory"


def save_memory(name: str, type: str, description: str, body: str,
                scope: str = "project",
                workspace_dir: Optional[str] = None) -> dict:
    """写入一条 memory，更新对应 MEMORY.md 索引。

    返回 {"saved": filepath, "scope": ..., "name": ...} 或 {"error": ...}。
    """
    if type not in VALID_TYPES:
        return {"error": f"非法 type: {type!r}（合法值 {VALID_TYPES}）"}
    if scope not in ("project", "global"):
        return {"error": f"非法 scope: {scope!r}（仅支持 project/global）"}
    slug = _slugify(name)
    if scope == "project":
        if not workspace_dir:
            return {"error": "scope=project 但未提供 workspace_dir"}
        target_dir = _project_dir(workspace_dir)
    else:
        target_dir = _global_dir()
    if target_dir is None:
        return {"error": "无法确定目标目录"}
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return {"error": f"创建目录失败: {e}"}
    filepath = target_dir / f"{slug}.md"
    desc_clean = (description or "").strip().replace("\n", " ")
    body_clean = (body or "").strip()
    content = (
        f"---\n"
        f"name: {slug}\n"
        f"description: {desc_clean}\n"
        f"metadata:\n"
        f"  type: {type}\n"
        f"---\n\n"
        f"{body_clean}\n"
    )
    try:
        filepath.write_text(content, encoding="utf-8")
    except Exception as e:
        return {"error": f"写入失败: {e}"}
    # 更新索引
    try:
        _update_index(target_dir, scope, workspace_dir)
    except Exception:
        pass   # 索引更新失败不应回滚 save——本体已落盘
    return {"saved": str(filepath), "scope": scope, "name": slug}


def delete_memory(name: str, scope: str = "project",
                  workspace_dir: Optional[str] = None) -> dict:
    """删除一条 memory，更新索引"""
    if scope not in ("project", "global"):
        return {"error": f"非法 scope: {scope!r}"}
    slug = _slugify(name)
    if scope == "project":
        if not workspace_dir:
            return {"error": "scope=project 但未提供 workspace_dir"}
        target_dir = _project_dir(workspace_dir)
    else:
        target_dir = _global_dir()
    if target_dir is None or not target_dir.exists():
        return {"error": f"目录不存在: {target_dir}"}
    filepath = target_dir / f"{slug}.md"
    if not filepath.exists():
        return {"error": f"memory 不存在: {slug}"}
    try:
        filepath.unlink()
    except Exception as e:
        return {"error": f"删除失败: {e}"}
    try:
        _update_index(target_dir, scope, workspace_dir)
    except Exception:
        pass
    return {"deleted": str(filepath), "scope": scope, "name": slug}


def _update_index(target_dir: Path, scope: str,
                  workspace_dir: Optional[str]) -> None:
    """重建 target_dir/MEMORY.md 索引：每行一条 `- [type] name — description`"""
    if not target_dir.exists():
        return
    rows = []
    for f in sorted(target_dir.glob("*.md")):
        if f.name == "MEMORY.md":
            continue
        mem = parse_memory_file(str(f), scope=scope)
        if mem is None:
            continue
        desc = mem.description or "(无描述)"
        rows.append(f"- [{mem.type}] {mem.name} — {desc}")
    header = f"# Memory 索引（{scope}）\n\n本目录下的 memory 清单——LLM 调 recall_memory(name) 读完整内容。\n\n"
    if rows:
        target_dir.joinpath("MEMORY.md").write_text(
            header + "\n".join(rows) + "\n", encoding="utf-8")
    else:
        # 没 memory 了——索引也清掉
        idx = target_dir / "MEMORY.md"
        if idx.exists():
            idx.unlink()


def load_memory_index(workspace_dir: Optional[str] = None) -> str:
    """加载两个目录的 MEMORY.md 拼成一段 system prompt 注入文本。
    无 memory 返回空字符串。
    """
    parts = []
    for d, scope_label in ((_project_dir(workspace_dir), "项目级"),
                           (_global_dir(), "全局")):
        if d is None:
            continue
        idx = d / "MEMORY.md" if d.exists() else None
        if idx is None or not idx.exists():
            continue
        try:
            text = idx.read_text(encoding="utf-8").strip()
        except Exception:
            continue
        if text:
            parts.append(f"## Memory 索引（{scope_label}）\n{text}")
    if not parts:
        return ""
    return (
        "\n\n# 跨 Session 记忆\n"
        "下面是已沉淀的事实/偏好的索引。**当前对话进行中如果需要某条的完整内容**，"
        "调 `recall_memory(name=\"...\")` 拉取——别凭印象猜。"
        "**遇到值得长期记住的事实**（用户偏好、项目背景、反馈、外部资源），"
        "用 `save_memory(name, type, description, body)` 写下来。\n\n"
        + "\n\n".join(parts)
    )


def list_all(workspace_dir: Optional[str] = None) -> list:
    """给 /memory list 用——返回所有 memory（dict 形式）含 scope"""
    out = []
    for mem in discover_memories(workspace_dir):
        out.append({
            "name": mem.name,
            "type": mem.type,
            "description": mem.description,
            "scope": mem.scope,
            "source_path": mem.source_path,
        })
    return out
