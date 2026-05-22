# yansh-code 路线图

> 这不是一份功能清单，是一份**学习地图**。
>
> 项目目标是通过自己造轮子，理解 Claude Code 等成熟代码 agent 工具的设计哲学：哪里做对了、哪里是工程妥协、为什么市面上多数同类工具效果差。每一项都按"Claude Code 怎么做 / 为什么好 / yansh 现状 / 学到什么"四段组织。
>
> 做的顺序按 **学习价值 × 完成成本** 排序，不严格按"先后"。

---

## 当前位置（2026-05）

| 场景 | 完成度 | 备注 |
|---|---|---|
| 从头写代码 | 80% | plan/code/review/fix 闭环已通；prompt 粗放 |
| 分析现有项目 | 60% | audit mode + workspace_symbols 已加；大型项目会爆 token |
| 修改代码 | 75% | replace_in_file/replace_symbol/apply_patch 全有；缺"看完上下文再决定怎么改"的 reasoning |

已建：18 个工具、4 类角色、tree-sitter 符号索引（带 mtime 缓存）、HIL diff 确认、git 快照与回滚、任务日志、流式输出、ESC 中断、命令安全沙箱、--cwd 多项目支持。

### 进度速览（2026-05-22 晚）

| # | 项目 | 状态 |
|---|---|---|
| P0 #1 | 分层符号索引 | 🟢 完成（top/deep 双模式 + directory_summary + audit 顶层注入） |
| P0 #2 | Prompt 调优 | 🟢 活跃迭代 |
| P0 #3 | 错误恢复闭环 | 🟢 完成（基础设施 + prompt 加固 + 信号全流程贯通） |
| P1 #4 | JSON 解析健壮性 | 🟡 部分（基础校验在，retry 未做） |
| P1 #5 | 全局状态重构 | ⬜ 未着手 |
| P1 #6 | 沙箱模式 opt-in | ⬜ 未着手 |
| P2 #7 | Plan Mode | ⬜ 未着手 |
| P2 #8 | Skills 系统 | ⬜ 未着手 |
| P2 #9 | 子 Agent | ⬜ 未着手 |
| P2 #10 | MCP 协议 | ⬜ 未着手 |
| P2 #11 | Hooks | ⬜ 未着手 |
| P2 #12 | 跨 Session 记忆 | ⬜ 未着手 |

每项的具体进度细节见对应章节顶部的「**进度（YYYY-MM-DD）**」行。维护方式：做了改动就回这里更新该行 + 速览表里的状态。

---

## 优先级 P0：从"玩具"到"真正能用"的分水岭

### 1. 分层符号索引（大型项目支持） 🟢 完成

**进度（2026-05-22）**：三项一起做完（笔记 [_11](./notes/shadow/2026-05-22_11-hierarchical-symbol-index.md)）：
- `workspace_symbols(path, recursive)` 改成 top/deep 双模式：默认 top 只列顶层文件符号 + 子目录摘要（py_files/total_symbols 计数）；`recursive=True` 复原旧全量行为
- 新增 `directory_summary(path)` 工具：返回 file_count / by_extension / key_files / subdirs / files_sample，不递归
- audit() 改顶层注入 + `_AUDITOR_ROLE` prompt 提示主动深挖（`workspace_symbols(path=...)` / `directory_summary(path=...)`）

**集成验证（yansh-code 自身 40 文件）**：top 模式 3,314 chars vs deep 12,975 chars，**缩减 74.5%**。3000+ 文件的大项目按比例估算 deep 模式会直接撑爆 200K context。

**单测**：tests/unit/test_audit.py 19 条全过（旧 4 + 新 15）。

**Claude Code 怎么做**：用户感知不到全局符号索引这种东西。Claude Code 在大项目里靠 `Glob` + `Grep` + 智能的"先看顶层目录，再按需深挖"——本质是**懒加载 + 信息收益最大化**。

