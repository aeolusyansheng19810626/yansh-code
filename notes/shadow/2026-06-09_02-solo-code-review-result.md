deepseek v4 pro的review 结果：
## P0 — 正确性

### [严重度 高] `_update_agent_state`「先失败后成功」重分类：精确行匹配可能漏删含尾部空白的命令
- **位置**：`tools.py:_update_agent_state`，行 50-63
- **问题**：写入时用 `entry_line = f"- `{command.strip()}`\n"`，匹配时用 `l == entry_line`。若某次调用传入的 `command` 原始值尾部带空格（`command.strip()` 会去掉），但后续同命令原始值不带空格，两次生成的 `entry_line` 一致——这是安全的。真正的风险在反向：若 `execute_command` 传入的 `command` 包含不可见字符（如 U+200B 零宽空格），`strip()` 可能不处理这些字符，两次调用生成的 `entry_line` 不同，导致旧行漏删、文件中出现重复条目。**待确认**：`command` 来源是否为 LLM 生成的工具调用参数，LLM 可能产出含不可见 Unicode 的字符串。
- **建议**：在 `_STATE_CMD_RE.match` 之后额外对 `cmd_stripped` 做一次 `unicodedata.normalize('NFKC')`；或将匹配逻辑从精确行匹配改为「按 `command.strip()` 值去重」，先解析已有 section 内容为 set，再做增删。

### [严重度 高] compact 后 `recent_pairs` 合法性修正循环在 `head` 为 `[system, user_initial]` 时可能推空 `old_pairs`
- **位置**：`agent.py:_compact_messages`，行 42-46
- **问题**：while 循环条件是 `while recent_pairs and old_pairs:`，仅当两者都非空才推。若 `recent_pairs[0][0]` 不是 `assistant/user`（比如是 `tool` 消息）、且 `old_pairs` 已空（极端场景：总共只有 `keep_recent_pairs` 个 pair），循环不执行，`recent_pairs` 起始角色仍非法，拼接后会产生违反 OpenAI tool_result 必须紧跟 assistant 约束的消息序列。
- **建议**：循环结束后增加断言/检查：若 `recent_pairs` 非空且 `first_role not in ('assistant', 'user')`，返回原 `msgs`（降级不压缩），避免产生非法消息序列。

### [严重度 高] test gate 回灌错误输出只取末尾 4000 字符，可能丢失 traceback 头部关键信息
- **位置**：`agent.py:solo`，行 198
- **问题**：`err_excerpt = raw[-4000:]` 只保留末尾。但 Python traceback 最有价值的信息在**最后一帧的头部**（哪个文件、哪一行、什么异常类型）和**最底部的原因链**。4000 字符通常足够覆盖尾部，但当 traceback 链很深（如 pytest 的 `_pytest.assertion.rewrite` 展开长列表 diff）时，`raw[-4000:]` 可能截在 diff 中间，丢掉「实际引发异常的那一行文件和行号」。这与 `_truncate_cmd_output` 的头尾各保留策略不一致。
- **建议**：与 `_truncate_cmd_output` 对齐：`raw[:2000] + "\n...\n" + raw[-2000:]`，或至少确保最后一段包含 `Error` / `FAILED` 行的上下文。

### [严重度 中] gate 回灌后 `_solo_drive` 可能因 `rounds_used` 重置而绕开 no_progress 熔断
- **位置**：`agent.py:_solo_drive`，行 8-10；`solo`，行 207-209
- **问题**：每次 gate 回灌重新调用 `_solo_drive`，`rounds_used` 重置为 0，而 `no_progress_state` 跨 gate 持续累积。当 agent 在回灌修复中连续做探索（纯 read/search）未写文件，`streak` 会累积并可能触发 `2*_SOLO_NO_PROGRESS_CAP` 熔断。这本身是正确的。但 `rounds_used` 重置意味着**每轮 gate 回灌可以独立消耗最多 `soft_limit` 轮**，可能导致实际总轮次远超 `_SOLO_SOFT_LIMIT`。行 193 的 `total_rounds >= _SOLO_SOFT_LIMIT` 检查部分缓解，但只在每轮 gate 开始时检查——如果 agent 在第 N 轮 gate 陷入长修复循环，仍会烧 token。
- **建议**：gate 回灌给 `_solo_drive` 传一个递减的 `soft_limit`，用 `_SOLO_SOFT_LIMIT - no_progress_state['total_rounds']` 代替固定值。

### [严重度 中] sentinel 扫描循环中对 `out["result"]` 做字典方法调用，若 `_dispatch_tool_calls` 对某些工具返回非 dict 会抛异常
- **位置**：`agent.py:_solo_drive`，行 53-56
- **问题**：`out["result"].get("_task_complete")` 假设 `out["result"]` 是 dict。若某工具返回的 result 是字符串（如某些 MCP 工具或错误路径），`.get()` 会抛 `AttributeError`，导致整个主循环崩溃。
- **建议**：加守卫 `if not isinstance(out.get("result"), dict): continue`。

