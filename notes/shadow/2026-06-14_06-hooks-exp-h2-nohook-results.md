# H2-v4 对照组结果：noHook vs Hook

参考：[./2026-06-14_05-hooks-exp-h2v4-results.md](./2026-06-14_05-hooks-exp-h2v4-results.md)

## 对照组设计

相同 workspace（9 failed / 5 passed，同一 buggy stub），只删除 `.yansh/hooks.json`，其余完全一致。

---

## 指标对比

| 指标 | **有 hook（H2-v4）** | **无 hook（noHook）** |
|---|---|---|
| 成功 | ✅ | ✅ |
| 黑盒通过率 | 全过 | 全过 |
| 总轮次 | **6** | **4** |
| 写操作 | 3× replace_in_file | 1× write_file |
| 每次写操作修复 bug 数 | **1 个**（逐一）| **3+1 个**（一次全改）|
| tokens_in | **66,215** | **43,324** |
| cost | **$0.22** | **$0.16** |
| duration | 32s | 39s |
| hook 触发 | 3 次 | 0 次 |

## 工具调用序列

```
有 hook：read → read → replace_in_file → replace_in_file → replace_in_file → execute → task_complete
无 hook：read → read → write_file → execute → task_complete
```

---

## 核心发现

### 发现1：hook 改变了 agent 的修复策略（最重要）

- **有 hook**：agent 用 `replace_in_file` 逐一修复（3 轮），每次只改一处
- **无 hook**：agent 用 `write_file` 全量重写，一次性修复所有 bug

**hook 的存在让 agent 从"批量重写"切换成了"增量修复"模式。**

推测原因：agent 感知到 hook 会在每次写操作后给反馈，因此选择更保守的小步改法（改一处，等 hook 告诉我还差什么）。这正是 hook 的设计意图——但对静态可见的 bug，这反而增加了轮次和成本。

### 发现2：noHook 多找了一个 max() 问题

noHook agent 自己额外修了 `max()` 的遍历逻辑（改用 `to_list()` 保证只扫有效元素），
共报告"4 处 bug"，而有 hook 的 agent 只精确修了 3 处。

**解释**：有 hook 时 agent 关注"哪些测试在失败"（hook feedback 聚焦），只做让测试通过的最小改动；无 hook 时 agent 做了更全面的代码审查，发现了 hook 组没有去碰的 max() 防御性问题。

→ **hook 让 agent 更"聚焦"但也更"局部"，无 hook 让 agent 更"全量"也更"主动"**。

### 发现3：静态可分析的 bug 不是 hook 的主战场

本实验的三个 bug（`>` vs `==`、`/capacity` vs `/_count`、`+i+1` vs `+i`）虽然在运行时才暴露，但 sonnet 能通过阅读代码 + 测试规格在脑内推演出全部错误，无需 pytest 反馈确认。

**hook 的真正价值区间**：需要 N 次试错才能收敛的场景——
- 第一次修复引入新 bug（修 A 破 B）
- bug 只在特定运行时状态下暴露（外部依赖、随机性、复杂累积状态）
- agent 无法静态枚举所有失败路径

---

## H2 系列最终结论

| 问题 | 答案 |
|---|---|
| hook 管道是否工作？ | ✅ 是（v4 验证：replace_in_file 触发，system_message 注入）|
| hook 是否改变 agent 行为？ | ✅ 是（策略从批量重写→增量修复，更保守）|
| hook 是否加速了修复？ | ❌ 否（有 hook 反而多用 2 轮、多花 $0.06）|
| hook 是否提供了必要信息？ | ❓ 否（sonnet 可静态识别这些 bug，hook feedback 冗余）|
| 何时 hook 才真正有价值？ | 修复会引入新 bug / 只有运行时才暴露的复杂状态 bug |

**一句话**：Hook 功能验证完毕，但要展现"hook 驱动自修复"的价值，需要找到 sonnet 静态分析无法覆盖的任务类型——这是下一轮实验（H2-v5 或新方向）的目标。
