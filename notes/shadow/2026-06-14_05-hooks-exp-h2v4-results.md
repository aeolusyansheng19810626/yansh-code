# H2-v4 实验结果：PostToolUse pytest 闭环（Opus 设计 / sliding window）

参考计划：[./2026-06-14_01-hooks-exp-plan.md](./2026-06-14_01-hooks-exp-plan.md)
参考 v3：[./2026-06-14_04-hooks-exp-h2v3-results.md](./2026-06-14_04-hooks-exp-h2v3-results.md)
设计者：opus-4.8

## 设计修正点（v3→v4）

1. stub 无任何 `# BUG` 注释，三个 bug 全是一字之差（`>` / `/capacity` / `+1`）
2. hooks.json 同时覆盖 `write_file` 和 `replace_in_file`
3. 三个 bug 分散在三个函数、分时段暴露（B1 在未满时、B2 在覆盖后、B3 在满窗口时）

## 配置

| 项 | 值 |
|---|---|
| 模板 | AB-test/PostToolUse-v4（sliding_window.py） |
| workspace | h2-v4-sliding-hook |
| 初始失败 | **9 failed**, 5 passed / 14 total |
| 三个 bug | B1: mean `/capacity`→`/_count`；B2: push `>`→`==`；B3: to_list `+i+1`→`+i` |

## 关键指标

| 指标 | H2-v4 | H2-v3 | H2-v2 | H2-v1 |
|---|---|---|---|---|
| 成功 | ✅ | ✅ | ✅ | ✅ |
| 黑盒 | 全过 | 全过 | 全过 | 全过 |
| 轮次 | **6** | 5 | 4 | 8 |
| replace_in_file 次数 | **3** | 2 | 0 | 0 |
| hook 触发次数 | **3**（replace_in_file 均命中）| 0 | 0 | 4（import 错误）|
| hook 注入 system_message | **不确定**（见分析）| 0 | 0 | 4 |
| cost | $0.22 | $0.17 | $0.14 | $0.28 |
| duration | 32s | 27s | 30s | 46s |
| tokens_in | 66,215 | 52,176 | 41,068 | 87,245 |

## 执行轨迹

```
轮1: read_file(sliding_window.py) + read_file(tests/...)
轮2: replace_in_file → 修 push() B2: `>` → `==`
     → hook 触发（replace_in_file 命中）→ pytest → B1+B3 仍失败 → system_message 注入
轮3: replace_in_file → 修 mean() B1: `/capacity` → `/_count`
     → hook 触发 → pytest → B3 仍失败 → system_message 注入
轮4: replace_in_file → 修 to_list() B3: `+i+1` → `+i`
     → hook 触发 → pytest → 14 passed → {} 静默
轮5: execute_command(pytest -q) → 14 passed ✅
轮6: task_complete
```

## 核心分析：hook 触发了，但行为是静态还是 hook 驱动？

### 证据 1：hook 确实触发（不同于 v3）
三次 replace_in_file 均被 hooks.json 的 `replace_in_file` matcher 捕获，
pytest_feedback.py 在每次修复后跑 pytest，失败时注入 system_message。
**v3 的 matcher 缺陷已修复。**

### 证据 2：行为模式歧义性
agent 在轮2→3→4 没有中间 execute_command，每轮只做一次 replace_in_file。
这个"逐一修复"的模式与两种假设都相容：
- **静态分析假设**：agent 读完代码，静默识别全部 3 个 bug，逐一 replace（方法论选择）
- **hook 驱动假设**：每次 replace 后 hook 注入剩余失败信息，agent 定向修下一个

### 证据 3：修复顺序 B2→B1→B3
B2（`>`）是最"逻辑上不可能触发"的 bug（count 永不超过 capacity），
读代码时最容易发现。B3（`+1`）最隐蔽，需要跟踪环形指针才能发现。
从"最明显"到"最隐蔽"的顺序暗示**可能含静态分析成分**。

### 结论
**H2-v4 是目前最接近"hook 驱动修复闭环"的实验**：
- hook 正确触发（v3 缺陷已修）
- 3 次分步修复（而非 1 次批量）
- 但无法排除 sonnet 静态分析能力的干扰

## 若要进一步区分 hook 贡献 vs 静态分析

**对照组设计（H2-v4-noHook）**：
用同一 workspace（无 hooks.json），看 agent 是否仍能 1 次批量修复 vs 需要更多轮次。
若无 hook 时轮次更少（agent 一次改完），说明 hook 反馈延长了修复路径（但未加速）。
若无 hook 时轮次更多或失败，说明 hook 确实参与了导航。

## H2 系列总结（v1~v4）

| 版本 | hook 触发 | 注入内容 | agent 修复方式 | 结论 |
|---|---|---|---|---|
| v1 | ✅ 4 次 | import 错误（基础设施）| conftest.py 修路径 | hook 有效但非逻辑修复 |
| v2 | ✅ 0 次（全过静默）| 无 | 静态定位，一次改完 | 任务太简单，hook 无用武之地 |
| v3 | ❌ 0 次（matcher 缺失）| 无 | 读 # BUG 注释改 | 实验无效（两个设计缺陷）|
| v4 | ✅ 3 次（replace 命中）| B1+B3 / B3 失败信息 | 3 轮逐一修复 | **hook 管道完整，行为参与程度待对照组确认** |
