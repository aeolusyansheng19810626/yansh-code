# H2 实验结果：PostToolUse pytest 闭环

参考计划：[./2026-06-14_01-hooks-exp-plan.md](./2026-06-14_01-hooks-exp-plan.md)

## 配置

| 项 | 值 |
|---|---|
| 模板 | AB-test/PostToolUse |
| workspace | h2-pytest-hook |
| 任务 | 实现 stats.py（mean/median/trimmed_mean/weighted_mean）|
| 模型 | claude-sonnet-4-6 |
| mode | solo + gate |
| hook | PostToolUse → write_file → pytest_feedback.py |

## 关键指标

| 指标 | 值 |
|---|---|
| 最终成功 | ✅ True |
| gate test_result | pass |
| 黑盒通过率 | 27/27 用例全过 |
| 总轮次 | 8 |
| write_file 次数 | 2（stats.py + conftest.py）|
| hook 触发/注入次数 | ~4 次（含 3 次 ModuleNotFoundError + 1 次全过）|
| cost | $0.28 |
| duration | 46s |
| tokens_in | 87,245 |
| 里程碑（smoke/预算/compact） | 无 |

## 执行轨迹

```
轮1: read_file(tests/test_stats.py)
轮2: write_file(stats.py)
     → hook 触发 → pytest → ModuleNotFoundError: No module named 'stats'
     → system_message 注入给 agent
轮3-4: execute_command(pytest --import-mode=importlib / 多种参数试错)
     → hook 触发（同样 import 失败）
轮5-6: list_files() / 诊断
轮7: write_file(conftest.py)  ← 关键修复：sys.path 注入
     → hook 触发 → pytest → 31 passed ✅ → 空输出（不注入）
轮8: task_complete(success=True)
```

## 核心发现

### Hook 功能本身：验证通过 ✅

- hook 正确触发（write_file 后）
- pytest 输出作为 system_message 注入 LLM
- agent 确实读取并响应了 hook 的错误信息

### 但"自修复"是基础设施层，不是逻辑层

- 计划假设：hook 会帮助 agent 发现函数实现的边缘案例 bug 并修复
- 实际发生：stats.py 第一次写入**逻辑可能已正确**，但 pytest 因 `sys.path` 问题无法导入 `stats` 模块
- agent 最终的修复是增加 `conftest.py` 解决模块导入路径，而非修改函数逻辑

### 实验设计缺陷

任务给 agent 一张白纸（无初始代码），sonnet 对 stats.py 这类 CRUD 类任务一次写对率很高，
基本不需要 hook 来修正逻辑。H2 真正想测的场景（"agent 写了错误实现 → hook 发现 → agent 修复"）
**没有被触发**。

## 修正实验设计（H2-v2 建议）

预置一个**有已知 bug 的 stats.py stub**：
- `trimmed_mean` 不排序直接截（大概率 `trimmed_mean([5,1,4,2,3], 0.2)` 返回错误值）
- `median` 修改了原列表
- `weighted_mean` 没有检查 `sum(weights) == 0`

agent 只需修复 bug，不需要从头实现。这样第一次 write_file 后 pytest 一定失败，
hook 注入具体失败用例，然后观察 agent 是否靠 hook 反馈逐步定向修复。

## 对比 baseline

无 hook 对照组（solo 同任务）未跑，但 46s / 8轮 / $0.28 是合理基准。
hook 有效延长了调试轮次（纯写代码预计 3-4 轮），但换来了不依赖 gate 的内嵌验证闭环。

## 结论

**H2-v1 结论：hook 管道工作正常，但任务设计没能激发"逻辑层自修复"行为。**
需要 H2-v2（预置 buggy stub）才能真正测试假设。