### [严重度 低] `_parse_pytest_failures` 函数读入文件未展示完整实现
- **位置**：`agent.py:_parse_pytest_failures`，行 240-246
- **问题**：代码被截断（行 246 后是 `import re as _re`）。若该函数在 gate 回灌中被使用但未完整实现，回灌分析能力受限。**待确认**：该函数是否在 gate 回灌逻辑中被实际调用，还是仅定义未使用。

---

## P0 — 安全 / 并发

### [严重度 高] `_STATE_FILE_LOCK` 是 `threading.Lock`，多进程/并行编排下无法防止文件竞争
- **位置**：`tools.py`，行 31；`agent.py:solo` 以 `subprocess` 调命令
- **问题**：本框架有 `parallel_orchestrator.py` 并行编排能力。若多个 solo agent 在不同进程（或 worktree）中同时运行，`threading.Lock` 完全无效——每个进程有独立的内存空间。可能导致：两个进程同时写入 `agent_state.md`，产生交错/损坏内容；或一进程删除另一进程刚写入的条目。
- **建议**：改用文件锁（`fcntl.flock` / `msvcrt.locking` 或 `filelock` 库）或用 `tempfile + os.replace` 做原子写入。最低限度至少用 `portalocker` 或 `atomicwrites` 等跨平台库。

### [严重度 中] `_STATE_CMD_RE` 正则 `^\s*(py\b|python[0-9.]*|pytest)` 存在误匹配和绕过风险
- **位置**：`tools.py`，行 30
- **问题**：
  1. `py\b` 边界仅右侧有 `\b`。若命令以 `py ` 开头（`python` 的 Windows launcher），能正确匹配。但 `py` 左侧无边界，形如 `spy` 不会误匹配（因 `^\s*` 前面无字符），基本安全。
  2. `python[0-9.]*` 会匹配 `python3.11`、`python` 等，但不会匹配 `pythonic_tool`（后面有字母，不在 `[0-9.]` 字符类）。此处较安全。
  3. **核心风险**：恶意或畸形命令 `python -c "import os; os.system('rm -rf /')"` 会被匹配，其完整命令（不含换行、<160 字符）会原文写入 `agent_state.md` 并被注入 system prompt——如果 LLM 生成的命令包含 markdown 注入内容（如 `python -c "print('## 已验证命令（exit=0）\n- ...')"  `），状态文件解析可能被污染。
- **建议**：对 `command` 做额外清理：移除 backtick、换行、markdown 标记符后再写入；或改用结构化格式（JSON）而非 markdown，避免注入面。