**为什么好**：LLM 的上下文是稀缺资源。一次性载入全项目结构（哪怕只是符号清单）很快会超出 200K 窗口。按需查询让"看的范围"和"任务需要"匹配。

**yansh 现状**：`workspace_symbols` 一次扫全项目并把摘要塞进 system prompt。500 文件以下勉强，几千文件就崩。

**学到什么**：
- Context window 管理是 LLM agent 的核心约束，比模型本身更重要
- 分层索引设计：目录摘要 → 文件清单 → 符号清单 → 函数体
- "信息收益"概念：每次工具调用应该让 LLM 离任务更近一步，而不是堆砌噪音

**怎么做（建议）**：
- `workspace_symbols` 改为按目录分层：默认只返回顶层目录摘要 + 每目录文件数；用 `path` 参数下钻
- 引入 `directory_summary(path)` 工具：返回某目录下文件类型分布、关键文件名、子目录列表
- 大项目下 audit() 不再预注入全量摘要，改成只注入顶层结构 + 让 LLM 主动深挖

---

### 2. Prompt 调优（角色 + 工具描述） 🟢 活跃迭代

**进度（2026-05-21）**：6 轮迭代笔记 [_01~_06](./notes/shadow/) + 多个 commit（`f2ac054` `cca5d03` `dbc25e2` `34f22ce`）：4 类任务模板、全链路意识、pre-existing 失败识别、部分写工具的反向警告。**待做**：系统性 few-shot；架构师层的 grep 强制；持续收集误用 case。

**Claude Code 怎么做**：系统 prompt 精心调过——"how to be a great developer"级别的行为约束、错误恢复模式、何时问问题。每个工具 description 也调过：包括什么时候用、怎么避免误用、典型陷阱。

**为什么好**：好的 prompt 等于免费的对齐层。**调一周 prompt 的效果，超过加 5 个工具**。Claude Code 给 Read 工具加一行 "Do NOT re-read a file you just edited" 就能消除一类常见冗余调用。

**yansh 现状**：`_ARCHITECT_ROLE` / `_CODER_ROLE` / `_REVIEWER_ROLE` / `_TESTER_ROLE` / `_AUDITOR_ROLE` 都是几行字粗放定义。工具 description 多是中性描述功能，没有"用法守则"。

**学到什么**：
- Prompt engineering 的边际收益曲线：80 分到 90 分远比 0 分到 80 分难
- Few-shot 比指令更有效：给一个对的例子 vs 写一段抽象规则
- "副作用警告"型描述比"功能介绍"型描述价值高 10 倍

**怎么做（建议）**：
- 给每个角色加 1-2 个完整 few-shot example（用历史失败 case 反推）
- 给写工具加"何时不要用"的反向提示（例：write_file 描述加"如果文件已存在且只改一处，应优先用 replace_in_file"）
- 收集 LLM 误用工具的实例做 prompt 修正——这个迭代可以持续半年

---

### 3. 错误恢复闭环 🟢 完成

**进度（2026-05-22）**：三波迭代完整落地——

- **第一波**（commit `7d1b399`，笔记 [_07](./notes/shadow/2026-05-21_07-p0-3-recovery-loop.md)）：基础设施层。`task_complete(success, summary)` 工具、fix/audit 软上限（6→12/8→16）、token 预算警告（60K/120K）、`error_kind` 全量铺到 21 个工具、fix() 加 interrupt 检查。
- **第二波**（commit `5d22465`，笔记 [_08](./notes/shadow/2026-05-21_08-prompt-and-loop-hardening.md)）：prompt 加固。`_TESTER_ROLE` / `_AUDITOR_ROLE` 把【收尾要求】移到顶部 + few-shot 示例 + 删反向暗示；fix/audit 加沉默退出兜底（追问一次）；`_CODER_ROLE` 模板 4 加 linter 归属规则（ruff/flake8 报错若不在 plan 文件按 pre-existing 处理）。实操 [_09](./notes/shadow/2026-05-21_09-p0-3-live-validation.md) 跑三场景验证，发现下一波漏洞。
- **第三波**（commit `8be9e4f`，笔记 [_10](./notes/shadow/2026-05-21_10-task-complete-signal-propagation.md)）：信号全流程贯通。`fix()` 返回 `{early_exit, success, summary}`；`code()` 返回 Optional[dict]，inner loop 识别 sentinel；`run()` 三处接信号（Coder 阶段后 / linter fix 后 / attempts 循环 fix 后）；`report()` 加 `task_complete_signal` 字段。集成验证场景 A：attempts 3→0，fail→success；场景 B：pass(错)→fail(对)，31s→2s。

