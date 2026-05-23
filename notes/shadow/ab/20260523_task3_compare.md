# AB Test #3：架构论证 — `task_complete` sentinel → NL 信号可行性

**提示词**（两边一致核心要求）：
> 评估 yansh-code 项目把 `task_complete` 从 sentinel 工具改成 LLM 自然语言信号的可行性。
> 给 (1) 改动范围 (2) 与 fix loop / dispatch_subagent / task_complete_signal 的兼容 (3) 风险 (4) 推荐做或不做。
> 输出 markdown 文档，不写代码、不改 repo、不跑测试。

**模型**：Claude Sonnet 4.6（两侧）
**日期**：2026-05-23
**任务类型**：纯架构论证 / 只读分析
**预期**：因为不涉及"领域知识 + 测试 pipeline"，CC 应该更接近 yansh —— 是个好的对照组

## 数据对比

| 维度 | yansh (audit mode) | Claude Code 子 agent (general-purpose) |
|---|---|---|
| 用时 | **155.51s** | **116s** |
| 工具调用 | **50** | **12** |
| Token (in+out) | **730,452** (in 715,956 + out 14,496) | **169,371** |
| 文件改动 | **0**（audit 强制只读） | **0**（prompt 约束） |
| 输出长度 | 长篇结构化文档（~5KB） | 中等结构化文档（~3.5KB） |
| 推荐结论 | 不做 | 不做 |
| 折中方案 | 双模式兼容 + 严格前缀行格式 | 工具 + NL 并行（fallback） |

**yansh ≈ 4× CC 的 token 和工具调用** —— 比 task #2 的 25× 差距大幅缩小。**预期被验证**：纯论证任务 yansh 优势减弱。

## 完成质量差异

### yansh 优势：精确行号 + 表格化改动点

yansh 把改动点列成表格，每行带行号：

| 文件 | 函数 / 位置 |
|---|---|
| `tools.py:35-47` | `task_complete()` |
| `tools_schema.py:~L404` | task_complete JSON Schema |
| `agent.py:1041-1044` | `_dispatch_tool_call()` |
| `agent.py:1616-1630` | `code()` inner loop |
| `agent.py:1736-1750` | `audit()` loop |
| `agent.py:2184-2191` | `fix()` loop |
| `subagent.py:221` | `_run_subagent()` |
| `task_log.py:85-89` | `finish_task_log()` |

**4 文件 8 处** —— 行号都对得上 repo 当前状态（yansh 跑了 34 次 read_file 确认）。

CC 给的也是 4 文件，但行号粒度更粗（`L1042-1044`、`L1619-1629` 这种范围），且少了 `tools_schema.py` 的精确位置。

### CC 独家洞察（key_decisions 里）

CC 在 `key_decisions` 显式列了 yansh **没提**的两条 trap：

1. **`plan_chat` 的 `exit_plan_mode_signal` 是同类 sentinel**
   > 若只改 `task_complete` 而保留 `exit_plan_mode_signal`，系统内部会有两套收尾机制并存，设计一致性下降。

   yansh 的方案完全没提到这个——这是个真问题，repo 里 `agent.py:plan_chat()` 用的就是同样的 sentinel 模式，单改一个会留下不一致。

2. **system prompt 的预算提醒文本包含 "task_complete" 字样**
   > L1714、L2161 在 messages 里注入提示文本（"调用 task_complete(...)"），若 NL parser 不区分 `role: system`，会误触发。

   yansh 风险节里有 R1（LLM 误触发）但没具体定位到这两处注入点。CC 抓到了 hidden trap。

### yansh 独家洞察

1. **R3：success/failure 语义丢失** —— 工具有显式 bool 字段，NL 要二次 parse；CC key_decisions 里也有但措辞较弱。
2. **R6：多工具调用并发场景** —— `_dispatch_tool_calls` 处理一批并发工具时，NL 信号需"前移到 call_llm 返回处理层"。CC 没提。
3. **审计了 `agent.py:2361, 2438, 2477, 2491, 2506, 2513, 2528` 共 7 个出口都读 task_complete_signal** —— 这是 yansh 跑 50 次工具调用的回报：grep 后逐个 read_file 验证过，引用粒度极细。

### 推荐结论一致

两边都判 ❌ 不做，理由收敛：
- 工具协议（结构化）比 regex 解析（非结构化）严格更优
- 改动 4 文件 8 处，引入 NLP 歧义风险，收益为零
- 已有 `silent_prompted` 兜底处理"忘记 task_complete"场景

折中方案两边也接近：**优先工具调用，fallback NL**。yansh 进一步建议加 source 字段（`"source": "nl_signal"`）标记置信度——这条 CC 没给。

## yansh 这次为什么花这么多

50 个工具调用分布：

