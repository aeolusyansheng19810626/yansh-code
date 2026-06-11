# Solo Mode 全量代码 Review 提示词（给 Fable 5）

> 用途：把下面「提示词正文」整段复制给 Fable 5，连同附带源码文件一起提供，做一次对 solo mode **当前完整代码**的全面 review。
> 源码参见 notes/shadow/review_src_20260610/。

---

## 提示词正文（复制以下全部）

你是一名顶级 Python 工程师，受邀对一个 AI coding agent 框架的 **solo mode** 子系统做一次完整、严格的 review。请基于我提供的所有源码文件综合判断，不要臆测未给出的实现。

### 背景

这是一个命令行 AI coding agent（类 Claude Code），接受自然语言需求，自主规划→读写文件→执行命令→自测，端到端完成多文件编程任务。底层 LLM 经由 IBM ICA 网关调用，**该网关不透传 prompt cache**，长对话的 input token 是 O(N²) 全额重发——**token 成本是一等约束**，所有设计决策都围绕此展开。

**solo mode** 是新增的「单一连续 context」端到端模式，对比旧架构（逐文件重建 context）的核心优势是跨文件接口一致性天然成立。系统由以下几部分组成：

**① 主循环**（`_solo_drive` + `solo`）：
- `solo()` 组装 system prompt（role + 项目规则 + workspace 符号索引），建空快照，启动主 loop。
- `_solo_drive()` 是实际 LLM 驱动循环：每轮 call_llm → compact → 分发工具调用 → 检测 task_complete sentinel → no_progress 熔断 → 沉默兜底。
- `no_progress` 熔断：连续 `_SOLO_NO_PROGRESS_CAP=6` 轮无写编辑/无命令运行先注入提醒，`2×CAP=12` 轮熔断。判定依据为 dispatch result（写工具需有 `success`，execute_command 排除 returncode=-1 的超时/拒绝）。

**② 外部 test gate**（`solo()` 中的 while gate 循环）：
- agent `task_complete` 后，框架推断 scope → 跑测试 → 失败则把回灌内容 append 进**同一条 messages** 继续驱动修复（最多 `_SOLO_GATE_MAX_ROUNDS=8` 轮）。
- gate 引入三态结果：`agent_completed`（agent 主动 task_complete(success=true)）× `gate_status`（passed/failed/no_command/coverage_unknown）。
- 最终 `success = agent_completed AND gate_status=="passed"`（全量兜底绿 → `coverage_unknown`，不算成功）。
- gate 每次回灌限 `_SOLO_GATE_DRIVE_LIMIT=15` 轮；连续 2 轮超时且错误相同则早停；连续 2 轮(test_cmd, err_hash, modified_files)三元组不变则提前失败退出。
- 失败分类 `_classify_test_failure`：timeout(rc=-1) / uncollectable(rc=2/ImportError) / assertion(rc=1)，回灌时附分类和处置提示。
- 回灌内容 `_build_gate_feedback`：STDOUT 和 STDERR 双通道都给（不二选一），各自保头尾截断。

**③ compact 压缩**（`_compact_messages` + `_maybe_compact_messages`）：
- token 超 40K 阈值时触发：保留 `[system, user_initial]` head + plan_anchor 锚点（首轮 assistant 规划文本，最多 2000 字，每次 compact 重注入为 system 消息）+ summary_system + 最近 2 个 pair 原文。
- thrashing 保护：连续 4 次压缩<15% 则 disabled，不再 compact。
- 摘要由 `_summarize_old_history` 调 LLM 完成；`_SUMMARIZE_SYSTEM` 强制保留改动文件名+函数、已验证 shell 命令。

**④ 框架环境知识文件**（`tools.py` 的 `_update_agent_state`）：
- 每次 `execute_command` 后，按 exit code 把 python/pytest 类命令写入 `.yansh/agent_state.md` 的白/黑名单，跨 run 复用。
- 支持重分类（先失败后成功 / 先成功后失败），精确行匹配移除旧条目再追加到正确 section。
- 每 section 限 20 条，多余的从底部裁剪；写盘用 tmp + `os.replace` 原子写。
- 任务启动和每次 compact 时注入 system prompt（截断 4000 chars）。

**⑤ snapshot 并发安全**（`snapshot.py` 的 `_backup_file_if_needed`）：
- 增量备份：每个文件只在首次触碰时备份（first-touch-only），meta.json 读-改-写整段加 `_SNAPSHOT_META_LOCK` + `_atomic_write`。

