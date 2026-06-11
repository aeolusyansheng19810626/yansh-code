# Fable 5 Solo Mode 全量 Review 结果（2026-06-10）

> 提示词见 [./2026-06-10_01-fable5-solo-review-prompt.md](./2026-06-10_01-fable5-solo-review-prompt.md)，源码见 review_src_20260610/。
> 已在真实源码核实：`_update_agent_state` 仅在命令正常结束路径调用（tools.py:479，timeout/拒绝提前 return 不会污染黑名单）；`_split_messages_into_pairs` 中途 system 消息会并入当前 assistant pair，不会破坏配对。

## P0 正确性

### [高] plan_anchor 跨 drive 误捕获 — 把 gate 修复回复当成"开场规划契约"
- 位置：`_solo_drive`（05:52-54），`rounds_used == 1 and plan_anchor is None`
- 问题：`rounds_used` 每次 `_solo_drive` 调用都从 0 起。若**初始 drive 首轮 assistant 是纯 tool_calls（content 为 None/空）**——模型急于动手时常见——anchor 保持 None；之后任意一次 gate 回灌 drive 的首轮若有文本（如"我来分析测试失败原因…"），就被捕获为 anchor，并以「开场规划锚点 — 跨文件接口契约以此为准」名义在每次 compact 作为 system 消息权威注入。错误内容被当成接口契约，可能主动把 agent 带偏。
- 同时回答特别关注#2：首轮纯 tool_calls 时 anchor 为 None → 规划完全不受保护，首次 compact 后规划只存在于 LLM 摘要里（`_SUMMARIZE_SYSTEM` 不保证保留接口签名清单）。
- 建议：在 compact_state 加 `anchor_window_closed` 标志——只允许 solo() 的第一次 drive 捕获（drive 加参数 `capture_anchor: bool`）；初始 drive 首轮无文本时，可在前几轮内继续找第一条非空 assistant 文本，窗口关闭后不再捕获。

### [高] gate 收敛三元组 err_hash 取错端，存在真实误停路径
- 位置：`solo()` gate 循环（05:258-267）
- 问题：md5 取 `(stderr+stdout)[:500]`。pytest 的失败详情在 **stdout 尾部**；前 500 字符是 session header + 进度行，对"哪条断言、什么根因"不敏感。同一批测试失败但根因已变（agent 实质有进展）→ 前 500 字符相同。更糟的是第三元：`snapshot_files_modified()` 是**累计集合**，gate 阶段 agent 修改的几乎总是它早已写过的文件 → `_cur_modified` 在 gate 阶段基本恒定。三元组里两元近似常量，收敛检测退化为 `test_cmd + 弱hash`——第二个 gate 轮就可能被「同错未变化」误停，宣告失败而 agent 其实在收敛。
- 建议：err_hash 改为对 `_parse_pytest_failures()` 的 test-id 集合取 hash（已有现成解析器），fallback 用输出**尾部** 500 字符；modified 元替换为"本 gate 轮内是否有成功写操作"。

### [中] gate 绿但 agent 未重新 task_complete → 误判失败
- 位置：`solo()`（05:276-287）
- 问题：回灌 drive 命中 15 轮 `_drive_limit` 退出（early_exit=False）→ `agent_completed=False`；下一轮循环跑测试**通过** → gate_status="passed" → break。`final_success = agent_completed AND passed` = False。实际产物已修好且针对性测试全绿，却报失败。agent 在 15 轮内"修完但没来得及/没意识到要再 task_complete"是现实路径。
- 建议：gate 测试通过但 agent_completed=False 时，给一次 1-2 轮的确认 drive（回灌"测试已绿，确认完成即 task_complete"）；或语义上接受「曾经 task_complete(success=true) + 最终 gate 绿」为成功。

### [中] agent 显式放弃后 gate 仍持续回灌，纯烧 token
- 位置：`solo()` gate 循环（05:203 起，循环条件不看 agent 意愿）
- 问题：agent `task_complete(success=false, "卡死需人工")` 后，循环照常跑测试→失败→回灌→再 drive，最多 8×15 轮 + 8 次测试。final_success 注定 False，这些轮次全是浪费（token 成本是一等约束）。
- 建议：drive 返回 `early_exit=True and success=False`（显式放弃）时 break 出 gate 循环。初始 drive 即放弃的情形同样适用。