| 工具 | 次数 | 用途 |
|---|---|---|
| `read_file` | 34 | 反复读 agent.py / tools.py / subagent.py / task_log.py 不同段 |
| `search_in_files` | 8 | grep `task_complete` / `task_complete_signal` / `TASK COMPLETE` / `sentinel` / `fix_loop` |
| `task_complete` | 4 | audit 模式每个子任务调一次 |
| `dispatch_subagent` | 3 | 派子 agent 做"读 main.py 控制流"等子任务 |
| `list_symbols` | 1 | main.py 符号清单 |

**有趣点**：yansh **派了 3 个 dispatch_subagent** 自分子任务——task #1 和 #2 都没出现这个行为。audit 模式 + 大型分析任务下 yansh 自己用并行子 agent 拆解了"main.py 控制流"、"task_complete 实现细节"等独立子问题，这是 yansh 的递归自调用能力。

CC subagent 只用 12 个工具就给出方案：CC 的 sonnet 在主响应里直接整合，没有自分子任务（`Agent` 工具在它手上但它没用）。

## 流程差异

**yansh audit 模式**：
- 强制只读（`READONLY_TOOL_NAMES` 限定工具集）
- 多轮 read_file + grep 验证 → dispatch_subagent 拆子任务 → 整合
- 最后通过 `task_complete(success=True)` 显式收尾，把 summary 落进 task_log

**CC general-purpose subagent**：
- 自由工具集，但 prompt 严格约束（"不写代码、不改 repo"）
- 单轮多工具调用 → 收尾前生成 `key_decisions`
- 主响应直接 return，没有显式信号

## task_complete_signal 的意外彩蛋

yansh 这次跑的 task_log 里 `task_complete_signal.success=True`，summary 完整。**这次任务本身就是论证 task_complete 该不该改**——而 yansh 自己作为"被论证对象"调用了 task_complete 来收尾。**自循环验证了它当前设计的可观测性确实有用**——log 里能看到"yansh 主动声明这次审计成功收尾"，这正是 task_complete_signal 字段存在的价值。论证结论"不做"也对应：连作为执行 agent 的 yansh 自己都享受这个机制的好处。

## 总结：什么场景选什么（更新）

| 任务类型 | 推荐 | 倍率 |
|---|---|---|
| 探索 / 信息检索（task #1）| **CC** | yansh ≈ 1.5× |
| 严格按字面要求小改 + 加测（task #2）| **CC**（25× 便宜，刚好满足） | yansh ≈ 25× |
| **完整功能落地**（含 schema、文档、清理）| **yansh** | 多花 25× 但语义闭环 |
| **架构论证 / 纯只读分析**（task #3） | **看深度需求** | yansh ≈ 4× |
| 不熟悉的代码库 | **yansh**（plan/audit 强制只读，更安全） | — |

**task #3 新发现**：架构论证任务下，**yansh 输出更详细 + 行号更准 + 派子 agent 自动拆解** vs **CC 简洁 + key_decisions 抓 trap 更准（plan_chat 类比、system prompt 注入）**。两边都得 4 文件 8 处的核心改动点和"不做"的结论——决策一致，过程不同。

如果你需要的是**精确改动清单 + 行号引用**用来下手改代码，选 yansh；
如果你需要的是**快速判断要不要做 + hidden trap 提醒**用来决策，选 CC。

## 数据收集总结

3 次 AB 跑下来，CC subagent 在不同任务类型上的表现稳定性：

| Task | yansh 优势 | CC 优势 |
|---|---|---|
| #1 探索 | docstring → 设计意图 | 路径短 |
| #2 写代码 | schema 闭环 + 死代码清理 | 25× 便宜 + key_decisions 抓 monkeypatch trap |
| #3 论证 | 精确行号 + dispatch_subagent + 风险点更全 | key_decisions 抓 plan_chat 类比 + system prompt 注入 trap |

**yansh 的"领域知识"优势**在 task #3 缩到 4×，验证假设——但**输出深度仍可见差距**，特别是 yansh 派子 agent 拆解的能力。CC 的 key_decisions 字段在三次任务中都贡献了**额外洞察价值**——这是 prompt 设计的功劳，不是模型能力差异。

## 附原始数据

- `20260523_task3_yansh.json` — yansh batch JSON 输出（含 audit body markdown + metadata）
- `20260523_task3_yansh_body.md` — 拆出来的 audit markdown body
- `20260523_task3_yansh_stderr.log` — yansh stderr console（rich 渲染的完整方案文档）
- CC subagent transcript：仅在父对话里，未单独保存

## 下一步候选

- task #4：bug 复现 / 修复任务（给一个真 bug repro 步骤，看 yansh 的 fix loop vs CC 单循环 debug 谁更快定位）
- task #5：跨文件重构（改一个广泛使用的函数签名 + 全 repo 适配，看 yansh 的 plan→code→fix 流水线 vs CC 的"读 + 改 + 验证"循环）
- 收尾：把 3 次 AB 的关键结论整合成一篇 README，方便后续选型查阅
