# Solo Mode 代码 Review 提示词（给第三方模型）

> 用途：把下面「提示词正文」整段复制给第三方模型（GPT/Gemini/独立 Claude 等），连同所列文件/函数的源码一起提供，做一次聚焦 review。
> 范围刻意限定在 solo mode 引入以来的增量代码，不审 task1-50 沉淀的稳定路径（plan/code/audit/fix/quality-gate，已经几十个 AB task 压过）。

---

## 提示词正文（复制以下全部）

你是一名资深 Python 工程师，受邀对一个 AI coding agent 框架的**增量代码**做一次严格 review。请只就我提供的代码片段判断，不要臆测未给出的实现。

### 背景

这是一个命令行 AI coding agent（类 Claude Code），接受自然语言需求，自主规划→读写文件→执行命令→自测，端到端完成多文件编程任务。底层 LLM 经由 IBM ICA 网关调用，**该网关不透传 prompt cache**，因此长对话的 input token 是 O(N²) 全额重发——token 成本是一等约束。

本次新增的 **solo mode** 是一个「单一连续 context」的端到端 agent：不像旧架构那样逐文件重建 context，而是持有一条连续的 messages 主循环（规划→write/run/fix→自测），跑完后还有一个外部 test gate 把失败 stderr 回灌进**同一条 messages** 继续驱动修复。配套还加了两块基础设施：

1. **compact 压缩**：对话 token 超阈值时，把旧消息对摘要化、只保留最近若干轮，控制 token 膨胀。
2. **框架自动维护的环境知识文件** `.yansh/agent_state.md`：每次执行 shell 命令后，框架按 exit code 把 `python`/`pytest` 类命令分类写入白名单（exit=0）/黑名单（exit≠0），跨 run 复用，并在任务启动和每次 compact 时注入进 system prompt——目的是让 agent 不必反复试探环境命令。

### 待审范围（请向我索取或我已附上以下源码）

| 文件 | 符号 | 行号 | 关注点 |
|---|---|---|---|
| `agent.py` | `solo()` | 4065 | 主入口：system prompt 组装、工具集裁剪、主 loop、test gate 回灌、收尾返回 |
| `agent.py` | `_solo_drive()` | 3957 | 抽出的主循环体：compact 触发、token 软提醒、工具分发、task_complete sentinel 扫描、agent 级 no_progress 熔断、沉默兜底 |
| `agent.py` | `_SOLO_ROLE` | 2229 | solo agent 的 system role 提示词常量 |
| `agent.py` | solo 4 常量 | 137-140 | soft_limit=120 / token_budget=600K / no_progress_cap=6 / gate_max_rounds=8，判断取值是否合理 |
| `agent.py` | `_maybe_compact_messages()` | 1446 | compact 触发条件 + 状态文件注入 |
| `agent.py` | `_compact_messages()` | 1358 | 实际摘要压缩逻辑（keep_recent_pairs） |
| `agent.py` | 状态文件注入 | 1420 / 4101 | `_MAX_STATE=4000` 截断 |
| `tools.py` | `_update_agent_state()` | 32 | **重点**：正则匹配命令 + 文件锁 + 跨 run 写盘 + 失败转成功的重分类逻辑 |
| `tools.py` | `_STATE_CMD_RE` / `_STATE_FILE_LOCK` | 28-29 | 命令白名单正则、并发锁 |
| `tools.py` | `execute_command()` | 349 | 命令执行 + 头尾截断 + 在 456 行调用 `_update_agent_state` |

### Review 维度（按优先级，请分维度组织输出）

**P0 — 正确性**
- `_update_agent_state` 的「先失败后成功」重分类：同一命令先进黑名单、后 exit=0，能否正确从黑名单移除并加入白名单？反向（先成功后失败）呢？精确行匹配会不会漏删或误删？
- compact 后开场规划/关键上下文是否可能被摘要丢失，导致 agent 后期跑偏？keep_recent_pairs 取值是否够。
- test gate 回灌循环的终止条件：是否存在不收敛（反复回灌同一错误烧轮次/烧 token）的路径？
- no_progress 熔断：判定「本轮有无写编辑」的依据是否可靠？会不会误杀正常的深度探索（agent 连续读文件/跑命令但暂未写）？
- sentinel（task_complete）扫描、沉默退出兜底是否有边界 bug。

**P0 — 安全 / 并发**
- `_update_agent_state` 写盘路径来自 workspace，是否存在路径注入/越权写出 workspace 的可能？
- `_STATE_FILE_LOCK` 是 `threading.Lock`：若框架有多进程/多 worktree 并行（本项目有并行编排），线程锁能否防住跨进程的文件竞争？是否需要文件锁？
- `_STATE_CMD_RE` 正则 `^\s*(py\b|python[0-9.]*|pytest)`：有无 ReDoS、误匹配（如 `pythonic_tool`）、或被恶意命令绕过分类的问题？
- 状态文件内容会被注入回 system prompt——是否存在「写盘内容污染后续 prompt」的注入面（如命令里带换行/markdown 注入）？现有的多行/超长过滤是否足够？

**P1 — 性能 / token**
- 连续 context 跑数千行代码，compact 阈值与 keep_pairs 的组合是否真能压住 token？有无更省的切法。
- `_MAX_STATE=4000` 截断、execute_command 头尾截断：截断策略会不会丢掉关键信息（如 traceback 尾部）？
- 每次 execute_command 都读+写状态文件，高频命令下的 I/O 开销是否值得？

**P2 — 可维护性**
- 注意：这是实验性代码，可维护性优先级最低。只标重大坏味道（重复逻辑、隐藏耦合、魔法数、`solo()`/`_solo_drive` 职责切分是否合理），不要纠结风格。

### 输出格式

按 `P0 正确性 / P0 安全并发 / P1 性能token / P2 可维护性` 四节组织。每条 finding 用：

- **[严重度 高/中/低] 一句话标题**
- 位置：`文件:行号` 或函数名
- 问题：具体说清触发条件和后果
- 建议：最小修复方向（不必给完整代码）

最后给一个 **总体判断**：这批增量代码可否直接合入主干长期使用，还是有必须先修的阻塞项。请只报你有把握的问题，不确定的标注「待确认」，不要为凑数量编造 finding。

---

## 附：如何提取源码喂给第三方

```bash
# 按上表行号导出片段，或直接给整文件（agent.py 较大，建议只给相关函数）
sed -n '3957,4200p' agent.py   # _solo_drive + solo
sed -n '2229,2360p' agent.py   # _SOLO_ROLE
sed -n '1358,1470p' agent.py   # compact 两函数 + 注入
sed -n '1,70p'      tools.py   # _STATE_CMD_RE + _update_agent_state
sed -n '349,460p'   tools.py   # execute_command
```
