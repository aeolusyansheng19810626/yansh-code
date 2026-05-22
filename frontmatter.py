"""共用的 YAML frontmatter 解析（最简子集，不依赖 pyyaml）。

P4 重构：skills.py / memory.py 各写一份半残的解析器，行为已不一致。
统一到这里，让所有 frontmatter-flavored 配置走同一份。

支持：
  - `key: value`（标量，去左右引号 ' " ）
  - `key: [a, b, "c d"]`（list，引号内逗号不切）
  - 一级嵌套：
      metadata:
        type: x
      → meta["metadata"] = {"type": "x"}

不支持（用 pyyaml 才能完整覆盖）：
  - 多级嵌套（> 1 层）
  - block 字符串（| / >）
  - anchor / alias

边界：
  - 解析失败（无 frontmatter 边界）→ 返回 ({}, text)，不抛
  - 内容含 frontmatter 终止符的边缘 case 不防（# 注释支持）
"""
from __future__ import annotations

import re
from typing import Tuple

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


def _split_list(s: str) -> list:
    """`[a, "b, c", 'd']` 内的逗号分割——引号包围的逗号不当分隔符"""
    out = []
    cur: list = []
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


def _strip_quotes(s: str) -> str:
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        return s[1:-1]
    return s


def parse(text: str) -> Tuple[dict, str]:
    """解析 frontmatter，返回 (meta_dict, body)。

    meta_dict 的值可能是：
      - str（标量）
      - list[str]（[a, b, c] 语法）
      - dict[str, str]（一级嵌套，如 metadata: {type: x}）
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    fm_text, body = m.group(1), m.group(2)
    meta: dict = {}

    # 当前处于哪个嵌套块（None = 顶级；str = 嵌套 key 名）
    nest_key: str = ""

    for line in fm_text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue

        # 缩进行——属于嵌套块
        if line.startswith(" ") or line.startswith("\t"):
            stripped = line.strip()
            if nest_key and ":" in stripped:
                k, _, v = stripped.partition(":")
                meta.setdefault(nest_key, {})
                if isinstance(meta[nest_key], dict):
                    meta[nest_key][k.strip()] = _strip_quotes(v.strip())
            continue

        # 顶级行——重置 nest_key
        nest_key = ""
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()

        # 空 value + 后续缩进行 = 嵌套块开始
        if not val:
            nest_key = key
            meta[key] = {}
            continue

        # list: [a, b, "c d"]
        if val.startswith("[") and val.endswith("]"):
            inner = val[1:-1].strip()
            if not inner:
                meta[key] = []
            else:
                items = []
                for piece in _split_list(inner):
                    p = _strip_quotes(piece.strip())
                    if p:
                        items.append(p)
                meta[key] = items
            continue

        # 标量
        meta[key] = _strip_quotes(val)

    return meta, body.strip("\n")