### [严重度 中] 状态文件内容注入 system prompt 存在 prompt injection 风险
- **位置**：`agent.py:_compact_messages` 行 68；`agent.py:solo` 行 150
- **问题**：`agent_state.md` 内容（含 LLM 曾执行过的命令原文）被直接拼入 system prompt。攻击面：若 agent 执行了 `python -c "print(...)"` 且输出内容被写入状态文件（exit=0），这些内容会被注入回 system prompt，可能含对抗性指令覆盖 agent 行为。
- **建议**：注入前对状态文件内容做 sanitize：过滤或转义 markdown 控制字符；或使用 ` ``` ` fenced code block 包裹，确保内容不被解析为 prompt 指令。

### [严重度 低] 状态文件路径来自 `_get_workspace()`，无路径穿越检查
- **位置**：`tools.py:_update_agent_state`，行 43
- **问题**：`Path(_get_workspace()) / ".yansh" / "agent_state.md"` 依赖于 `_get_workspace()` 返回合法路径。若框架配置允许用户设置任意 workspace 路径（如 `../../etc`），可能写到预期目录之外。**待确认**：`_get_workspace()` 是否已做规范化/范围检查。

---

## P1 — 性能 / Token

### [严重度 高] compact 阈值默认 40K、keep_recent_pairs=2 可能不足以压制 O(N²) 膨胀
- **位置**：`agent.py:_make_compact_state` 行 83-84；`_SOLO_TOKEN_BUDGET=600K`
- **问题**：默认 compact 阈值 40K 触发，但在连续 context 跑数千行代码的多文件任务中，每轮工具输出（read_file 全文、execute_command 的输出截断）累积极快。keep_recent_pairs=2 仅保留最近 2 轮原始对话——若 agent 在此间写入多个文件，旧文件内容、早期规划、接口约定全被摘要化。LLM 后续回合将只能依赖摘要回忆这些信息，摘要质量决定了 agent 后期是否会跑偏。`_summarize_old_history` 实现未给出，调用外部 LLM 又烧额外 token。
- **建议**：
  1. 阈值应可配置且与模型 context window 实际大小挂钩（如模型 200K 窗口则阈值设 60-80K）。
  2. keep_recent_pairs 建议提升到 3-4，尤其对于写重依赖文件的场景。
  3. 在摘要中保留「开场规划」原文（不压缩第一条 assistant 消息中的文件清单/接口签名）。

### [严重度 中] `_truncate_cmd_output` 头尾各 3000 字符 + gate 回灌取尾部 4000 字符，可能丢失中间关键报错
- **位置**：`tools.py:_truncate_cmd_output` 行 21-27；`agent.py:solo` 行 198
- **问题**：头尾各 3000 对 pytest 场景合理（头是收集阶段、尾是 FAILURES 摘要）。但对于长编译输出（如 TypeScript tsc 报错 50+ 个文件），关键的第一个错误在头 3000 以内的概率高，截断策略基本安全。但 `err_excerpt = raw[-4000:]` 在 gate 回灌时不一致：若 gate 运行的也是 pytest 且 stderr 很长，只取尾部 4000 可能不够。
- **建议**：gate 回灌统一用 `_truncate_cmd_output`。

### [严重度 中] 每次 `execute_command` 都读取全文 + 写入 `agent_state.md`，高频命令下 I/O 开销显著
- **位置**：`tools.py:_update_agent_state`，行 48-72
- **问题**：即使命令未命中 `_STATE_CMD_RE`（行 36 的 early return），每次 `execute_command` 仍调用此函数。命中后每次都读全文→逐行扫描→写回。当 agent 循环运行 `python -c "..."` 快速验证语法时，这是一笔不小的 I/O 开销。文件小时问题不大，但随着时间积累条目增多会退化。
- **建议**：加内存缓存（TTL 过期），只在条目真正变化时写盘；或先检查文件 mtime，若未变化则跳过重读。

### [严重度 低] `_MAX_STATE=4000` 硬截断可能丢环境信息
- **位置**：`agent.py:_compact_messages` 行 65；`agent.py:solo` 行 147
- **问题**：按字符截断（`_state_content[:_MAX_STATE]`），可能截在 markdown section 标题或条目中间，产生格式损坏的注入内容。另外 4000 字符约为 1000 token，相对于 40K compact 阈值占比适中，但若环境命令条目多（积累后）可能大量被截。
- **建议**：按行截断而非按字符，保证最后一行完整；或在条目级做 LRU 淘汰而非粗暴截断。

---

## P2 — 可维护性

### [严重度 中] 状态文件注入逻辑重复出现在 3 处
- **位置**：`agent.py:_compact_messages` 行 60-70、`agent.py:solo` 行 142-152、以及隐含在 `_maybe_compact_messages` 后的 compact 路径
- **问题**：同样的「读 `.yansh/agent_state.md` → 截断 → 注入」逻辑重复编码，`_MAX_STATE=4000` 硬编码在两处。
- **建议**：提取为 `_inject_agent_state(base_text: str) -> str`。

### [严重度 低] `_MAX_STATE=4000` 在 `_compact_messages` 中是局部变量，在 `solo` 中是局部变量（同名不同作用域），且与 `_CMD_OUTPUT_HEAD=3000` 同一文件不同函数分散定义
- **位置**：多处
- **建议**：统一为模块级常量 `_AGENT_STATE_MAX_CHARS=4000`。

### [严重度 低] `_solo_drive` 与 `solo` 职责切分基本合理，但 `no_progress_state` 在两个函数间共享 mutate 且重置逻辑有隐式耦合
- **位置**：`agent.py:_solo_drive` 行 70-87；`agent.py:solo` 行 169、207-209
- **建议**：`no_progress_state` 的初始化与生命周期管理应由 `solo` 独有，`_solo_drive` 只读 `streak` 并在调用方重置，避免跨 gate 调用时隐式共享 mutable state。

---

## 总体判断

**不建议直接合入主干长期使用。**存在以下必须先修的阻塞项：

1. **多进程锁不安全**（`threading.Lock` → 文件锁）：并行编排下会导致 `agent_state.md` 数据损坏。
2. **compact 重分类循环边界 bug**（`recent_pairs` 起始非法时无降级）：可能在边缘 case 产生违反 API 约束的消息序列，导致 LLM 调用失败。
3. **`out["result"]` 非 dict 时 sentinel 扫描崩溃**：会导致主循环异常退出，任务失败。

建议修复以上三项后再合入。其余发现（gate 回灌输出截断策略、compact 阈值保守、prompt injection 风险）可在后续迭代中处理。

---------------------------------------------------
gemini pro 3.1的review结果：
Listed directory yansh-code
Listed directory review_src_20260609
Viewed 01_agent_compact.py:1-116
Viewed 02_agent_solo_role.py:1-135
Viewed 03_agent_solo_consts.py:1-7
Viewed 04_agent_solo.py:1-247
Viewed 05_tools_update_state.py:1-73
Viewed 06_tools_execute_command.py:1-115

对该增量代码的 Review 结果如下。本次改动虽在架构思路上实现了端到端长连 context，但在边界条件、Token 保护及状态共享上存在严重缺陷。

### P0 — 正确性

- **[严重 高] `_update_agent_state` 的重分类逻辑存在文件读写与概念混淆漏洞**
  - **位置：** `05_tools_update_state.py:63` (`lines = [l for l in lines if l != entry_line]`)
  - **问题：**
    1. **解析遗漏**：`splitlines(keepends=True)` 解析时，若文件的最后一行没有末尾换行符，与带 `\n` 的 `entry_line` 做 `==` 匹配将永远为假，导致旧项无法被删掉，产生多行重复。
    2. **环境知识概念污染**：`pytest` 或 `python test.py` 失败（exit≠0）绝大多数是因为**代码逻辑有 Bug 或测试用例挂了**，而不是“当前环境不支持 pytest 命令”。如果把跑红的 `pytest` 强行划入黑名单（`## 失败命令`）跨 run 复用，未来的 agent 看了会被误导，认为环境里禁止用 pytest，从而彻底抛弃自测机制。
  - **建议：** 改用字典+正则精确替换而非全文件行遍历；明确剥离测试类命令的记录，或者仅对 `exit=127` (Command not found) 等环境级别的报错记入黑名单。

