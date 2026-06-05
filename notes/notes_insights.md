# yansh-code 技术笔记知识提炼报告

## 目录

1. [LLM Agent 架构设计原则](#1-llm-agent-架构设计原则)
2. [Prompt 工程核心规律](#2-prompt-工程核心规律)
3. [Token 成本与性能优化](#3-token-成本与性能优化)
4. [错误恢复与流程健壮性](#4-错误恢复与流程健壮性)
5. [安全机制设计](#5-安全机制设计)
6. [工具系统设计](#6-工具系统设计)
7. [跨任务 AB 测试规律](#7-跨任务-ab-测试规律)
8. [工程基础设施](#8-工程基础设施)
9. [总结与决策矩阵](#9-总结与决策矩阵)

---

## 1. LLM Agent 架构设计原则

### 单 Agent 优于 Multi-Agent 协作

最反直觉但被反复验证的结论：**删除独立 Reviewer Agent 后性能反而提升**（272s → 164s）。原因有三：

- LLM 的 Reviewer 本质是同一模型的不同 Prompt，没有真正的视角差异
- Multi-agent 协议切断了 context，LLM 审查质量反而低于自我审查
- JSON 协议脆弱性 + 缺少完成信号导致死循环

**规律**：LLM 推理基于完整 context，人为切断 context 的代价大于分工收益。

### 子 Agent 的正确使用场景

子 Agent 的核心价值是**上下文隔离**而非分工协作：

- 子 Agent 内部 read_file/grep 产生约 800 token，summary 压缩至 80 token，实测 10 倍节省
- 适用场景：独立探索任务、只读分析、可并发的子问题
- 不适用场景：需要完整上下文推理的核心编码、小改动任务（Plan 开销反成 overhead）

子 Agent 并发（ThreadPoolExecutor，max_workers=4）实测加速比 2.4x，受限于 cache miss 和速率。

### Plan/Coder 解耦是双刃剑

Plan 准确时最省 token（44K），Plan 错误时引发 token 爆炸（915K，21 倍差距）。**Plan 的 expected_edits 精准度是整体成本的决定性因素**，估算原则：新文件 1 处、微调 1-3 处、中等重构 5-20 处、大改 30+ 处，建议高估 50%。

### 子 Agent 角色权限隔离

三角色工具集严格映射：

| 角色 | 工具集 | 适用场景 |
|------|--------|----------|
| explorer/auditor | READONLY_TOOL_NAMES | 代码探索、审计 |
| general | 全量工具 | 完整任务执行 |

双重递归防护：thread-local `_IN_SUBAGENT` flag + 物理工具过滤，保证并发安全。

---

## 2. Prompt 工程核心规律

### 具体形状优于抽象原则

一条具体规则的效果是抽象原则的 10 倍。例如：

- 无效："检查全链路依赖"（抽象原则）
- 有效："检查 agent.py 里 `if name == X` 的 dispatch 分支"（具体形状）

直接点名代码库的具体结构（dispatch 表、tools_schema.py 注册、READONLY_TOOL_NAMES），LLM 能立即锚定检查目标。

### 反例比正例更关键

- 单纯文字规则无效，LLM 仍按默认模式执行
- 补充三个 anti-pattern 具体例示后 LLM 立刻遵守（如禁止弱化断言、禁止修改 pre-existing 测试）
- **错误设计**：只说"何时写记忆"；**正确设计**：明确说明"何时不写"

### 任务尺度感知

LLM 默认模式匹配最复杂输出格式（如审计模板）。需要显式教 LLM 按任务复杂度调整输出：简单问题给清单，复杂问题给报告。不区分尺度是市面 audit 工具冗长的根本原因。

### Prompt 改动的 ROI 极高

3 行规则字可让任务速度提升 66%、工具调用减少 50%、答案准确度从错变对。单条 Prompt 规则效果超过增加 5 个工具。

### 英文化 Prompt 的隐患

将中文 Prompt 英文化时，删除了中文中的**隐性启发**（如"查 notes/shadow 找 pre-existing 记录"）。英文化虽在单测通过，但会引入 fix loop 行为退化，必须端到端任务验证，而非仅靠单元测试。

---

## 3. Token 成本与性能优化

### 分层符号索引

将 workspace_symbols 从全量递归改为默认顶层模式，缩减中等项目 **74.5%** 输出。大项目节省 200K+ context 窗口。

设计原则：改默认值而非添加 mode 参数，让 LLM 自动受益，旧调用方显式标 `recursive=True` 体现意图。

### Fix Loop 精确化

将 `pytest` 全套测试改为只跑改动文件对应的 `test_<basename>.py`，消除大量无关测试噪声。实测工具调用减少 63%，token 减少 56%。

### 子 Agent 模型分层

explorer/auditor 子 Agent 切换至 Haiku，general 保持 Sonnet。在架构论证类任务中成本下降 62%，且对输出质量无影响。

### Compact 阈值设计

compact 阈值不是越低越好：

- 必须高于压缩后最小消息体积，否则触发 thrashing 死循环
- 应按**压缩率**而非绝对阈值判定（压缩率 > 60% 才有意义）
- 实测最优范围：60K token（30K 过低会导致 thrashing，80K 过高触发频率不足）

### 动态轮次调度

跨文件重构的框架硬上限（单文件 5 轮）是根本限制。解法：

```
attempts_left = max(coder_rounds_per_file, ceil(expected_edits/edits_per_round) + 2)
```

将 tools.py 的 56 处改动完成率从 7% 提升至 100%。

---

## 4. 错误恢复与流程健壮性

### task_complete 信号全流程贯通

信号设计的核心原则：**让 LLM 自己声明状态，而非流程硬编码**。

信号传播链：`fix()` → `code()` → `run()` → `attempts` 循环。三种场景：

| 场景 | early_exit | success | 外层行为 |
|------|-----------|---------|----------|
| LLM 主动完成 | True | True | 立即终止，标成功 |
| LLM 主动放弃 | True | False | 立即终止，标失败 |
| 沉默退出 | False | - | 继续 attempts |

集成验证：pre-existing 失败场景从 3 次 attempts 优化为 0 次直接成功（33s），矛盾任务场景从虚假通过改为正确失败（2s），时间节省 93%。

### Pre-existing 失败识别

**机械可判定规则**：检查失败 assert 引用的函数/常量名是否在 plan 文件列表中。不在则跳过，无需 LLM 主观判断。

v4 版本引入 baseline 快照机制：fix 前运行 pytest 捕获预存在失败集合，之后判定 `current_failures ⊆ baseline_failures` 即为通过。实测将跨文件重构任务从多次失败到单轮通过（attempts=1）。

### 沉默退出兜底

单次追问策略：第二次沉默才真正退出。避免无限循环同时保障信号可观测性。实测三个场景均由 LLM 主动调用 task_complete，兜底成为真正的安全网而非主路径。

### 范围克制

pre-existing 失败、lint 报错均按"不在 plan 文件内"原则跳过，不尝试修复。关键反例：LLM 会"顺手"弱化断言、删 import、改无关代码，需在 Prompt 中明确列举这三类禁止行为。

---

## 5. 安全机制设计

### 信任边界是攻击的核心入口

三个 P0 级安全漏洞均源于**信任边界跨越时的最薄弱穿透点**：

1. **audit 绕过**：`dispatch_subagent(role='general')` 在 audit 模式下获得写权限。修复：路由层强制降级，audit 模式下 general 角色自动降为 auditor（5 行代码）。

2. **项目配置 RCE**：`.yansh/hooks.json` 无确认即 `shell=True` 执行。供应链攻击 PoC：克隆恶意 repo 后启动即触发。修复：引入 `workspace_trust.py`，默认拒绝项目配置，白名单 + 用户 opt-in。

3. **内存路径穿越**：`recall_memory(name='../../README')` 可读任意 .md 文件。修复：双重验证——`_slugify(name)` + `resolve().is_relative_to()`。

### 安全设计原则

- 物理限制 > Prompt 约定：Plan Mode 写工具屏蔽用白名单过滤而非 Prompt 约束
- 默认安全：非交互模式（CI）自动拒绝未知 workspace，避免 prompt 挂起
- 子 Agent 内部自动跳过 Hook，防止重复触发

---

## 6. 工具系统设计

### 符号级工具的价值

tree-sitter 驱动的 `get_symbol_definition` 相比 Grep+Read 组合：

- yansh 2 次工具调用 vs CC 4 次，得到函数体 + docstring 的完整语义
- 对"文档自动聚合"场景有独特深度价值
- 多线程并发需要全局锁串行化 Parser（_TS_PARSER_LOCK），单锁方案比 thread-local 快 6 倍（40ms vs 240ms）

### MCP 协议：80% 收益的 20% 实现

JSON-RPC over stdio 实现 MCP 最小版，核心价值：**从"一次实现内置工具"到"一次实现协议"**。

关键工程细节：

- EOF 不唤醒 pending 导致 60s 死等，需在 reader_loop finally 中批量 set 错误响应
- 孙进程泄漏：shutdown 必须无条件 kill_tree，不能信任 graceful 退出
- 工具命名空间 `mcp__<server>__<tool>` 避免撞名

### Hook 系统设计

PreToolUse/PostToolUse 是事件驱动的黄金粒度触发点。Windows 子进程超时需 `CREATE_NEW_PROCESS_GROUP` + `taskkill /F /T` 才能杀整个进程树，`subprocess.run(timeout=)` 会留孤儿进程。

Hook 聚合：短路 block 机制，任意 hook block 则整体 block，modify 则链式累积。失败容错优先保可用性，单个 hook 错误不影响整体决策。

### Skills 系统的分层决策

关键字命中走快速路径（零延迟零成本），语义模糊时才调用 LLM（约 $0.0003）。"宁缺勿滥"的 Prompt 设计使 LLM 不会强行误匹配。

---

## 7. 跨任务 AB 测试规律

### 任务类型决定最优工具

| 任务类型 | 最优选择 | 原因 |
|----------|----------|------|
| 代码探索/只读分析 | CC 或 yansh audit mode | Plan 流水线是 overhead |
| 小 bug 修复（有明确失败测试） | CC | 串行路径快，Plan 解耦无收益 |
| 中型功能开发（需要 Plan + 多文件配套改动） | yansh code mode | 领域知识深，语义闭环 |
| 大量机械配套改动（50+ 处） | CC 或提升 yansh 动态轮次 | yansh 硬上限是框架级限制 |
| 架构论证/文档生成 | CC 更经济，yansh 更精确 | 按输出用途选择 |

### AB 测试方法论

- 必须用 git diff 和 pytest 验证客观结果，self-report 字段不可信
- 测试环境污染（workspace 残留、baseline 超时）可导致假结论，每次必须完整清理
- 多家独立 review 覆盖率：单家最多 60-70%，剩余 30-40% 需 cross-check

### Token 消耗规律（5 任务汇总）

- CC：431K（基准，方差最小）
- yansh：2023K（4.7×，方差 21 倍，plan 精准度决定一切）
- yscode：1928K（4.5×，写代码任务强，探索任务退化）

---

## 8. 工程基础设施

### 模块化与循环依赖

- subagent.py 从 agent.py 拆出后，用**惰性导入**（函数体内动态导入）解决循环依赖
- frontmatter.py 统一 skills/memory 的解析逻辑，消除重复不一致
- 拆分模块虽增加总行数但降低耦合度，agent.py 减少 178 行

### 并发安全关键点

- `list.append` 原子不代表组合操作原子，for 迭代期间 append 行为未定
- `collections.deque(maxlen=N)` 替代 `list + pop(0)`：既消除 race 又获得 O(1) 性能
- 任何读-改-写或迭代-修改的组合都需加锁
- CPython GIL 下隐性安全，Python 3.13 free-threaded 后会暴露

### JSON 解析健壮性

ICA 后端 `response_format` 会静默降质（输出退化成 `{}` 而非 400 拒绝）。用硬规则黑名单比动态探测更适合已知后端的坑。JSON retry 包装：校验失败时携带原始 content + 错误信息回源重试一次。

### 测试工程

- 假阳性测试源于过度 Mock 和过弱断言，需用端到端验证
- 使用精确等于（`==`）而非不等（`<=`）锁定实现语义
- 跨平台 CI 矩阵（ubuntu-latest + windows-latest）10 行 YAML 胜过 100 行兼容性论证
- 单测通过 ≠ LLM 行为正确，必须端到端任务验证

---

## 9. 总结与决策矩阵

### 核心洞见（按重要性排序）

**第一优先级：架构决策**

LLM 推理基于完整 context，人为切断 context 的代价大于分工收益。减法（删除 Reviewer）的 ROI 高于加法（增加 Agent）。子 Agent 的价值是上下文隔离，不是功能分工。

**第二优先级：Prompt 工程**

具体形状 > 抽象原则，反例 > 正例，显式 > 隐式。一条好 Prompt 规则的价值超过 5 个新工具。Prompt 改动必须端到端验证，单元测试无法覆盖 LLM 行为。

**第三优先级：成本控制**

Plan 的 expected_edits 精准度是成本的决定性因素。模型分层（explorer/auditor 用 Haiku）是最可靠的成本优化方案，无副作用。分层符号索引可节省 74.5% context。

**第四优先级：健壮性**

信号设计优于硬编码流程。让 LLM 声明状态（task_complete），而非流程时钟强制结束。baseline 快照机制是跨文件重构任务的关键突破点。

**第五优先级：安全**

信任边界是攻击的核心入口。项目配置文件是供应链攻击面，默认拒绝是正确的安全姿态。多家独立 review 才能覆盖 P0 漏洞，单家覆盖率只有 60-70%。

### 可直接复用的工程模式

1. **动态轮次**：`max(base_rounds, ceil(expected_edits/edits_per_round) + 2)`
2. **baseline 快照**：fix 前捕获预存在失败，用子集判定替代精确匹配
3. **信号传播**：`{early_exit, success, summary}` 三字段从内层循环向外传递
4. **分层索引**：默认顶层摘要 + 按需下钻，而非全量递归注入
5. **子 Agent 模型路由**：`explorer/auditor → haiku, general → sonnet`
6. **Prompt 反例节**：专门列举三类禁止行为，比正向规则更有效