**关键收益**：LLM 调 `task_complete(success=true)` 时外层立即标 success 退出（不再无谓 retry）；调 `task_complete(success=false)` 时外层立即跳过测试标 fail。Sonnet 4.6 在三场景里都正确触发协议——prompt 加固已把"必须显式收尾"内化。

**Claude Code 怎么做**：工具失败 → LLM 看到错误 → 自然换路（换工具、换参数、问用户、放弃并报告原因）。整个 agent loop 没有硬性的"6 轮上限"——它通过其他机制（context 占用、用户中断、明确的"我做不了"声明）来收敛。

**为什么好**：复杂任务往往要 20+ 工具调用。死板的 N 轮上限会让任务**总在最后一步前被砍**。

**yansh 现状**：`fix()` 死循环上限 6 轮（agent.py:1247）；`audit()` 上限 8 轮；`reason` 字段二选一（test_failure / review_rejection）。错误带回 LLM 后，LLM 没明确的"放弃信号"通道。

**学到什么**：
- Agent state machine 不是 finite state，是"软约束 + 自然退出"
- 退避策略：transient 重试、permanent 升级、模糊状态报告并问人
- 收敛信号设计：让 LLM 主动声明"我做完了"或"我做不了"比定时砍掉更优雅

**怎么做（建议）**：
- fix/audit 把硬上限改成"软目标 + token 预算"：累计 token > 阈值才硬退
- 引入 `task_complete(success: bool, summary: str)` 工具，让 LLM 主动声明结束（claude code 没有这个，但 yansh 没有 stop_reason 的细分，需要补）
- 错误反馈格式标准化：tool error 包含 `error_kind`（transient/invalid_args/not_found/permission/...），LLM 才知道是该重试还是换路

---

## 优先级 P1：长期工程健康

### 4. JSON 解析健壮性 🟡 部分

**进度（2026-05-21）**：Pydantic 校验 + 失败 log 已加（commit `fa9f991`）。**待做**：调研 ICA 网关 `response_format`、解析失败自动 retry 1 次。

**Claude Code 怎么做**：tool calling 用的是 OpenAI 兼容的 function calling，schema 由后端校验。模型偶尔生成不合法 JSON 会被 SDK 层 retry。结构化输出走 strict json schema mode（OpenAI/Anthropic 都支持）。

**为什么好**：不依赖正则解析 LLM 输出。schema-first 设计让"模型该输出什么"在协议层就固定。

**yansh 现状**：上一轮已加 Pydantic 校验 + 失败 log。但 `llm_client.py` 对 Claude/ICA 跳过 `response_format`，仍依赖 `_extract_json` 的正则提取。

**学到什么**：
- 不要相信 LLM 的格式遵守能力——要在协议层校验
- "正则提取 markdown 包裹的 JSON" 是个普遍 hack，每个 agent 项目都写过
- 后端能力差异是真实痛点（Claude vs OpenAI 的 structured output 协议不同）

**剩余工作**：
- 调研 ICA 网关是否支持 `response_format`，能传就传
- 失败时**自动 retry 1 次**带"返回必须是合法 JSON，前次输出：{raw}"的修正提示

---

### 5. 全局状态重构 ⬜ 未着手

**Claude Code 怎么做**：每个 session 独立进程，session 内状态局部封装。多个 Claude Code 实例可以并行跑而互不干扰。