- **[严重 高] Compact 会丢失极关键的「开场规划」导致 Agent 跑偏**
  - **位置：** `01_agent_compact.py:21-27` `head_count` 截断计算
  - **问题：** 当前 `head_count` 最多只能算到 2（只保留首条 system 提示词和首条 user 原始诉求）。然而根据 prompt 设计，Agent 会在**首个 assistant 回复**中输出最关键的「开场规划清单与 symbol_contract」。这个极其重要的锚点消息被落入了 `old_pairs` 并进入 LLM 摘要引擎，极大可能被 summary 模糊掉甚至丢弃，导致 Agent 在长对话后期完全忘掉模块边界与最初的依赖设计。
  - **建议：** 修正 `head_count`，如果 `msgs[2]` (首个 assistant) 包含了规划，必须作为 immutable head 一起保留，即 `head_count` 加 2（包含对应的 user_initial_result）。

- **[严重 高] test gate 回灌存在无视 Agent 放弃意图的死循环陷阱**
  - **位置：** `04_agent_solo.py:192` test gate 的 while 循环
  - **问题：** 如果 Agent 在 `_solo_drive` 内识别到代码逻辑无解，或达到自身能力极限，主动调用 `task_complete(success=false)`。`_solo_drive` 返回后，外层的 gate 循环**完全没检查其 early_exit/success 意图**，直接又去跑测试，发现测试还是红的，于是 `gate_round += 1` 并强行把同样的错误日志再次回灌进去。Agent 会被迫陷入“放弃 -> 强行唤醒 -> 无动作放弃 -> 强行唤醒”的死循环，瞬间烧光 max_rounds 和大量 Token。
  - **建议：** 在 test gate 循环里追加意图检查：`if signal.get("early_exit") and not signal.get("success"): break`。

- **[严重 中] no_progress 熔断条件会被只读探针（如 `ls`/`cat`）轻易绕过**
  - **位置：** `04_agent_solo.py:65` `productive` 变量计算
  - **问题：** 只要工具名等于 `execute_command` 就算 productive，并重置熔断 streak 计数。假如 Agent 迷失，连续十多轮只运行 `cat` 或者 `ls`（这在实质上是不推进任务的空转），依然会被算作 "productive"，导致防死循环的软熔断机制彻底失效。
  - **建议：** 在此判定逻辑中拦截只读的探测命令，或为纯读取探测设置一个更高的独立熔断阈值。

- **[严重 低] 沉默兜底状态 `silent_prompted` 无法复原导致误杀**
  - **位置：** `04_agent_solo.py:90-101` `silent_prompted`
  - **问题：** `silent_prompted` 在整个 `_solo_drive` 循环里是全局单次有效的。如果 Agent 在第 2 轮不小心沉默了一次，触发警告并设为 `True`，随后它连续正常工作了 50 轮。但如果第 52 轮又偶然输出空白，由于它还是 `True`，框架会直接无警告终止任务。
  - **建议：** 在确认有工具调用的 `if msg.tool_calls:` 分支末尾，将 `silent_prompted = False` 予以重置。

### P0 — 安全 / 并发

- **[严重 高] 多进程/多 worktree 并行时 `threading.Lock` 无法保护状态文件**
  - **位置：** `05_tools_update_state.py:31` `_STATE_FILE_LOCK`
  - **问题：** 背景中提到「本项目有并行编排」，而 `threading.Lock` 仅能在单进程内的多线程中生效。如果外部的 orchestrator 同时拉起了多个独立 Python 进程操作相同的 `.yansh/agent_state.md`，读改写（Read-Modify-Write）必然发生数据竞争与损坏。
  - **建议：** 换用系统级别的文件锁（如基于 `fcntl/msvcrt` 或第三方 `filelock` 库）。

- **[严重 高] `_STATE_CMD_RE` 存在误匹配，且面临 Prompt 注入风险**
  - **位置：** `05_tools_update_state.py:30-40` 正则匹配与长度过滤
  - **问题：** 
    1. `python[0-9.]*` 后面没有限定单词边界 `\b`。像 `pythonic_script.sh` 这样的执行命令会被错误判定为基础 Python 命令。
    2. 虽然拦截了 `\n`，但未拦截 `\r`（回车符）和反引号 ``` ` ```。如果有命令夹带了这些字符写入 `.yansh/agent_state.md`，注入回 Prompt 后不仅会破坏外层的 Markdown 解析，甚至可能篡改 System Instruction。
  - **建议：** 加上结尾词边界 `r'^\s*(py\b|python[0-9.]*\b|pytest)'`；入库前剔除 `` ` `` 和 `\r`。

