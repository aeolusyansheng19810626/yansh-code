"""Skills 系统（P2 #8 最小版）

约定：每个 skill 是一个 markdown 文件，frontmatter 声明触发词与适用 mode；
正文（markdown）作为 prompt 片段拼进 system prompt。

目录：
  <workspace>/skills/*.md   项目级（优先）
  ~/.yansh/skills/*.md      全局（备选；项目级未命中时尝试）

文件示例：
  ---
  name: code-review
  description: 代码审查工作流
  triggers: ["review", "审查", "code review"]
  modes: ["audit", "plan"]
  ---
  ## 审查清单
  - 命名 / 风格 / 边界 / 错误处理 / 测试
  - ...

触发机制（最小版）：用户输入 / requirement 字符串与 triggers 关键字（不区分大小写）匹配命中。
注入点：plan() / code() / audit() / plan_chat() 的 system prompt 末尾。

不做（留待下一波）：
- LLM 智能匹配（基于上下文/历史而非纯关键字）
- skill 间依赖 / 优先级
- skill 安全沙箱（第三方 skill 多大程度能修改 agent 行为）
- 跨工作区 dry-run / disable
"""
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


@dataclass
class Skill:
    name: str
    description: str = ""
    triggers: list = field(default_factory=list)
    modes: list = field(default_factory=list)   # 空 list = 所有 mode 适用
    body: str = ""
    source_path: Optional[str] = None

    def applies_to_mode(self, mode: Optional[str]) -> bool:
        """modes 为空表示通用；否则 mode 必须在列表里"""
        if not self.modes:
            return True
        if mode is None:
            return True   # 未指定 mode 时也通用
        return mode in self.modes


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """最简 YAML 子集：key: value（字符串或 [list]）。返回 (meta, body)"""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    fm_text, body = m.group(1), m.group(2)
    meta = {}
    for line in fm_text.splitlines():
        line = line.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        # list: [a, b, "c d"]
        if val.startswith("[") and val.endswith("]"):
            inner = val[1:-1].strip()
            if not inner:
                meta[key] = []
            else:
                items = []
                # 简单分隔：考虑双引号包围的项
                for piece in _split_list(inner):
                    p = piece.strip()
                    if (p.startswith('"') and p.endswith('"')) or (p.startswith("'") and p.endswith("'")):
                        p = p[1:-1]
                    if p:
                        items.append(p)
                meta[key] = items
        else:
            # 标量：去引号
            if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                val = val[1:-1]
            meta[key] = val
    return meta, body.strip("\n")


def _split_list(s: str) -> list:
    """支持引号包围逗号的简单分割"""
    out = []
    cur = []
    in_str = None
    for ch in s:
        if in_str:
            cur.append(ch)
            if ch == in_str:
                in_str = None
        elif ch in ('"', "'"):
            in_str = ch
            cur.append(ch)
        elif ch == ",":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if cur:
        out.append("".join(cur))
    return out


def parse_skill_file(filepath: str) -> Optional[Skill]:
    """从一个 .md 文件解析 Skill。失败返回 None（不抛错——单个 skill 坏不应崩 yansh）"""
    try:
        text = Path(filepath).read_text(encoding="utf-8")
    except Exception:
        return None
    meta, body = _parse_frontmatter(text)
    name = meta.get("name") or Path(filepath).stem
    triggers = meta.get("triggers") or []
    modes = meta.get("modes") or []
    if not isinstance(triggers, list):
        triggers = [str(triggers)]
    if not isinstance(modes, list):
        modes = [str(modes)]
    return Skill(
        name=str(name),
        description=str(meta.get("description") or ""),
        triggers=[str(t).lower() for t in triggers],
        modes=[str(m) for m in modes],
        body=body or "",
        source_path=str(filepath),
    )


def _user_skills_dir() -> Path:
    """全局 skill 目录：~/.yansh/skills"""
    return Path.home() / ".yansh" / "skills"


def discover_skills(workspace_dir: Optional[str] = None) -> List[Skill]:
    """扫描项目级 + 全局 skill 目录。重名时项目级优先。"""
    skills: dict = {}
    # 全局优先扫——同名时项目级覆盖
    for d in (_user_skills_dir(), Path(workspace_dir) / "skills" if workspace_dir else None):
        if d is None or not d.exists():
            continue
        for f in sorted(d.glob("*.md")):
            sk = parse_skill_file(str(f))
            if sk is not None:
                skills[sk.name] = sk
    return list(skills.values())


def match_skills(user_input: str, skills: List[Skill],
                 mode: Optional[str] = None) -> List[Skill]:
    """关键字匹配：user_input 含任一 trigger（不区分大小写）即命中"""
    if not user_input:
        return []
    text = user_input.lower()
    matched = []
    for sk in skills:
        if not sk.applies_to_mode(mode):
            continue
        if any(t and t in text for t in sk.triggers):
            matched.append(sk)
    return matched


def format_skills_prompt(matched: List[Skill]) -> str:
    """把命中的 skill 格式化成 system prompt 片段"""
    if not matched:
        return ""
    parts = ["", "# 已加载的 skills（按需参考；非强制规则）"]
    for sk in matched:
        header = f"\n## skill: {sk.name}"
        if sk.description:
            header += f"\n*{sk.description}*"
        parts.append(header)
        if sk.body:
            parts.append(sk.body)
    return "\n".join(parts)


def load_and_format(user_input: str, workspace_dir: Optional[str] = None,
                    mode: Optional[str] = None) -> tuple[str, List[Skill]]:
    """便捷入口：扫描 + 匹配 + 格式化，一次返回 prompt 片段和命中列表。"""
    all_skills = discover_skills(workspace_dir)
    matched = match_skills(user_input, all_skills, mode=mode)
    return format_skills_prompt(matched), matched
