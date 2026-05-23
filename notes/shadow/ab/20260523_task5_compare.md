# AB Test #5：跨文件重构 — yansh code mode vs CC 子 agent

**题目**：给 `tools.py:_err(kind, msg, **extra)` 加必传位置参数 `tool: str`，签名改成 `_err(kind, msg, tool, **extra)`，返回 dict 加 `"tool"` 字段（错误溯源）。全 repo ~64 个调用点（tools.py 56 / agent.py 4 / subagent.py 1 / test_tools.py 3）适配新签名。加 1 条新单测验证 `result["tool"]` 字段。

**预期**（task #3 笔记里写的）：yansh 的 plan→code→fix 流水线在多文件配套改动场景应反弹，对照 CC 的"读+改+验证"循环。

**实际**：**预期完全反过来**——yansh 在硬上限切碎下半残翻车，CC 完胜。

## 数据对比

| 维度 | yansh (code mode) | Claude Code 子 agent (general-purpose) |
|---|---|---|
| 用时 | **498.8s** (8.3 min) | **294.1s** (4.9 min) |
| 工具调用 | **130** | **54** |
| Token (in+out) | **1,856K** (sonnet 1062K + haiku 794K) | **184K** |
| 估算成本（粗算） | sonnet $3/M + haiku $1/M ≈ **$4.0** | sonnet $3/M ≈ **$0.55** |
| attempts | **3 (max 用尽)** | 1 |
| 测试结果 | **fail** ❌ | **pass** ✓ |
| 完成度 | **~7%**（tools.py 56 处只改了 4 处） | **100%** + 主动加辅助函数透传 |
| dispatch_subagent | 7（多个被"未声明 task_complete 截断"） | 0 |
| 5 轮工具上限警告 | 3 文件耗尽（tools.py / agent.py / test_tools.py） | n/a |
| fix loop 12 轮上限 | 强制退出 | n/a |

**CC 在所有维度完胜：用时 1.7×、tokens 10×、成本 7.3×、且 yansh 没修完测试不过**。

## yansh 翻车定位

stderr 关键事件序列：

1. **plan 阶段** ✓：列出 4 个文件 + test_command（合理）
2. **Coder 阶段**：3 个文件全部"已用尽 5 轮工具调用上限"——LLM 在每个文件的 5 轮里没能一次性把 56 / 4 / 3 个调用点全改完
3. **dispatch_subagent 派了 7 个**：但 stderr 显示多次"子 agent 未声明 task_complete，已截断"——子 agent 也半残
4. **fix loop**：attempt 1 跑测试 21+ TypeError 失败 → fix attempt 2 → "fix 已达 12 轮上限，强制退出"
5. **attempt 3 max 用尽 → exit**

**`_err` 签名改对了，但 56 个调用点只适配了前 4 处**（line 202/204/207/215）。其余 52 处继续 `_err("kind", "msg")` 缺位置参数，运行时全部 TypeError。

工具分布：read_file 64 + get_symbol_definition 16 + search_in_files 14 + execute_command 13 + replace_in_file **7** + dispatch_subagent 7 + task_complete 5 + list_symbols 4 = 130。

**只 7 次 replace_in_file** 改了 4 个文件——平均每文件不到 2 次 edit，但每个文件需要的 edit 数量是 56/4/1/3 = 64+。**LLM 没合并多处编辑也没用 replace_all**，单次 edit 1-3 处太低效。

## CC 路径：54 工具一次跑通

CC 报告：Read 5 + Edit 32 + Bash 2 + Grep 2 = ~41（其他几个工具用法）。

CC **主动设计深化**：除了 `_err` 加 tool 参数外，发现 `_validate_path` / `_check_dangerous` / `_load_ts_parser` / `_parse_symbols_cached` 是被多个工具复用的内部辅助，给它们加了 `tool: str = "..."` 默认参数透传——错误溯源能精确到 caller 工具而非辅助函数。这是 yansh 没想到的设计。

CC 不受 5 轮上限切割，单线程串行 Edit 32 次扫完所有调用点。

## 翻车根因（yansh 框架结构性）

| 问题 | 触发场景 | task #4 没暴露原因 | task #5 暴露 |
|---|---|---|---|
| **每文件 5 轮工具上限** (`agent.py:1800`) | 单文件需要密集修改（>5 处 edit） | task #4 单文件只 1 处 edit 够 | tools.py 56 处 edit / 5 轮 = 不可能完成 |
| **fix loop 12 轮上限** | 测试驱动验证 + 多次反复修 | task #4 1 attempt 就过 | 21+ TypeError → fix 反复跑全套 → 12 轮耗尽 |
| **3 attempts max** | 任何不能在 3 个完整 plan→code→fix 周期内完成的任务 | task #4 1 attempt | 大改动 + fix 失控 → attempts 用尽 |
| **dispatch_subagent 在大任务下子 agent 也半残** | 子 agent max_steps=8 / 16 hard cap，对大文件改动也不够 | task #4 子 agent 跑探查（小活）够 | 子 agent 派去改 56 处，max_steps 截断 |