**为什么好**：测试性 + 可并发性。yansh 的 `_BATCH_MODE / _PROJECT_TYPE / _CURRENT_SNAPSHOT / _AST_CACHE` 都是模块级全局，单元测试要 `reload(tools)` 重置。

**yansh 现状**：CLI 单进程单任务下能跑，但测试体感差，未来想做"多 workspace 并行 audit" 会撞墙。

**学到什么**：
- "全局状态便利" 是技术债的隐形利息——开发期省 1 小时，测试期还 10 小时
- Python 模块级状态 vs class 实例 vs context manager：什么场景用哪个
- 渐进式重构 vs 大爆炸重写：一般选前者

**怎么做（建议）**：
- 引入 `Session` class 持有这些状态；模块级变量保留为兼容代理
- 测试改用 `with Session(tmp_path) as sess:` 模式

---

### 6. 沙箱模式（opt-in） ⬜ 未着手

**进度（2026-05-21）**：已有命令安全沙箱（黑名单 + 未识别确认）。**待做**：`--sandbox docker` opt-in。

**Claude Code 怎么做**：默认运行在用户机器上，但 IDE 集成时有 read-only file mode、explicit auth 提示。本身不强制 docker 沙箱。

**为什么好**：把"是否需要隔离"的决策权留给用户。不是所有场景都需要 docker overhead。

**yansh 现状**：执行在宿主机 shell。已有命令安全沙箱（黑名单 + 未识别确认），但没有进程级隔离。

**学到什么**：
- 安全是连续谱不是开关：黑名单 → 确认机制 → 工作目录限制 → 进程隔离 → 容器隔离 → VM 隔离
- 易用性 vs 安全的取舍：每加一层隔离都损失一些易用性
- "默认安全"和"opt-in 安全"的产品哲学差异

**怎么做（建议）**：
- `--sandbox docker` 选项：执行 `execute_command` 时通过 docker run 跑，挂载 workspace 为 ro 卷
- 暂不动其他工具——只 execute_command 是"会运行任意代码"的工具

---

## 优先级 P2：对标 Claude Code 关键特性（选学）

这些是"做了能学到核心设计、不做也不影响基本功能"的功能。每项可独立做。

### 7. Plan Mode（真正的 plan，不是输出 JSON） ⬜ 未着手

**进度（2026-05-21）**：工具白名单机制已有（audit mode 复用）。**待做**："审核 → 批准 → 实施"状态机；plan 阶段禁用写工具。

**Claude Code 怎么做**：进入 plan mode 后**禁用所有写工具**，LLM 只读探索、产出 markdown 计划，用户批准后才退出 plan mode 开始实施。

**为什么好**：把"理解需求"和"动手做"分离。用户能在 LLM 烧 token 之前看到方案。

**yansh 现状**：`--mode plan` 只是"输出结构化 JSON 计划，跳过实施"。没有"用户审核 → 批准 → 实施"的状态机。

**学到什么**：
- Plan mode 是个**用户体验创新**而非技术创新——它本质是"先 commit 设计，再写代码"的工程文化的产品化
- 工具白名单已有（audit mode 用了），plan mode 是同一机制的另一应用
- "退出审批"的状态机设计

---

### 8. Skills 系统 ⬜ 未着手

**Claude Code 怎么做**：Skills 是用户自定义的 prompt 包，按用户输入或上下文自动触发。本质是**可复用的角色 + 工作流模板**。

**为什么好**：不需要让 LLM 从零思考"怎么做 X"，先加载领域专家的工作流。

**yansh 现状**：没有。`.agent_rules` 是项目级常量规则，不是按需触发的 skill。

**学到什么**：
- Prompt as a Service：把"经验"封装成可分发的 unit
- 触发机制设计：关键字 / 上下文 / 显式调用三种
- 信任边界：第三方 skill 能多大程度修改 agent 行为