### P1 — 性能 / Token

- **[严重 中] test gate 回灌截断策略会丢失真正的 Error Traceback**
  - **位置：** `04_agent_solo.py:198` `raw[-4000:] if len(raw) > 4000 else raw`
  - **问题：** Python 和 pytest 报错时，核心的 Exception Traceback 栈堆通常位于输出流的中上部，最尾部往往只是一句简短的 `FAILED tests/x.py::y - AssertionError`。直接截取最后 4000 字符回灌，Agent 将无法看到报错明细，只能靠盲猜修复代码。
  - **建议：** 复用 `tools.py` 里的 `_truncate_cmd_output` 方法，将超长日志掐中段，保留 Head 和 Tail。

- **[严重 低] 状态注入文本在 Compact 期间遭遇“套娃式”无意义总结**
  - **位置：** `01_agent_compact.py:48-68` 和注入逻辑
  - **问题：** `rest` （包含上一次压进去的 `summary_system`，里面自带了长长的 `.yansh/agent_state.md` 注入）被送给了 LLM 重新做 `_summarize_old_history`。大模型会被迫阅读自己之前注入的环境知识并强行写进摘要。这不仅极大浪费了 O(N²) 的 summarization token，还会导致环境知识在摘要里异变。
  - **建议：** 将动态拼接的 `[持久环境知识...]` 在交给 LLM summarize 之前通过标识符剔除。

- **[严重 低] `_MAX_STATE` 的暴力截断会切断 Markdown 代码块**
  - **位置：** `01_agent_compact.py:66` 和 `04_agent_solo.py:148`
  - **问题：** `[:4000]` 生硬地按字符数把文件截断。很大概率刚好切在一个命令反引号或小节头内部，且总会把尾部最新积累的环境知识丢弃。
  - **建议：** 按行进行倒序聚合保留，或从末尾找最近的换行符进行安全截取。

### P2 — 可维护性

- **[低] 状态文件的读取及硬截断存在 Copy-Paste 散弹式修改**
  - **位置：** `01_agent_compact.py:61-68` 和 `04_agent_solo.py:143-150`
  - **问题：** 相同的读文件、异常捕获、4000字符截断、拼接逻辑被分别硬编码在两处。未来改格式极易漏改。
  - **建议：** 提取一个纯函数 `_get_agent_state_prompt() -> str` 并统一调用。

---

### 总体判断

**不能直接合入主干，存在必须先修的阻塞项。**
当前代码的 "Compact 丢弃开场规划" 会摧毁 Agent 长序列的目标锚点；"test gate 忽略 task_complete(success=false) 导致死循环" 则会在出错时直接烧穿 Token 预算并严重卡死；"多进程写同一文件用 threading.Lock" 会产生必然的 IO 竞争污染。建议优先修复上述 P0 并调整好回灌信息的头尾截断策略（P1）后，再行 Review 合入。

