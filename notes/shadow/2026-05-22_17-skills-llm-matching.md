# Skills LLM 智能匹配（P2 #8 续）

承接 [_16](./2026-05-22_16-skills-system.md)：用户聊到"能否导入 GitHub 上的 skill"，
顺势问到"想让模型自己判断要不要调用 skill"。这一波就把这个加上。

## 改了什么

### skills.match_skills 升级为分层决策

```
1. mode 过滤淘汰 → 候选列表 candidates
2. candidates 为空 → 返回 []（不调 LLM）
3. 关键字匹配（match_skills_keyword）→ 命中走 fast path 返回（不调 LLM）
4. use_llm=True → 调 _llm_select_skills
5. use_llm=False → 返回 []（向后兼容/测试场景）
```

短路设计的目标：**最常见的"显式关键字命中"零成本零延迟**；只在关键字模糊时才花钱调 LLM。

### `_llm_select_skills(user_input, candidates, mode)`

调 `llm_client.call_llm` 走当前 cascade（不强切 Haiku，避免改 client 状态）。
prompt 给候选清单的 (name, description, triggers 前 5 个示例)；要求 JSON 输出
`{"skills": ["name1", ...]}`；**宁缺勿滥**——拿不准就返回空。

失败处理：
- LLM 抛错 → 返回 None → caller 保守返回 []
- LLM 返回非法 JSON → 返回 None
- LLM 返回未知 skill name → 静默丢弃（用 `name_set` 做交集）
- 支持 `\`\`\`json ... \`\`\`` markdown 围栏（复用 `_extract_json` 逻辑写成内联）

### `match_skills` / `load_and_format` 加 `use_llm=True` 参数

向后兼容：`match_skills_keyword` 公开 API 保留；现有调用走 `match_skills(use_llm=True)` 默认是新行为。

### 旧的 `match_skills` 关键字版重命名为 `match_skills_keyword`

公开导出，作为：
- LLM 失败的降级路径
- 测试场景的纯关键字模式
- 用户想要"完全可预测"的部署可显式调

## 验证

### 单测（tests/unit/test_skills.py，新增 10 条）

- `test_match_skills_keyword_function_still_works` — 旧 API 公开
- `test_match_skills_keyword_hit_skips_llm` — 关键字命中时 spy 显示 LLM 调用次数 = 0
- `test_match_skills_no_keyword_calls_llm` — fake LLM 选 review，输入是 "code review"（不在 triggers 里）→ 验证 LLM 收到了候选清单
- `test_match_skills_llm_returns_empty` — LLM 主动判定都不适用 → []
- `test_match_skills_llm_failure_falls_back_to_empty` — `boom()` 抛错 → []（不崩）
- `test_match_skills_llm_invalid_json` — LLM 出非法 JSON → []
- `test_match_skills_use_llm_false_keyword_only` — 关键字不命中 + use_llm=False → []，spy 验证 0 次 LLM call
- `test_match_skills_no_candidates_skips_llm` — mode 过滤后无候选 → 立即返回 []，spy 验证 0 次 LLM call
- `test_match_skills_llm_filters_unknown_names` — LLM 给的 name 不在候选里 → 静默丢弃
- `test_match_skills_llm_with_markdown_codeblock` — `\`\`\`json ... \`\`\`` 围栏能解析

12/12 文件全过；test_skills.py 30/30。

### 集成验证（ICA Sonnet 4.6）

写两个 skill：
- **api-design**：description "REST API / HTTP 接口设计审查"，triggers `["api", "endpoint", "接口"]`
- **perf-review**：description "性能瓶颈审查"，triggers `["perf", "performance", "性能", "慢"]`

跑 5 场景：

| 输入 | 关键字命中 | LLM 决策 | 结果 |
|---|---|---|---|
| "看看 API **接口**设计如何" | ✅ "接口" 命中 fast path | 不调 LLM | api-design ✅ |
| "看看代码**效率**怎么样" | ❌ 无 perf/性能/慢 | LLM 选 perf-review | perf-review ✅ |
| "这个 **HTTP 服务**设计合不合理" | ❌ 无 api/接口/endpoint | LLM 选 api-design | api-design ✅ |
| "代码风格审查" | ❌ | LLM **不强行选** 返回空 | [] ✅ |
| "效率" + use_llm=False | ❌ | 不调 LLM | [] ✅（向后兼容） |

**关键证据**：
- LLM 能跨"字面关键字"做语义匹配——"效率→性能"、"HTTP 服务→API 设计"
- LLM 不会强行选——"代码风格审查"完全无关时返回空（这是 prompt 里"宁缺勿滥"指令生效）
- 关键字命中走 fast path——零延迟零成本
- use_llm=False 完全等价旧版关键字匹配

## 评估

### 跟上一波（关键字版）的本质区别

关键字版要求用户**显式**说出 trigger 词；LLM 版让用户用**自然语言**就能命中——
这才是 "Prompt as a Service" 真正的可用形态。社区分发的 skill 用户根本不需要记 triggers，
LLM 看 description 就能选对。

### 跟 Claude Code 的差距收窄

之前 ROADMAP 说"yansh 关键字匹配 vs Claude Code LLM 智能匹配"是核心差距。这一波
把这个差距收窄到——
- 都是 LLM 智能匹配 ✅
- 都支持显式 trigger 提示 ✅（yansh 的 triggers 字段相当于给 LLM 的硬关键词 hint）

剩下的差距：
- Claude Code 看上下文（对话历史、项目文件、最近改动）；yansh 只看当前一句 user_input
- Claude Code 能联合调用多个 skill；yansh 也支持但没专门优化
- Claude Code 有 skill 间依赖；yansh 还没

### 成本权衡

每次任务多一次 LLM call（约 200 input + 50 output tokens × Haiku 价格 ≈ $0.0003）——
当前 cascade 走 Sonnet 4.6 的话约 $0.0015。
但**关键字命中走 fast path** 把这个成本降到 0——常用场景不付费。
把 Haiku 作为强制路由的优化留给下一波（需要在 llm_client 加 `model` 参数路径）。

下一波（不在这次范围）：
- 强制走 Haiku（专用最便宜模型做这种轻量决策）
- 上下文感知（看对话历史 / project 状态）
- skill 间依赖：选了 A 时自动加载 A 声明依赖的 B
- skill 选择缓存：相同 input 短期内不重复调 LLM

## 关键文件

| 文件 | 改动 |
|---|---|
| `skills.py` | `match_skills` 重构成分层决策；`match_skills_keyword` 公开 API；新增 `_llm_select_skills`；`load_and_format` 加 `use_llm` |
| `tests/unit/test_skills.py` | +10 条单测，覆盖 fast path / LLM 路径 / 失败降级 / 边界 |