**怎么做（最小版）**：
- `skills/` 目录每个 `.md` 是一个 skill，frontmatter 声明 `triggers: [...]`
- 用户输入匹配触发词 → 该 skill 内容加到 system prompt

---

### 9. 子 Agent / 任务分派 ⬜ 未着手

**Claude Code 怎么做**：`Task` 工具能派生子 agent，子 agent 有自己的 context window，结果作为单个消息回到主 agent。

**为什么好**：**上下文隔离**。让子 agent 烧 100K token 探索代码库，主 agent 只看到 2K 总结。整个会话能跑得久得多。

**yansh 现状**：没有。所有工作在单个 LLM context 内串行。

**学到什么**：
- Context isolation 是 long-running agent 的核心模式
- 主 agent / 子 agent 的接口设计：传什么、收什么
- 子 agent 失败的隔离

---

### 10. MCP 协议支持 ⬜ 未着手

**Claude Code 怎么做**：原生支持 MCP（Model Context Protocol），用户能接入第三方工具服务器（Linear、Sentry、GitHub MCP 等）。

**为什么好**：不需要在 yansh 内置每个集成。生态扩展点。

**yansh 现状**：没有。要加新工具必须改 `tools.py + tools_schema.py`。

**学到什么**：
- 标准协议的价值（一次实现，N 个供应商可接入）
- JSON-RPC over stdio 的简洁性
- 客户端 / 服务器 / 用户三方的信任模型

---

### 11. Hooks ⬜ 未着手

**Claude Code 怎么做**：用户能在 settings.json 配置事件钩子（PreToolUse / PostToolUse / Stop / UserPromptSubmit），命令行脚本响应事件。

**为什么好**：开放扩展点。"每次写文件后自动 prettier"、"每次 commit 前 lint"，用户自己编排。

**yansh 现状**：没有。所有行为硬编码。

**学到什么**：
- 事件驱动架构如何嵌入 agent
- 用户自定义 vs agent 内置：边界在哪
- Hook 失败的容错设计

---

### 12. 跨 Session 持久记忆 ⬜ 未着手

**进度（2026-05-21）**：有 `.agent_rules`（项目静态规则）+ task_log（任务历史）。**待做**：LLM 主动写的 memory；按相关性调取。

**Claude Code 怎么做**：内置 memory 系统（你正在体验它），按类型分类（user/feedback/project/reference），对话间可调取。

**为什么好**：避免每次 session 重头解释"我是谁、项目背景是什么"。

**yansh 现状**：有 `.agent_rules`（项目级静态规则）和 task_log（任务历史），但没有可由 LLM 主动写入的 memory。

**学到什么**：
- 短期记忆（session）vs 长期记忆（cross-session）vs 项目记忆（user_rules）的三层设计
- 写入触发：用户显式 vs LLM 自主决定写
- 调取触发：每次都加载 vs 按相关性查

---

## 怎么用这份文档

1. **不要追求都做完**——做完意味着重新发明 Claude Code，没意义
2. **按 P0 → P1 → P2 顺序**，但每个 P 内部可以挑感兴趣的先做
3. **每做一项前**，先打开 Claude Code 用相同的功能，观察它的实际行为细节，再回来对比 yansh 的实现
4. **每做完一项**，写 1 段笔记（commit message 里就行）：你以为 Claude Code 是这么做的、实际是不是、差距在哪。这才是这个项目的真正产出
5. P2 里 #9（子 agent）和 #10（MCP）是 LLM agent 设计的两个**关键架构概念**，做完这两项你对 agent 工具的理解会上一个台阶

## 不在路线图上的事

明确**不做**的：

- 重写成 Rust/Go 提性能：用户感知差异有限，工程量极大
- 自研模型 / fine-tune：偏离学习目标
- 商业化：偏离学习目标
- 浏览器/IDE 插件：CLI 已经能满足学习目的
- 完整对标 Claude Code 全部功能（subagents 调度、worktree 自动化、permission UI、session resume...）：边际学习价值递减
