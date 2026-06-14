# H2-v2 实验结果：PostToolUse pytest 闭环（buggy stub）

参考计划：[./2026-06-14_01-hooks-exp-plan.md](./2026-06-14_01-hooks-exp-plan.md)
参考 v1：[./2026-06-14_02-hooks-exp-h2-results.md](./2026-06-14_02-hooks-exp-h2-results.md)

## 配置

| 项 | 值 |
|---|---|
| 模板 | AB-test/PostToolUse-v2 |
| workspace | h2-v2-pytest-hook |
| 任务 | 修复已知 bug（stats.py stub，3 个预置 bug）|
| 模型 | claude-sonnet-4-6 |
| mode | solo + gate |
| hook | PostToolUse → write_file → pytest_feedback.py |
| v1 改进 | 预置 conftest.py（修复 sys.path 问题）|

## 预置 bug（3 个）

| bug | 函数 | 失败测试 |
|---|---|---|
| `data.sort()` 修改原列表 | `median` | `TestMedian::test_does_not_modify_input` |
| 截断前未排序 | `trimmed_mean` | `TestTrimmedMean::test_trim_unsorted_input` |
| 未检查 `sum(weights)==0` | `weighted_mean` | `TestWeightedMean::test_all_zero_weights_raises` |

初始 pytest：`3 failed, 28 passed`

## 关键指标

| 指标 | H2-v2 | H2-v1（对比）|
|---|---|---|
| 最终成功 | ✅ True | ✅ True |
| gate test_result | pass | pass |
| 黑盒通过率 | 27/27 | 27/27 |
| 总轮次 | **4** | 8 |
| write_file 次数 | **1** | 2 |
| hook 注入次数 | **0**（触发后全过，静默）| 4（import 错误）|
| cost | **$0.14** | $0.28 |
| duration | **30s** | 46s |
| tokens_in | **41,068** | 87,245 |

## 执行轨迹

```
轮1: read_file(stats.py) + read_file(tests/test_stats.py)   ← 读懂 bug + 测试
轮2: write_file(stats.py)  ← 一次性修复全部 3 个 bug
     → hook 触发 → pytest → 31 passed → {} 静默（无注入）
轮3: execute_command(pytest tests/ -q)  ← agent 手动确认
     → 31 passed ✅
轮4: task_complete(success=True)
```

agent summary：
> 修复了 3 个 bug：1) median 用 sorted() 替换 .sort()；2) trimmed_mean 截断前先排序；3) weighted_mean 补充 sum(weights)==0 的 ValueError 检查。

## 核心发现

### Hook 静默 = 正确行为 ✅

hook 在 write_file 后确实触发，pytest 全过 → 返回 `{}`（无注入）。
这是设计预期：成功时不产生噪音。

### Sonnet 静态分析能力强，无需 hook 驱动修复

agent 通过「读代码 + 读测试」在脑内完成了所有 3 个 bug 的定位，一次写入全部修复。
hook 的 feedback 没有机会参与修复循环，因为修复本身从未失败。

### "hook 驱动自修复"的触发条件尚未找到

H2-v1 中 hook 注入了 import 错误（基础设施问题，非逻辑）。
H2-v2 中 bug 可通过静态分析发现，hook 全程静默。

真正能触发 hook 驱动修复的 bug 特征应是：
- **运行时才暴露**（非代码阅读可发现）：复杂状态依赖、并发副作用、边缘输入组合
- **测试失败信息本身提供定位线索**（而非代码本身）

例：实现一个带缓存的函数，缓存 key 设计有 bug（相同参数不同顺序命中不同 key）——只有 pytest 报告具体失败值时才能定位。

## H2 系列结论

| 场景 | hook 作用 |
|---|---|
| CRUD/简单逻辑（H2-v1/v2） | 基础设施 debug（v1）或静默（v2）；不参与逻辑修复 |
| 复杂运行时 bug（待测） | 假设 hook feedback 会成为修复关键线索 |

**Hook 功能验证完毕；但"逻辑层自修复"假设需要更难的任务才能触发。**
建议后续实验用带运行时状态 bug 的任务（如缓存/累积状态错误）作为场景。
