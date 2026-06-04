# architect explorer 探索深度限制

日期：2026-06-04

## 背景

complex 任务（如 task5：65 处调用点修改）中，plan() 前的 explorer subagent 没有 token 预算限制，最多跑 10 步，可能大量读文件消耗 token 但产出质量不稳定。

## 改动内容

### commit 178a047：初版实现

- `subagent.py`：`_run_subagent` 新增 `token_budget` 参数，超限注入收尾提示（软上限，仿 audit 模式）
- `agent.py`：plan() 内 explorer 调用 `max_steps` 10→6，`token_budget=50_000`
- explorer 任务提示收窄：优先 `search_in_files` 定位调用点数量，避免整文件读取

### commit 1b150d2：opus-4.8 review 修复

opus-4.8 review 发现 2 个 Major + 3 个 Minor：

| # | 问题 | 修复 |
|---|------|------|
| M1 | 全局 token 计数在并发 subagent 下偏大，预算可能提前误触发 | 注释说明软上限特性，budget 60K 留余量 |
| M2 | `_session_tokens_by_model` 累加无锁，并发 read-modify-write | `llm_client.py` 加 `_session_tokens_lock`，读写全部保护 |
| m3 | 收尾提示用 `role="user"`，与 audit/fix 不一致 | 改为 `role="system"` |
| m4 | `max_steps=6` 对大型任务偏紧 | 改为 8 |
| m5 | `if token_budget` 把 0 当无预算 | 改为 `is not None` |

## 最终参数

- `max_steps=8`（原 10，降低但保留余量）
- `token_budget=60_000`（软上限，实际因全局计数可能略偏大）
- explorer 任务：search-first 策略，优先定位数量而非读文件内容

## 关键经验

- `get_session_total_tokens()` 是全局累计，并发 subagent 时各自的增量会相互污染。短期内用宽松 budget 规避，根本修法是 per-thread token 计数（工作量较大，暂缓）。
- token 累加无锁是预存在问题，本次改动让并发路径依赖此计数，顺手修了。
- opus-4.8 review 再次发现真实 bug（M2 无锁），且用时 88s 极快（88K tokens）。