### 待审范围（已附源码文件，请全部阅读）

| 文件 | 符号 | 当前行号 | 关注点 |
|---|---|---|---|
| `agent.py` | 5 个 solo 常量 | 138-142 | SOFT_LIMIT=120 / TOKEN_BUDGET=600K / NO_PROGRESS_CAP=6 / GATE_MAX=8 / GATE_DRIVE_LIMIT=15 |
| `agent.py` | `_SOLO_ROLE` | 2263 | agent system role 提示词，与实现是否一致 |
| `agent.py` | `_compact_messages()` | 1360 | plan_anchor 注入、pair 配对、summary 结构 |
| `agent.py` | `_make_compact_state()` | 1448 | compact 状态初始化，plan_anchor 字段 |
| `agent.py` | `_maybe_compact_messages()` | 1459 | compact 触发条件、thrashing 保护、plan_anchor 透传 |
| `agent.py` | `_force_include_smoke()` | 1557 | smoke test 强并入 helper（plan/solo 两路共用） |
| `agent.py` | `_get_out_result()` | 3991 | 按 id 从 dispatch outs 取 result，no_progress 用 |
| `agent.py` | `_solo_drive()` | 3999 | 主驱动循环全文 |
| `agent.py` | `solo()` | 4114 | 主入口全文（含 gate 循环） |
| `agent.py` | `test()` + `judge()` | 4291-4305 | 测试执行与判定 |
| `agent.py` | `_classify_test_failure()` | 4307 | 失败三分类 |
| `agent.py` | `_clip()` | 4318 | 保头尾截断 |
| `agent.py` | `_build_gate_feedback()` | 4325 | 回灌内容构造 |
| `tools.py` | `_STATE_CMD_RE` / `_STATE_FILE_LOCK` / `_STATE_SECTION_LIMIT` | 28-30 | 正则、锁、section 上限常量 |
| `tools.py` | `_trim_section()` | 33 | section 裁剪 helper |
| `tools.py` | `_update_agent_state()` | 51 | 环境知识写盘全文 |
| `tools.py` | `execute_command()` | ~355 | 命令执行 + 截断 + 调 _update_agent_state |
| `snapshot.py` | `_SNAPSHOT_META_LOCK` + `_atomic_write()` | 19-26 | 锁和原子写 helper |
| `snapshot.py` | `_backup_file_if_needed()` | ~64 | first-touch 备份 + meta 并发安全 |

### Review 维度（请分维度组织输出，高优先）

**P0 — 正确性**

1. **gate 三态逻辑**：`agent_completed × gate_status` 的所有组合下，`final_success` 的值是否与"任务真实完成"语义吻合？是否有逻辑短路（如 gate 绿但 agent 未完成被误判为成功，或 agent 完成但 gate 无法运行被一律判为失败是否合理）？
2. **compact plan_anchor 持久性**：多次 compact 后 plan_anchor 是否仍能可靠注入？`_solo_drive` 首轮捕获的时机是否正确（首轮 LLM 回复有时含工具调用无纯文本，anchor 会不会为空）？
3. **`_update_agent_state` 重分类正确性**：同一命令先失败后成功（反向亦然），精确行匹配移除旧条目再追加，是否能正确处理？文件末尾无换行符时 `splitlines(keepends=True)` 能否精确匹配 `entry_line`（带 `\n`）？
4. **gate 收敛检测（三元组）**：err_hash 取前 500 字节，modified 用 snapshot 当时的快照——是否存在「agent 实际改了文件但 hash/modified 未变化」导致误停的路径？
5. **no_progress 按 result 判定**：`_get_out_result` 按 `tc.id` 匹配，`outs` 顺序和 id 是否一一对应？失败写工具不算进展、超时命令不算进展——是否会误杀"agent 尝试写但文件被锁/路径越界"这类非空转场景？
6. **沉默退出兜底**：`silent_prompted` 在 `_solo_drive` 每次调用时重置（本次 drive 内只追问一次）——gate 回灌多次调用 drive 时，每轮都重置是否合理？
7. **gate scope 与 smoke**：`_force_include_smoke` 只在 `tests/test_smoke.py` 实际存在时才并入——若 smoke 文件不存在、agent 任务要求新建 smoke test，gate 会在 agent 新建前就判红并回灌。是否需要考虑"本轮 modified 含 smoke 文件新建"的路径？

**P0 — 安全 / 并发**