-------------------------------------------------------------
gpt-5.5的review结果：
• P0 正确性

  - [严重度 高] test gate 可能把失败/沉默的 agent 结果覆盖成成功
      - 位置：agent.py:4125-4168
      - 问题：_solo_drive() 即使因沉默退出、no_progress 熔断、soft limit 或 task_complete(success=false) 返回，solo() 仍
        进入外部 test gate；只要现有测试通过，就执行 signal["success"] = True。如果任务其实没完成、没修改，或需求不是测
        试可覆盖项，会误报成功。

      - 建议：只有在 agent 明确 task_complete(success=true) 或存在本轮有效修改时，test gate 才能把最终结果置为成功；否则
        测试通过只能作为附加信息，不能覆盖失败信号。

  - [严重度 高] gate 回灌可能丢掉真正失败输出
      - 位置：agent.py:4151
      - 问题：raw = stderr or stdout。pytest 常见情况是失败详情在 stdout，stderr 只有 warning/deprecation；只要 stderr
        非空，回灌给 LLM 的就是无关 stderr，真正 traceback 被丢掉，导致修复跑偏或烧轮次。

      - 建议：合并 stdout/stderr 并标注来源；或优先选择包含 FAILED/Traceback/AssertionError 的通道。

  - [严重度 中] gate 会重复回灌同一错误直到上限
      - 位置：agent.py:4132-4163
      - 问题：失败后没有检测“测试命令、错误摘要、修改文件集合”是否变化。若 _solo_drive() 未产生有效修改，下一轮会再次运
        行同一测试并回灌同一错误，最多烧 8 个 gate round 加主循环 token。

      - 建议：记录上一轮 test_cmd + err_excerpt hash + modified snapshot；若无变化则提前失败退出。

  - [严重度 中] no_progress 把任何 execute_command 都算进展
      - 位置：agent.py:4019-4024
      - 问题：重复运行同一个失败命令、无效命令、纯探测命令都会清零 streak，可能把空转拖到 _SOLO_SOFT_LIMIT=120。
      - 建议：只把“命令不同、输出带来新信息、或发生在写操作之后的验证命令”算进展；失败且重复的命令不应无限清零。

  - [严重度 中，待确认] compact 可能压掉开场规划里的关键 contract
      - 位置：agent.py:1331-1341, agent.py:1358-1431
      - 问题：开场规划在第一次 compact 后只进入 900 字摘要；如果规划里有跨文件 symbol contract、接口命名、约束，而代码尚
        未全部落地，摘要可能丢细节。

      - 建议：把首轮规划单独 pin 成不可压缩的短结构化 anchor，或在 compact 时强制保留“文件/符号/接口 contract”字段。

  - [严重度 低] 状态重分类正常 exact match 下可工作，但对重复行不稳
      - 位置：tools.py:44-68
      - 问题：同一命令先失败后成功、先成功后失败，在文件未并发损坏且命令字符串完全一致时能正确移动。但如果文件里因并发或
        人工编辑出现同一 entry_line 同时在两个 section，section_for_entry 只记录最后一次匹配，可能留下另一处重复。

      - 建议：无论当前 section 是什么，写入前都删除所有同一 entry_line，再追加到目标 section。

  P0 安全并发

  - [严重度 高] 线程锁挡不住多进程写状态文件

      - 建议：使用跨进程文件锁，并采用“写临时文件 + atomic replace”；或者改为 append-only 日志再定期归并。

  - [严重度 高] 状态文件存在 prompt injection 面
      - 位置：tools.py:44, agent.py:1416-1423, agent.py:4097-4104
      - 问题：命令字符串原样写进 markdown 反引号，再作为 system prompt 注入。当前只过滤换行和长度，未转义反引号、HTML/
        markdown 指令文本；预存在 .yansh/agent_state.md 的内容也会被整体注入。恶意仓库可污染后续 system prompt。

      - 建议：状态文件改结构化格式如 JSON，注入时渲染为明确 data-only 的转义文本；只接受框架生成的 schema，忽略自由文
        本；至少转义反引号和控制字符。

  - [严重度 中] 状态写入路径未做 workspace 边界校验
      - 位置：tools.py:41-42
      - 问题：Path(_get_workspace()) / ".yansh" 未 resolve() 后校验仍在 workspace 内；如果 .yansh 是 symlink，可把
        agent_state.md 写到 workspace 外。

      - 建议：解析真实路径并校验 relative_to(_WORKSPACE_ROOT)；必要时拒绝 symlink 目录。

  - [严重度 低] 命令识别正则误匹配
      - 位置：tools.py:28
      - 问题：python[0-9.]* 没有右边界，pythonic_tool 会匹配；pytest 也没有 \b，pytestwhatever 会匹配。ReDoS 风险不明
        显，但分类会污染状态。

      - 建议：改为 ^\s*(?:py\b|python(?:\d+(?:\.\d+)*)?\b|pytest\b)。

  P1 性能token

  - [严重度 中] compact thrash 后直接禁用，后续 token 可能失控
      - 位置：agent.py:1446-1479
      - 问题：连续 4 次压缩收益低就 disabled=True，之后在长任务里 messages 会继续增长；在 ICA 不透传 prompt cache 的背景
        下，这会把成本重新推回 O(N²)。

      - 建议：不要完全禁用；改成更激进策略，例如增加压缩比例、只保留 1 pair、丢弃低价值 tool output、或强制结构化 task
        memory。

  - [严重度 中] 600K token 预算只是过晚软提醒
      - 位置：agent.py:137-140, agent.py:3975-3989
      - 问题：_SOLO_TOKEN_BUDGET=600_000 只注入一次收敛提示，不阻断、不改变策略。对不透传 prompt cache 的网关，这个阈值
        偏像事后告警。

      - 建议：增加硬策略：超过阈值后禁止大范围探索、降低 keep pairs、强制执行收尾/测试，或暴露为配置。

  - [严重度 低] timeout 路径未截断输出且不更新状态
      - 位置：tools.py:439-447
      - 问题：超时返回原始 stdout/stderr，可能把大量输出塞回 LLM；同时 python/pytest timeout 不会进入失败命令黑名单。
      - 建议：timeout 返回也走 _truncate_cmd_output()，并按失败更新状态。

  - [严重度 低] _MAX_STATE=4000 前缀截断可能长期丢某个 section
      - 位置：agent.py:1420-1423, agent.py:4101-4104
      - 问题：状态文件按全文前 4000 字截断，如果一个 section 过长，后面的成功/失败 section 会完全不可见。
      - 建议：按 section 分别限量，保留最近 N 条成功和失败命令。

  P2 可维护性

  - [严重度 低] 状态注入逻辑重复
      - 位置：agent.py:1416-1423, agent.py:4097-4104
      - 问题：同样的读取、截断、拼 prompt 逻辑出现两份，后续修安全/格式时容易漏一处。
      - 建议：抽成 _format_agent_state_for_prompt(max_chars=4000)。

  - [严重度 低] gate 与主 loop 共享 no_progress_state["total_rounds"] 是隐藏耦合
      - 位置：agent.py:4120-4163
      - 问题：gate 修复轮会继续消耗主 loop soft limit，这是设计上可能合理，但当前不直观，调参时容易误判
        _SOLO_GATE_MAX_ROUNDS 的真实可用轮数。

      - 建议：显式命名为 shared budget，或拆出 gate 独立预算。

  总体判断

  不建议直接合入主干长期使用。必须先修的阻塞项是：test gate 覆盖失败为成功、gate 丢 stdout 失败详情、状态文件 prompt
  injection、跨进程写状态竞争。重分类逻辑在单进程、无重复行、命令完全一致的正常路径下基本可用，但需要和并发/去重一起补
  强。

