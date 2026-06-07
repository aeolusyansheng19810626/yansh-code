---
name: longrun-miniql-r7
description: yansh miniQL R7 — fix loop compact 修复后重跑，cost 大降但暴露 arity 震荡新瓶颈
metadata:
  type: project
---

# miniQL R7 基准（fix loop compact 修复后）

**日期**：2026-06-07  **对应修复**：commit d9cb8dc（fix loop 接入 auto-compact）  **max-cost**：默认 $50（未调高）

## 结果

| 项 | 值 |
|---|---|
| cost | **$9.90**（R6 $51.31，−81%） |
| input/output | 3.07M / 116K，ratio **27:1**（R6 166:1） |
| attempts | 6/6 用尽（未熔断，正常跑完） |
| 黑盒验收 | 0/10 |

## cost 大降的归因（须诚实）

- **compact 实际触发 0 次**（auto-compact / 已压缩均 0）。所以 R7 的低 cost **不能直接归功于 compact 修复**——这次 LLM 轨迹本身收敛（每轮 fix 内部 loop 浅，messages 没膨胀到超 60K 阈值）。
- compact 修复的价值是**安全网**：R6 那种 fixer 在多文件反复 read 导致单次 fix() 累积 518K 的失控场景，compact 会兜住。R7 没触发 = 这次没失控。
- 修复本身正确：单测验证 helper 逻辑 + fix() 接入点；code() 重构无回归（121 passed）。
- LLM 轨迹方差大：R6 vs R7 是两次独立生成，cost $51 vs $9.90 主要是轨迹差异。

## smoke test 机制：稳定生效 ✓

- R7 也生成了高质量 smoke test（449 行，走 subprocess + `python -m miniql`，不违规 import 内部）。
- smoke 自己跑 **20 failed, 9 passed** —— 确实抓到了 CLI 崩溃。
- fixer 被引向真实入口：`__main__.py` 提及 8 次、`build_logical_plan` 4 次。
- 结论：R6 的机制验证在 R7 复现，smoke test 稳定生成并驱动 fixer 看真实入口。

## 黑盒 0/10 新根因：build_logical_plan arity 震荡

错误回到 `build_logical_plan() missing 1 required positional argument: 'catalog'`（R5 同款接口错），但这次是**震荡修不好**：

- output 里 `build_logical_plan` 出现 **1577 次**、`missing 1 required` **612 次**。
- 各轮失败数剧烈横跳：**91 → 1 → 23 → 27 → 91 → 1 ...**

多调用点对同一函数用不同 arity：
| 位置 | 调用 | 参数 |
|---|---|---|
| 定义 logical_plan.py | `build_logical_plan(resolved_or_stmt, catalog)` | 2 |
| __main__.py（CLI 入口） | `build_logical_plan(resolved)` | 1 |
| test_executor.py run_sql | `build_logical_plan(resolved, catalog)` | 2 |

fixer 把定义改 1 参 → test 的 2 参调用全挂（91 failed）；改回 2 参 → __main__/smoke 的 1 参调用挂（1 failed）。**单方向修复引入对方回归，6 轮横跳未收敛。**

## 与 R1 震荡的关系

R1 有过 "1↔82 震荡"，已加震荡熔断（_TESTER_ROLE oscillation guard + 跨轮失败数追踪 regression 警告，commit 658cccf）。**但 R7 没拦住**，原因待查：
- 可能震荡发生在 fix() 内部 loop（单 attempt 内多轮），而熔断检测在 run() 的 attempt 之间。
- 或失败数 91↔1 的检测阈值/逻辑未触发。

## R8 方向（候选）

1. **震荡熔断为何没拦住 build_logical_plan 91↔1**——查现有 oscillation guard 触发条件，是否覆盖 fix 内部 loop。
2. **根治多调用点 arity 不一致**：fixer 面对"一个函数被多处用不同签名调用"时，应统一所有调用点到定义签名（而非改定义迁就单个调用点）。可能需要在 fix 注入"以定义为准，修所有调用点"的引导，或静态列出某函数的所有调用点 + 各自 arity。
3. 注意：__main__.py(1参) 和 test(2参) 的矛盾是 coder 生成时就埋下的；smoke 暴露了它，但 fixer 缺"统一调用点"的策略。