1. **prompt injection**：`agent_state.md` 内容（含 LLM 产出的命令原文）直接拼入 system prompt。现有过滤：换行符跳过、>160 字符跳过、原子写保内容完整。但命令内含 backtick、markdown 标题（`## `）、控制字符（如 `\r`）是否仍有注入面？`_trim_section` 按 `\n## ` 切分 section，命令含 `\n## ` 会不会破坏文件结构？
2. **`_STATE_CMD_RE` 正则**：当前 `^\s*(py\b|python[0-9.]*\b|pytest\b)`——有无 ReDoS 风险？`py\b` 能否正确匹配 Windows 的 `py.exe` 或 `py -3.11` 形式？
3. **snapshot 并发**：`_SNAPSHOT_META_LOCK` 是线程锁，`_backup_file_if_needed` 中 `if target.exists(): return` 是提前 return（锁外）——两个线程同时首触同一文件，竞态窗口是 `target.exists()` 到 `with lock` 之间，可能导致双重备份（shutil.copy2 覆盖，无损）或 meta 漏记。这是否可接受？
4. **gate 回灌 STDOUT/STDERR 大小**：`_clip(head=1500, tail=2000)` 每通道 3500 chars，两通道合计 7000 chars，加提示文本一次回灌约 7500 chars。solo context 已 compact 过，这个量是否在合理范围？

**P1 — 性能 / token**

1. **compact thrashing 后 token 失控**：disabled 后 messages 只靠 600K 软提醒兜底，ICA 不透传 prompt cache，长任务后段会重发全量 context。disabled 策略是否合理，或有更好的降级方案？
2. **plan_anchor 注入开销**：每次 compact 都把规划文本（最多 2000 字 ≈ 500 token）重注入。若任务跑 20 次 compact，额外多发 10K token。这个权衡是否值得？
3. **`_update_agent_state` I/O**：每次 execute_command 都读+写状态文件（即使命令未命中正则，早 return 前没有读文件，这点 OK）。命中时读-改-写，文件上限 20 条/section，实际大小有界——是否有内存缓存优化的必要？
4. **gate 每次 drive 限 15 轮**：总 120 轮，gate 最多 8 次回灌，理论上 gate 阶段最多消耗 120 轮（8×15 > 120，受 total_rounds 上限兜底）。这个分配是否合理？能否让 agent 在有限 gate 轮次内有效修复？

**P2 — 可维护性（只标重大坏味道）**

1. `_trim_section` 用字符串 `index` + `re.search` 手解析 markdown，是否健壮？若 section_header 出现多次（虽然逻辑上不应该）如何处理？
2. `solo()` 函数约 180 行（含 gate 循环），gate 循环本身 ~80 行——是否值得抽 `_run_gate()` helper？
3. compact thrash disabled 后，`_maybe_compact_messages` 直接 return 原 msgs——调用方（`_solo_drive`）无感知，若后续需要"thrash 时改变策略"会比较难挂。

### 特别关注（最高价值）

以下两点是本系统最容易产生「单测全绿但端到端崩」的位置，请重点审：

1. **gate_status=coverage_unknown 的语义**：agent 认真写了单测 → scope 命中 → `_detect_python_test_cmd(scope=非空)` 返回精准命令 → gate_status=passed。但这些单测和 agent 共享同样错误的模块假设——与「CLI 入口 / `__main__` 参数装配」相关的断裂单测是测不出来的。smoke test 是唯一出口。请评估：若项目没有 `tests/test_smoke.py`，solo gate 能否检测到跨文件 CLI 调用链断裂？
2. **plan_anchor 内容的信息密度**：`compact_state["plan_anchor"] = msg.content[:2000]`，首轮 assistant 回复截前 2000 字。若 agent 首轮大量输出工具调用（write_file 多个文件），`msg.content` 可能是空字符串（tool_calls 消息无 content）或极短——anchor 实际存的是什么？这个路径下 compact 后规划文本是否还能保留？

### 输出格式

按 `P0 正确性 / P0 安全并发 / P1 性能token / P2 可维护性` 四节组织。每条 finding：

- **[严重度 高/中/低] 一句话标题**
- 位置：`文件:行号` 或函数名
- 问题：触发条件和后果
- 建议：最小修复方向（不必给完整代码）

最后给**总体判断**：当前代码状态是否适合长期生产使用，有哪些必须先修的阻塞项。请只报有把握的问题，不确定的标「待确认」，不要凑数。

---


