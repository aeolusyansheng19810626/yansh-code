# Solo Mode 深挖 Review — 给 opus 4.8（跨文件链 / 真伪裁决 / 设计意图）

> 第二轮定向深挖。上一轮你独家抓出最隐蔽的「test gate 30s 超时」，并对多条 finding 做了准确的真伪裁决（区分「算/不算 finding」）。本轮我已把你上次标「取决于审范围外 X」的源码补齐，请先复核我据此做的裁决，再往新地带深挖。
> 用法：把下面「提示词正文」整段复制给 opus 4.8，连同附带源码文件一起提供。

---

## 提示词正文（复制以下全部）

你是一名资深 Python 工程师，受邀对一个 AI coding agent 框架的 **solo mode 增量代码**做第二轮深挖 review。你的专长是跨文件调用链追踪与真伪裁决。本轮请只就提供的源码判断，不臆测未给出的实现。

### 背景

命令行 AI coding agent（类 Claude Code），自主规划→读写跑修→自测，端到端完成多文件任务。底层 LLM 经 IBM ICA 网关调用，**网关不透传 prompt cache**，长对话 input token 是 O(N²) 全额重发——token 是一等约束。

**solo mode**：单一连续 context 主循环（`_solo_drive`），跑完后外部 **test gate** 把失败输出回灌进**同一条 messages** 继续驱动修复（最多 8 轮）。配 compact 压缩（旧消息对走 LLM 摘要）+ 框架自动维护的环境知识文件 `.yansh/agent_state.md`（按 exit code 把 python/pytest 命令分类写白/黑名单，注入 system prompt）。

### 第一部分 — 复核我对你上轮「待确认」项的裁决

我已补齐源码，请基于新附文件**确认或推翻**以下三条，每条给「确认/推翻」+ 一句依据：

1. **threading.Lock（你上轮标「取决于 workspace 是否共享」）**
   `10_parallel_orchestrator.py`：每个子任务建独立 git worktree（`base_cwd/.yansh/worktrees/<name>`）+ 独立进程 `main --cwd <wt>`，`agent_state.md` 落各自 worktree。
   → 我判：**threading.Lock 不是跨进程 bug**，仅残留「非原子 `write_text` 进程中途崩溃损坏文件」低危。
   请核：(a) 此判断对否？(b) 同一进程内 subagent 并发（`09` 里 `_dispatch_tool_calls` 用 ThreadPoolExecutor 跑 ≥2 个 subagent，各 subagent 内 `execute_command` 写同一 state 文件）时，`threading.Lock` 是否真的够用？(c) 是否值得加原子写（tmp + os.replace）。

2. **compact M1 边界（你上轮标「取决于 `_split_messages_into_pairs` 配对粒度」）**
   `07_compact_internals.py` 含该函数：每个 pair = assistant + 紧随 tool messages；rest 起始零散 user 单独成 pair。
   → 我判：assistant 与其 tool_result 绑同一 pair，**孤立 tool_result 基本不可能**，M1 的「recent 起始合法」检查充分。
   请核：是否有反例——如 rest 起始即零散 `tool` 消息（无 assistant 在前）、单个 pair 含多个 assistant、或 old_pairs 被推空后 recent 起始仍非法的退化路径？

3. **摘要是否保签名（你上轮标「取决于 `_summarize_old_history` 是否逐字保签名」）**
   `07` 含 `_SUMMARIZE_SYSTEM`：强制项覆盖「③改动文件名+函数」「⑥逐字保留成功 shell 命令」，但**不含「开场规划 / 文件清单 / 跨文件接口契约 / symbol_contract」**。
   → 我判：「compact 丢 plan-anchor」**成立**（与 `_SOLO_ROLE` 的「This plan is your anchor」直接冲突）。
   请核并二选一给最小修法 + 理由：(a) 给 `_SUMMARIZE_SYSTEM` 加一条强制项「逐字保留尚未落地的文件清单/接口签名/symbol_contract」；(b) 把首个 assistant 规划 pair 永久 pin 进 head 不参与压缩。哪个更省 token、更稳、更不易回退？

### 第二部分 — 深化你的独家发现（test gate 30s 超时）

你上轮指出 `test()→execute_command(_timeout_sec=30)`，大项目 pytest 必超时判红、烧满 8 轮。请深化：
- 给 test gate 一个**独立、可配置、更长**超时的具体改法（不影响普通命令的 30s）；
- 排查**同类「默认参数在长任务/大项目下静默失效」隐患**：`judge()` 只看 `returncode==0`，把超时(-1)、进程崩溃、真断言失败**混为一谈**。回灌时 agent 收到的只是一段被截断的输出，无法区分「测试太慢被 kill」与「代码真错」——这是否放大 30s 问题、导致 agent 在「修不动的超时」上空转？给出让 agent 能区分这三类的最小改动。

### 第三部分 — 新地带（上轮未覆盖的跨文件全局态）

1. **`_CURRENT_SNAPSHOT` 全局态一致性**：`solo()` 设一次空快照（`create_snapshot([])`），跨多次 `_solo_drive` + gate 回灌 + 并发 subagent 复用；写工具「按需增量备份」。多线程 subagent 并发写文件时，这个全局快照的读改写一致吗？solo 多轮下 `/revert` 语义是否还正确？

2. **solo 下 smoke test 被绕过？**：`08_scope_chain.py` 里 `_apply_test_scope_override` 有「若 `tests/test_smoke.py` 存在则强制并入 scope」的保险丝——但它只在 plan 路径被调用。solo **无 plan**，gate 直接用 `_infer_test_scope`（不含 smoke 强并入）。
   → 推断：solo 下端到端 smoke test 可能根本不会被 gate 跑到，而 `_SOLO_ROLE` 又要求「必须跑真实入口验证」。请核实这个矛盾是否成立，以及它如何与第一部分第 3 条（compact 丢规划）叠加放大「跨文件 CLI 调用链断裂但单测全绿」的经典失效（背景：曾出现 200 单测全绿但 `python -m pkg` 9/10 崩溃）。

### 输出格式

沿用上轮：`[严重度] 标题` / 位置 / 问题 / 建议，并明确区分「确认 / 推翻 / 待确认」。第一部分对每条先给「确认/推翻」结论再展开。最后给**总体判断**：补齐这些后，阻塞项清单相比上轮如何收敛。只报有把握的，不凑数。

### 附源码

- 上轮全部：`01_agent_compact.py` `02_agent_solo_role.py` `03_agent_solo_consts.py` `04_agent_solo.py` `05_tools_update_state.py` `06_tools_execute_command.py`
- 本轮补充：`07_compact_internals.py`（pair 配对 + summarize prompt）`08_scope_chain.py`（snapshot→scope→test_cmd + smoke 强并入）`09_dispatch_workspace.py`（dispatch 返回结构 + _get_workspace）`10_parallel_orchestrator.py`（每 worktree 独立 workspace 证据）