### [低] `_update_agent_state` 末行无换行时精确匹配失效
- 位置：tools.py `_update_agent_state`（07:74）
- 问题：文件被外部编辑/截断致末行无 `\n` 时，`l == entry_line`（带 `\n`）不匹配 → 重分类移除失败 → 同一命令在白/黑两个 section 各留一条，互相矛盾的信息注入 prompt。正常写入路径自身不会产生无换行末行，触发依赖外因。
- 建议：匹配与移除都用 `l.rstrip("\n") == entry_line.rstrip("\n")`。

### [低] sentinel 检查裸索引 `out["result"]`
- 位置：`_solo_drive`（05:65）
- 问题：与 `_get_out_result` 的防御风格不一致，dispatch 返回异常形态时 KeyError 直接炸主循环。
- 建议：改 `(out.get("result") or {}).get("_task_complete")`。

### 审过无须改的点
- **no_progress 按 result 判定**（prompt P0-5）：合理。连续失败写入不算进展是对的——那确实是卡死；6 轮提醒已给出 `task_complete(success=false)` 出口，12 轮熔断是正确兜底。`_get_out_result` 按 id 匹配本身无问题。
- **silent_prompted 每 drive 重置**（P0-6）：合理。每次回灌是新的对话推进，重置最多多花 8 轮追问，可接受。
- **smoke 新建时序**（P0-7）：非问题。gate 在 agent task_complete **之后**才首次运行，agent 新建的 `tests/test_smoke.py` 彼时已落盘，`_force_include_smoke` 能捡到；「新建前判红」路径不存在。

## P0 安全 / 并发

### [中] `\r` 绕过换行过滤，可伪造 agent_state.md section 头
- 位置：tools.py `_update_agent_state`（07:57 只查 `"\n" in cmd_stripped`）
- 问题：含 `\r`（无 `\n`）的命令通过过滤写入文件。下次读取时 `splitlines(keepends=True)` **按 `\r` 也分行**：① 精确行匹配永久失效（写进去一行，读出来两行）；② `l.startswith("## ")` 可被命令中 `\r## 已验证命令（exit=0）` 伪造 section 头，破坏分类；③ 伪造内容随状态文件注入 system prompt 且跨 run 持久。命令文本来自 LLM、LLM 受 repo 内容影响——恶意 repo 存在间接注入链。
- 建议：过滤一切控制字符：`if any(ord(c) < 0x20 for c in cmd_stripped): return`（同时覆盖 `\n`/`\r`/`\t`/ESC 染色序列）。backtick 破坏 markdown code span 属低危（prompt 注入面不变，LLM 读的是原文），可顺带不管。

### [无问题] `_STATE_CMD_RE`
- 无嵌套量词，无 ReDoS。`py\b` 正确匹配 `py.exe`、`py -3.11`（`.`/空格都是边界）；不误匹配 `pytest`（由第三支匹配）/`py2exe`（无边界）。小遗漏：`./venv/bin/python x` 这类路径前缀命令不被记录，白名单覆盖不到，影响小可不修。

### [低] snapshot 首触竞态比自评更糟：可能损坏 baseline，不止"双重备份无损"
- 位置：snapshot.py `_backup_file_if_needed`（09:85-98）
- 问题：`if target.exists(): return` 在锁外，且**锁内 copy2 前不复查**。时序：线程 B 通过 exists 检查 → 线程 A 持锁完成备份、释放、其调用方**写入修改后的 src** → B 进锁，把已修改内容 copy2 覆盖进备份 → baseline 损坏，/revert 还原到错误版本。触发需两个并发 agent 首触同一文件，概率低，但后果是静默数据损坏。
- 建议：锁内 copy2 前补 `if target.exists(): return`，一行修复。

### [无问题] gate 回灌大小
- 每次 ~7.5K chars ≈ 2K token，×8 轮上限 ≈16K token，且旧回灌会被 compact 吞掉。量级合理。

## P1 性能 / token

