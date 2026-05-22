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


# P4 重构：frontmatter 解析抽到 frontmatter.py，本模块透传以保留向后兼容。
from frontmatter import parse as _parse_frontmatter


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


def match_skills_keyword(user_input: str, skills: List[Skill],
                         mode: Optional[str] = None) -> List[Skill]:
    """关键字匹配（旧版逻辑保留为公开 API；快速、零成本、零延迟，作为 LLM 失败降级）"""
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


def _llm_select_skills(user_input: str, candidates: List[Skill],
                       mode: Optional[str] = None) -> Optional[List[str]]:
    """让 LLM 判断哪些 skill 适用。返回选中的 skill name 列表；失败返回 None（caller 降级）。

    设计：候选 metadata 给 LLM（name/description/triggers），用户输入也给。要求 JSON 输出。
    走当前 cascade 的最便宜模型（不强行切 Haiku，避免改 client 状态）。
    """
    if not candidates:
        return []
    try:
        import llm_client as _llm
        import json as _json

        listing = []
        for i, sk in enumerate(candidates, 1):
            triggers_hint = ", ".join(sk.triggers[:5]) if sk.triggers else "（无关键词）"
            listing.append(
                f"{i}. {sk.name} — {sk.description or '(无描述)'}\n"
                f"   关键词提示：{triggers_hint}"
            )
        listing_text = "\n".join(listing)
        mode_hint = f"（当前任务 mode={mode}）" if mode else ""

        sys_prompt = (
            "你是 yansh 的 skill 选择器。基于用户输入和可用 skill 清单，决定哪些 skill 适用。"
            "选择标准：skill 描述与用户意图实质相关（不必字面命中关键词）；宁缺勿滥——"
            "拿不准就不选。输出严格 JSON：{\"skills\": [\"name1\", ...]}；都不适用就 {\"skills\": []}。"
            "不要解释，只输出 JSON。"
        )
        user_msg = (
            f"用户输入：{user_input}\n{mode_hint}\n\n"
            f"可用 skill：\n{listing_text}\n\n"
            f"哪些 skill 适用？输出 JSON。"
        )
        messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_msg},
        ]
        resp = _llm.call_llm(messages, response_format={"type": "json_object"}, stream=False)
        content = (resp.choices[0].message.content or "").strip()
        if not content:
            return None
        # 复用 agent._extract_json 的逻辑，避免循环 import：手写一段最简
        text = content
        if "```" in text:
            import re as _re
            m = _re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
            if m:
                text = m.group(1).strip()
        else:
            obj_start = text.find("{")
            obj_end = text.rfind("}")
            if obj_start != -1 and obj_end > obj_start:
                text = text[obj_start:obj_end + 1]
        try:
            data = _json.loads(text)
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        names = data.get("skills")
        if not isinstance(names, list):
            return None
        return [str(n) for n in names if isinstance(n, str)]
    except Exception:
        return None


def match_skills(user_input: str, skills: List[Skill],
                 mode: Optional[str] = None,
                 use_llm: bool = True) -> List[Skill]:
    """智能匹配 skill。

    决策顺序：
      1) mode 过滤（modes 字段不允许的直接淘汰）
      2) 候选 0 个：返回空（无需 LLM）
      3) 候选 1 个：仍跑关键字匹配——若命中直接返回；否则按 use_llm 决定
      4) 候选多个 + use_llm：调 LLM 判断；失败降级关键字
      5) use_llm=False：用关键字匹配（向后兼容）

    use_llm=False 时本函数完全等价旧版关键字匹配。
    """
    if not user_input or not skills:
        return []
    candidates = [sk for sk in skills if sk.applies_to_mode(mode)]
    if not candidates:
        return []

    # 关键字命中优先（零成本短路）
    keyword_hits = match_skills_keyword(user_input, candidates, mode=mode)
    if keyword_hits:
        return keyword_hits

    if not use_llm:
        return []   # 无关键字命中 + 不让 LLM 判断

    # 候选很多 / 关键字未命中 → 让 LLM 判断
    selected = _llm_select_skills(user_input, candidates, mode=mode)
    if selected is None:
        return []   # LLM 调用失败：与"关键字未命中"一致，返回空（保守）
    name_set = {n for n in selected}
    return [sk for sk in candidates if sk.name in name_set]


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
                    mode: Optional[str] = None,
                    use_llm: bool = True) -> tuple[str, List[Skill]]:
    """便捷入口：扫描 + 匹配 + 格式化，一次返回 prompt 片段和命中列表。

    use_llm=True（默认）：关键字命中走 fast path，不命中时调 LLM 判断。
    use_llm=False：仅关键字匹配（向后兼容/测试场景）。
    """
    all_skills = discover_skills(workspace_dir)
    matched = match_skills(user_input, all_skills, mode=mode, use_llm=use_llm)
    return format_skills_prompt(matched), matched