-----------------------------------------------------------------
claude opus 4.8的review结果：
● 读完 6 个文件。_maybe_compact_messages 在第 116 行被截断（thrash 计数后应是 disabled 判定 + return
  msgs），逻辑可推定，下面据此评审。

  ---
  P0 — 正确性

  [中] _update_agent_state 重分类逻辑本身正确，但正则缺词边界导致误分类
  - 位置：tools.py:30 _STATE_CMD_RE
  - 问题：python[0-9.]* 与 pytest 两个分支后面没有 \b，.match 只锚定行首。pythonic_tool foo、pytestx
  ...、python_helper.sh 都会命中并被写进白/黑名单。只有 py\b 分支有边界。后果：污染状态文件（注入回
  prompt），不影响功能正确性。
  - 建议：改 ^\s*(py|python[0-9.]*|pytest)\b，给三个分支统一加词边界。
  - 注：重分类核心逻辑（先失败后成功 / 先成功后失败）经核对是对的——精确整行匹配 + 全量移除再追加，entry_line 恒带尾
  \n，splitlines(keepends) 能精确命中，不漏删不误删。这条不算 finding。

  [中-高] compact 会把「开场规划/symbol_contract」摘要掉，丢失 agent 的锚点
  - 位置：agent.py:_compact_messages head 切分（21-30）
  - 问题：head 只固定 [system, user_initial]。但 _SOLO_ROLE 要求的开场规划（文件清单、跨文件接口签名）写在第一个
  assistant turn，属于 pairs，会落进 old_pairs 被 LLM 摘要。keep_recent_pairs=2 只保最近两轮。长任务一旦 compact，agent
  自产的 symbol_contract 取决于 _summarize_old_history 是否逐字保签名——若摘要泛化，后期跨文件命名就会漂。而 role
  里明说「This plan is your anchor」，自相矛盾。
  - 建议：把第一个 assistant pair（规划轮）也 pin 进 head（head + first_assistant_pair + summary +
  recent_pairs），或单独抽取规划文本固定再注入。这是长期使用的实际隐患。

  [中] test gate 30s 硬超时会让大型测试永远判红、烧满 8 轮
  - 位置：agent.py:solo gate 循环（187）→ test() → execute_command(_timeout_sec=30)
  - 问题：judge 只看 returncode==0；execute_command 默认 30s 超时返回 returncode=-1。一个真实多文件项目的 pytest 很容易
  >30s，于是每轮 gate 都超时→判红→回灌一段 agent 根本无法「修」的 timeout，直到耗尽 8 轮 /
  soft_limit。把能跑过的任务误判为失败。
  - 建议：test gate 用独立、可配置且更长的超时（如 120–300s），与普通命令的 30s 解耦。

  [低] gate 不尊重 agent 的 task_complete(success=false) 主动放弃
  - 位置：agent.py:solo gate 循环
  - 问题：agent 已判定「卡死无解」返回 success=false 后，gate 仍继续跑测试+回灌再 drive，最多重复 8 轮。浪费
  token，且违背 role 里「不要烧光轮次」的约定。
  - 建议：gate 检测到上一轮 _solo_drive 返回 early_exit and not success（主动放弃）时，最多再给一轮就停。

  [待确认] compact 边界可能产生孤立 tool_call/tool_result
  - 位置：_compact_messages M1 修复（42-46）
  - 问题：M1 只保证 recent_pairs[0] 起始角色合法。但若 _split_messages_into_pairs 会把「带 tool_calls 的 assistant」与其
  tool_result 拆到相邻两个 pair，old_pairs 末尾被摘要后，recent 起始的 tool_result 仍可能孤立（OpenAI 要求
  assistant.tool_calls 必须有对应 tool 消息）。是否安全完全取决于 _split_messages_into_pairs
  的配对粒度——该函数未在审范围内。
  - 建议：确认 pairing 始终把 assistant + 其全部 tool_result 绑在同一 pair；否则 M1 的「起始合法」不足以保证整体合法。

  no_progress 熔断（productive = 写工具 或 execute_command，warn@6 / 熔断@12）判定可靠，R10 已修「连跑验证被误杀」，12
  轮纯探索容忍度足够，不构成误杀风险。sentinel 扫描与沉默兜底逻辑无边界 bug（兜底 silent_prompted 每次 drive
  重置是有意设计）。

  ---
  P0 — 安全 / 并发

  [中｜待确认] threading.Lock 防不住跨进程文件竞争
  - 位置：tools.py:31 _STATE_FILE_LOCK；写盘 tools.py:48-72
  - 问题：本项目有并行编排/worktree。threading.Lock 只在单进程内有效。若并行的多个进程共享同一 _get_workspace()（即同一
  .yansh/agent_state.md），读-改-写非原子 + 非原子 write_text，会丢更新甚至写坏文件。
  - 取决于：各并行 run 是否独立 workspace。若每个 worktree 自带 workspace（各自的 state
  文件），则无跨进程争用，现状可接受。
  - 建议：确认共享情况；若可能共享，改 OS 级文件锁（portalocker / msvcrt.locking）+ 原子写（tmp 文件 +
  os.replace）。即便不共享，也建议原子写防进程中途崩溃损坏文件。

  [低] 状态文件内容回灌 prompt 的注入面有限但未完全封死
  - 位置：tools.py:46 entry 拼接；注入在 agent.py:68 / 150
  - 问题：command 原文写入文件内容再注入 system prompt。现有过滤（含 \n 跳过、>160 跳过、行首恒为 - 无法伪造 ##  section
  header）已挡住大部分。残留：命令含 \r（lone CR，非 \r\n）不被 "\n" in 捕获，写入后 read-back 时 splitlines 会按 \r
  切行，导致整行匹配失效→重分类失灵、条目重复堆积。
  - 建议：用 re.sub(r'\s+', ' ', cmd).strip() 归一化内部空白后再存；同时挡 \r。

  正则无 ReDoS（线性、无嵌套量词）。写盘路径来自固定 _get_workspace()，command 只进文件内容不进路径，无路径越权。

  ---
  P1 — 性能 / token

  [中] 状态文件无大小上限，无限增长 + 每条命令全量读写
  - 位置：tools.py:_update_agent_state（每次 execute_command 调用，tools.py:110）
  - 问题：只按精确行去重，不同命令永久累积，文件无上限/无轮转。_MAX_STATE=4000
  只截断注入，不截断落盘。长期跑下来每条命令都要全量读+全量重写整个文件，O(file size) I/O；且 4000
  之外的旧条目永远注不进 prompt 却仍参与每次读写。
  - 建议：落盘也设上限（如每 section 保留最近 N 条 / 文件超 8KB 时丢最旧），从源头控制。

  [低] compact thrash 禁用后长任务 token 失控
  - 位置：_maybe_compact_messages thrash 计数（105-116，尾部截断）
  - 问题：连续 max_consecutive(默认 4) 次压缩<15% 即 disabled=True，本任务后续不再 compact。超长连续 context
  在禁用后只能靠 --max-cost 兜底，token O(N²) 重发会很贵。属已知权衡，标注即可。

  _MAX_STATE 头截断保留的是最新条目（新条目插在 section 顶部），合理。execute_command 头尾各 3000 截断：traceback /
  pytest 摘要都在尾部，被保留，策略正确。这两条不算 finding。

  常量取值（soft_limit=120 / token_budget=600K 软提醒 / no_progress 6&12 / gate 8）整体合理，无需调整。

  ---
  P2 — 可维护性（仅标重大坏味道）

  - [低] 重复定义 _get_project_rules：模块级一份（02:82），plan()
  内又嵌套一份（02:97）完全重复，易踩到作用域歧义。删掉嵌套版。
  - [低] 状态文件注入块复制粘贴两份：agent.py:61-68（compact 内）与 agent.py:143-152（solo 启动）逻辑一致，含同一魔法数
  _MAX_STATE=4000 各写一遍。抽 _load_state_for_injection() -> str 一处维护。
  - solo() / _solo_drive() 的职责切分（入口组装+gate vs 纯主循环）合理，不动。

  ---
  总体判断

  不建议直接合入长期使用，有 2 个实际阻塞项必须先修：

  1. test gate 30s 超时（P0
  正确性）——真实项目测试极易超时，会把能过的任务系统性误判为失败并烧满回灌轮次。这是「长期使用」的硬伤，必修。
  2. compact 丢失开场规划（P0 正确性）——与 _SOLO_ROLE 的「plan is your
  anchor」直接冲突，长任务跨文件漂移的根因，必修（pin 规划轮进 head 即可）。

  1 个必须先确认再决定： 并行编排是否共享 workspace。共享则 threading.Lock 是真
  bug（跨进程竞争+非原子写），需文件锁+原子写；独立则可接受。

  其余（正则词边界、状态文件无界增长、\r 注入、重复代码）是改善项，可合入后跟进，不阻塞。重分类核心逻辑、no_progress
  熔断、sentinel/沉默兜底、截断策略经核对均正确，无需改动。

  (M1 tool_call 边界一条标了「待确认」，取决于审范围外的 _split_messages_into_pairs 配对粒度，请一并核一下。)

