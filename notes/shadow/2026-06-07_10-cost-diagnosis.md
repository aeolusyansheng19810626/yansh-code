---
name: cost-diagnosis-r6
description: miniQL R6 $51 费用诊断 — fix() 内部循环无 auto-compact，O(N²) input 爆炸
metadata:
  type: project
---

# R6 $51 费用诊断

**日期**：2026-06-07  **对应 log**：20260607-222940-301249.jsonl

## 成本构成

sonnet 4.6 标准价 input $3/M、output $15/M：
- input **16.5M × $3 ≈ $49.5**
- output 98K × $15 ≈ $1.5
- 合计 $51 —— **92% 烧在 input**

## input:output ratio 趋势（指纹）

| 轮 | cost | input | ratio | tool_calls |
|---|---|---|---|---|
| R3 | $5.79 | 1.5M | 17:1 | 114 |
| R4 | $27.80 | 8.82M | 98:1 | 124 |
| R5 | $30.71 | 9.83M | 108:1 | 128 |
| R6 | $51.31 | 16.77M | **166:1** | 130 |

tool_calls 几乎不变（114→130），input 翻 11 倍。浪费不在"操作多"，在"每次 LLM call 携带的上下文越来越大"。ratio 随 fix 轮次飙升——典型 "fix loop 无 compact" 指纹。

## 根因（两层）

### 1. fix() 内部循环无 auto-compact（主因，可控）

- `code()` 有 auto-compact（agent.py:2572，超 `compact_threshold_tokens` 默认 60K 触发 `_compact_messages`）。
- **`fix()` 的 while loop（agent.py:3765）完全没有 compact**。每轮 `call_llm(messages)` 后 `messages.append(...)`，线性累积、全量重发。
- fix() 内唯一 token 控制是 `budget_warned`（`_FIX_TOKEN_BUDGET`，3771-3783）——但它只**注入一句"收尾"文字提醒**，不删任何历史 message，messages 继续膨胀。
- 实测：单次 fix() 调用 `token 增量 518700`。fixer 在 5-8 文件间反复 read（test_smoke 14 次、types 9 次、parser 8 次），每个 read 结果几千 token 全堆在 messages 里，每轮 LLM call 全量重发 → O(N²)。
- 5 个 attempt × 各一次 fix()（每次几百 K）+ code() 阶段 = 16.5M。

### 2. ICA 网关不透传 prompt cache（结构性，暂不可控）

`scripts/probe_ica_cache.py` 已探测：ICA 不透传 `cache_control`。重复前缀全额计费。cc 在这里走缓存省 ~90%。

## cc 对比

| 维度 | cc | yansh on ICA |
|---|---|---|
| prompt caching | 有，重复前缀收 10% | 无，全额 |
| 修复 loop 上下文管理 | 有 | code() 有、**fix() 没有** |

cc 同样任务大概率 $5-10 量级。

## 修复方向

**最高性价比**：把 `_compact_messages` 接到 `fix()` 的 while loop（agent.py:3765），每轮开头检测 `_estimate_messages_tokens(messages)` 超阈值就 compact。复用 code() 现成的 compact 逻辑（2572-2618，含 thrashing 保护）。预计砍掉 fix 阶段 50-70% input。

注意点：
- fix() 的 messages 结构 = [system, user] + N×(assistant tool_calls + tool results)。compact 要保留 system + 初始 user + 最近 K 轮，中间历史 summarize。
- 复用 `compact_threshold_tokens` / `compact_keep_recent_pairs` / thrashing 保护（`_compact_disabled`）配置。
- code() 的 compact 是抽好的 `_compact_messages(msgs, keep_recent_pairs)`，fix() 直接调即可。
