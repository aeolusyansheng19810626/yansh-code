# Solo Mode 深挖 Review — 给 gpt-5.5（运行时逻辑 / 信号 / 数据流）

> 第二轮定向深挖。上一轮你独家抓出「gate 覆盖成功」「stderr or stdout 丢 traceback」两个真实运行时 bug——本轮发挥同样的控制流/数据流/状态机视角，做穷举式深挖。
> 用法：把下面「提示词正文」整段复制给 gpt-5.5，连同附带源码文件一起提供。

---

## 提示词正文（复制以下全部）

你是一名资深 Python 工程师，受邀对一个 AI coding agent 框架的 **solo mode 增量代码**做第二轮深挖 review。第一轮已完成，本轮**聚焦运行时逻辑 / 数据流 / 状态机**，请只就提供的源码判断，不臆测未给出的实现。

### 背景

命令行 AI coding agent（类 Claude Code），自主规划→读写文件→执行命令→自测，端到端完成多文件任务。底层 LLM 经 IBM ICA 网关调用，**网关不透传 prompt cache**，长对话 input token 是 O(N²) 全额重发——token 是一等约束。

**solo mode**：单一连续 context 主循环（`_solo_drive`），跑完后外部 **test gate** 把失败 stderr 回灌进**同一条 messages** 继续驱动修复（最多 8 轮）。配 compact 压缩 + 框架自动维护的环境知识文件 `.yansh/agent_state.md`。

### 第一轮已裁决的结论（**不要重复报告这些**）

以下已被源码核实，无需再提：

- **threading.Lock 不是跨进程 bug**：并行编排（`10_parallel_orchestrator.py`）为每个子任务建独立 git worktree + 独立进程 `main --cwd <worktree>`，`agent_state.md` 落各自 worktree，无跨进程共享。残留仅「非原子 write_text 崩溃损坏」低危。
- **compact M1 边界安全**：`_split_messages_into_pairs` 把 assistant+其 tool_result 绑同一 pair，孤立 tool_result 基本不可能。
- **compact 会丢开场规划**（已确认真问题，修法另议）：摘要 prompt `_SUMMARIZE_SYSTEM` 强制保留「改动文件+成功命令」，但不含「开场规划/接口契约」。
- **正则 `_STATE_CMD_RE` 缺词边界**（已确认，低危）：`pythonic_tool` 会被误匹配。
- **已采纳的你的两个真 bug**：gate `signal["success"]=True` 覆盖失败信号；`raw = stderr or stdout` + `raw[-4000:]` 丢 traceback。本轮请**深化**它们的修法与边界，而非重复发现。

### 本轮深挖任务（按优先级）

**任务 1 — signal 生命周期真值表（最高优先）**
`solo()` 里 `signal["success"]` 被多处读写：
- `_solo_drive` 的 4 个 return 分支：task_complete(成功) / task_complete(放弃 success=false) / no_progress 熔断 / 沉默退出 / soft_limit 上限；
- gate 循环里 5 处：无测试命令 `break`（不改 signal）、judge 绿 `=True`、轮次耗尽 `=False`、达 gate 上限 `else` 分支 `=judge(test_result)`、回灌后重新 `_solo_drive` 整体覆盖 signal。

请构造一张**真值表**：纵轴 = agent 最终意图（task_complete成功 / 主动放弃 / 熔断 / 沉默 / 超限），横轴 = gate 测试结果（无测试可跑 / 绿 / 红到耗尽 8 轮）。每格填**最终返回的 success 值**，并标出所有「与任务实际是否完成不符」的格子。已知「agent 放弃但旧测试绿→被覆盖成 True」是其中一格，请**找全所有错配格**（尤其：agent 自称成功 + gate 无测试 break → signal 保留 True，等于零外部复核就报成功）。

**任务 2 — 错误回灌通道的确定性规则**
结合 `06_tools_execute_command.py`：stdout/stderr 由两个独立线程分别收集、各自走 `_truncate_cmd_output`（头尾各 3000）。判断 `pytest` / `python -m` 真实失败时 traceback、FAILURES 摘要、assert diff 分别落在哪个通道，给出**替代 `stderr or stdout` + `raw[-4000:]` 的确定性「回灌内容选择」规则**（要具体到：合并还是择一、按什么标志位 FAILED/Traceback/Error 选、截断该保头还是保尾）。

**任务 3 — gate↔drive 共享预算的烧钱上界**
`no_progress_state["total_rounds"]` 跨 gate 累积，`rounds_used` 每次 drive 重置，`gate_round` 独立计数，`soft_limit` 每次 drive 传同一 `_SOLO_SOFT_LIMIT=120`。`total_rounds >= soft_limit` 的检查只在**每轮 gate 开头**。请追出：
- 最坏情况下总 LLM 轮数 / token 的上界；
- agent 在某一轮 gate 内陷入长修复循环时，因 total_rounds 只在 gate 轮首检查而超烧的具体路径；
- 是否存在「同一错误反复回灌、drive 无有效修改」的不收敛路径（gate 未检测 test_cmd/err/修改文件集是否变化）。

**任务 4 — scope 数据流：无复核即成功的完整触发链**
链路：`snapshot_files_modified`（`08`）→ `_infer_test_scope`（`08`，源文件 X.py 找 `tests/**/test_<stem>.py`）→ `_detect_python_test_cmd`（`08`）。穷举 agent「写了代码但 gate 测不到」的所有分支：
- 没建 tests/ 目录；
- 测试没按 `test_*` / `*_test.py` 命名；
- 只改了测试文件本身；
- 新建文件未被 `snapshot_files_modified` 记录。

每个分支说明 gate 最终走向（`break` 跳过复核 / 跑了无关测试 / 回退全量）。**结合任务 1**，给出「agent 自称成功 → gate scope 落空 break → 报 success=true 但零测试复核」的完整触发链，并评估这是不是比单个 bug 更严重的系统性失效。

### 输出格式

沿用上轮：每条 finding 用 `[严重度 高/中/低] 一句话标题` / 位置 `文件:行号` / 问题（触发条件+后果）/ 建议（最小修复方向）。**任务 1 请直接给真值表**。只报有把握的，不确定标「待确认」，不要凑数。

### 附源码

- `04_agent_solo.py`（_solo_drive + solo + gate）
- `06_tools_execute_command.py`（stdout/stderr 双通道 + 截断）
- `08_scope_chain.py`（snapshot → infer_scope → detect_test_cmd）
- `09_dispatch_workspace.py`（dispatch 返回结构）