**yansh 的硬上限设计假设是"小到中型修改"**——每文件 5 轮、fix 12 轮、3 attempts。这套限制在 task #4 没暴露问题（小改动），但 task #5 直接撞墙。

## 共同盲点：没有

CC 这次没有盲点，反而做了 yansh 没想到的辅助函数透传（设计更细致）。

yansh 的盲点是**框架级**而非 LLM 级——LLM 行为没问题（第一次 edit 都对），是上限把它的努力切碎了。

## 设计反思（yansh 侧）

`agent.py:1800` 的 5 轮上限：
```python
if attempts_left <= 0 and response_message.tool_calls:
    warn = f"[警告] {filename} 已用尽 5 轮工具调用上限"
```

这是写代码场景设计的——单文件改完就该 5 轮内收尾，避免 LLM 死循环。但**密集多点修改**是合理需求（重构、批量适配），切割是反优化。

可能改法（**未做**，记入 backlog）：
- (a) 5 轮上限改成 token 上限（按文件大小动态调整）
- (b) 上限耗尽时不直接 break，而是把"剩余工作"喂回去再给 5 轮
- (c) plan 阶段 LLM 输出"预计 edit 数" → 调度按数量决定上限
- (d) 用 `replace_all` 类工具一次改多处（schema 已支持但 LLM 不主动用）

## task #5 数据 vs 此前 4 次

| Task | 类型 | yansh:CC token 比 | yansh 完成度 | 这次结论 |
|---|---|---|---|---|
| #1 探索 | 信息检索 | 1.5× | 100% | 半 CC 略胜 |
| #2 写代码+加测 | 中等单功能 | 25× | yansh 100%（更深） | yansh 闭环深 / CC 便宜 |
| #3 架构论证 | 只读分析 | 4× | 100%（输出深） | yansh 行号准 / CC 抓 trap |
| #4 bug 修复 | 局部小改 | 4× | 100% (相同修法) | CC 4× 便宜，质量打平 |
| **#5 跨文件重构** | **大量配套改动** | **10×** | **yansh ~7% / CC 100%** | **CC 完胜 + yansh 翻车** |

**修正预期**：yansh 在"完整功能落地（小到中型）"反而是优势区；**"大量机械配套改动"反而是 yansh 死亡区**——硬上限切割导致根本完成不了。

## 总结：什么场景选什么（修订后）

| 任务类型 | 推荐 | 倍率 |
|---|---|---|
| 探索 / 信息检索（task #1）| **CC** | yansh ≈ 1.5× |
| 写代码 + 加测（task #2）| **CC**（25× 便宜） | yansh ≈ 25× |
| 完整功能落地（中型，含 schema/文档/清理） | **yansh** | 多花但语义闭环 |
| 架构论证 / 纯只读分析（task #3） | **看深度需求** | yansh ≈ 4× |
| bug 修复（含失败测试，task #4） | **CC** | yansh ≈ 4× |
| **大量机械配套改动 / 跨文件重构（task #5）** | **CC**（yansh 跑不完） | yansh ❌ |
| 不熟悉的代码库 | **yansh**（plan/audit 强制只读，更安全） | — |

**关键修正**：之前预期的"yansh 跨文件优势"被 task #5 证伪——yansh 的硬上限设计在大改动下不支持。yansh 在**"中型范围 + 需要 plan + 多阶段语义闭环"**的任务上有优势，**不是**"改的文件数多就赢"。

## 数据文件

- `20260523_task5_yansh.json` — yansh batch JSON 输出（task_log 格式）
- `20260523_task5_yansh_stderr.log` — yansh stderr 完整跑测过程（含 5 轮警告 / 子 agent 截断 / 12 轮 fix 退出）
- `20260523_task5_cc_diff.patch` — **CC 完整修法的 unified diff**（674 行），可 `git apply` 复现 CC 修法
- CC subagent transcript：在父对话里，未单独保存

## 下一步

- 综合 5 次 AB 写一篇 README（决策矩阵 + 关键 lesson）
- yansh 框架级 backlog：5 轮上限可调 / fix 12 轮上限可调 / 子 agent max_steps 可调（针对重构场景）