### [中] thrash disabled 后无降级，长尾 O(N²) 裸奔
- 位置：`_maybe_compact_messages`（03:104-105）
- 问题：disabled 后本任务永不再压，ICA 无 prompt cache，后段每轮全量重发。compact 反复无效通常意味着"近期 pair 本身太大"（如巨型 tool 输出），彻底放弃是最差降级。
- 建议：disabled 时退化为**无 LLM 的硬截断 compact**（保 head + anchor + 最近 1 pair，旧历史直接丢弃换一行占位说明），或把 threshold 抬到 `当前估值×1.5` 继续稀疏压缩。不必复杂，关键是别完全不管。

### [无问题] plan_anchor 注入开销
- anchor 注入后常驻 messages，每轮都发——这正是其目的；~500 token 换规划零漂移，在"跨文件一致性是核心卖点"的前提下划算。不改。

### [无问题] `_update_agent_state` I/O
- 未命中正则早退无 I/O；命中时文件有界（2 section × 20 条 × ≤160 chars ≈ <8KB）。无缓存必要。

### [无问题] gate 15 轮 × 8 次分配
- 8×15 > 120 由 soft_limit 闸住；`max(1, _remaining)` 看似可在预算耗尽时多给 1 轮，但循环顶部 `total_rounds >= _SOLO_SOFT_LIMIT` 先 break，实际不可达。分配合理。15 轮对"断言级修复"够用，对"架构级返工"不够——但那本就该失败。

## P2 可维护性

1. `_trim_section` 手解析：entry 行受正则约束（必以 `- \`py...` 开头）不可能伪造 header——**前提是修掉 \r 漏洞**；header 重复属已损坏文件，只裁第一处可接受。修 \r 后此处不必动。
2. `solo()` ~180 行：建议抽 `_run_gate(messages, tools, compact_state, ...) -> (gate_status, signal)`。不止可读性——上面 err_hash/三态两个 bug 都属于"抽出来就能单测"的逻辑，现在埋在 180 行函数里测不到。
3. disabled 无感知：低优先，有真实需求（thrash 时换策略）再加返回信号，现在不动。

## 特别关注

**1. 无 smoke 时 gate 检测不到 CLI 调用链断裂 — 确认，这是当前最大的假绿来源。**
targeted scope 全部是 agent 自产单测，与实现共享同一套模块假设；`gate_status="passed"` 证明的是"自洽绿"，不是"端到端绿"。`__main__` 参数装配、console_scripts 入口、跨模块 wiring 的断裂，单测结构性测不出。现有机制里 `tests/test_smoke.py` 是唯一出口，但它的存在完全靠运气（项目自带或 agent 自发写）。
建议（二选一，a 更贴现有架构）：
- a) `_SOLO_ROLE` 把「必须写 tests/test_smoke.py（subprocess 跑真实入口）」升级为硬性完成判据；gate 端配合：若 modified 含入口文件（`__main__.py`/`cli.py`/console_scripts）而 smoke 不存在，gate_status 给新值 `no_smoke`（不算 passed），回灌一次要求补 smoke。
- b) gate 加廉价探针：检测到包入口时跑 `python -m <pkg> --help`，非零即回灌。

**2. plan_anchor 空内容路径 — 确认存在，且比 prompt 预想的更糟**：不止"anchor 为空丢规划"，还有跨 drive 误捕获把 gate 修复文本当契约注入（见 P0 正确性第 1 条）。

## 总体判断

架构方向是对的：三态 gate、anchor 重注入、跨 run 环境知识、收敛检测，每一件都解决真问题，且大部分边界（pair 配对、轮次预算、超时早停、原子写）处理细致。**当前不建议直接当长期生产形态**，阻塞项 3 个：

1. plan_anchor 跨 drive 误捕获（P0 高）——会主动注入错误契约，损害核心卖点；
2. err_hash 前缀 + 累计 modified 导致 gate 误停（P0 高）——直接压低 solo 的端到端通过率，且在 AB 数据里表现为"莫名 gate failed"，污染评估；
3. gate 绿但 agent 未重宣告 → 误判失败（P0 中）——同样污染 AB 成功率统计。

另有两个一行修（\r 控制字符过滤、snapshot 锁内复查 exists）建议顺手带上。smoke 缺失的假绿是设计缺口而非代码 bug，需要定方案（推荐 a：role 硬性判据 + gate no_smoke 状态）后单独做。
