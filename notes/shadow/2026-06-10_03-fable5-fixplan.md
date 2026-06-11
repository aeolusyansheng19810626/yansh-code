# Fable 5 Review 核实结论 + 修复清单（第四轮 review）

> Fable 5 原始结果见 ./2026-06-10_02-fable5-solo-review-result.md，提示词 ./2026-06-10_01-fable5-solo-review-prompt.md。
> 本文 = opus 对照当前 HEAD 源码逐条核实后的可执行修复清单。已 merge 的代码在 main（领先 origin 3 commits）。

## 核实结论：Fable 5 零误读，发现 3 个「修复引入的回归」

前三轮（deepseek/gemini/gpt/opus）审的是原始 solo 代码；Fable 5 审的是 P0/P1/P2 修完后的代码，抓出 3 个我们自己修复引入的回归（#1#2#3），其中 #2#3 会污染后续 AB 评估数据（表现为"莫名 gate failed / 成功率被压低"）。

## 确认的真 bug（修复方案已定，按优先级）

### #1 [P0高] plan_anchor 跨 drive 误捕获 —— P0-3 方案缺陷
- **位置**：`agent.py` `_solo_drive` 首轮捕获处（约 4040-4045，`if rounds_used == 1 and compact_state.get("plan_anchor") is None`）
- **问题**：`rounds_used` 每次 `_solo_drive` 调用从 0 起。初始 drive 首轮若纯 tool_calls（content 空）→ anchor=None；之后任意 gate 回灌 drive 的首轮文本（"我来分析测试失败…"）被当成「开场规划锚点—跨文件接口契约」注入每次 compact，主动带偏 agent。
- **修复**：`_solo_drive` 加参数 `capture_anchor: bool = False`。只有 `solo()` 的**第一次** drive 调用传 `capture_anchor=True`，gate 回灌的调用不传（默认 False，永不捕获）。捕获条件去掉 `rounds_used==1` 限制，改为「初始 drive 期间任意一轮，只要 anchor 仍为 None 且本轮 msg.content 非空就捕获」——解决首轮纯 tool_calls 时 anchor 丢失。

### #2 [P0高] gate 收敛三元组退化致误停 —— P1-10 方案缺陷
- **位置**：`agent.py` `solo()` gate 循环（约 4247-4257，`_cur_err_hash` / `_cur_modified` / `_cur_gate_key`）
- **问题**：① err_hash 取 `(stderr+stdout)[:500]` 是**前缀**，pytest 失败详情在**尾部**，前 500 是 session header，根因变了 hash 不变；② `_cur_modified = snapshot_files_modified()` 是**累计集合**，gate 阶段改的都是早写过的文件 → 基本恒定。两元近似常量 → 第二个 gate 轮就可能被「同错未变化」误停，agent 其实在收敛。
- **修复**：① err_hash 改为对 `_parse_pytest_failures(stdout+stderr)` 的 test-id 集合取 hash（agent.py 已有该解析器），解析空时 fallback 用输出**尾部** 500 字符；② 第三元从「累计 modified」改为「本 gate 轮内是否有新增成功写」——在回灌 drive 前记录 `prev_modified = set(snapshot_files_modified())`，drive 后 `new_writes = set(snapshot_files_modified()) - prev_modified`，把 `bool(new_writes)` 纳入收敛判定。收敛条件：连续两轮（test-id 集合 hash 相同 AND 本轮无新增写）才停。

### #3 [P0中] gate 绿但 agent 未重宣告 → 误判失败 —— P0-1×P1-9 交互
- **位置**：`agent.py` `solo()` gate 循环 + 收尾（约 4271 `agent_completed=...`、4277 `final_success`）
- **问题**：回灌 drive 撞 15 轮 `_drive_limit` 退出（early_exit=False）→ agent_completed=False；下一轮 gate 测试**通过** → gate_status="passed" → break；`final = agent_completed AND passed` = False。产物已修好、针对性测试全绿，却报失败。
- **修复**：gate 测试通过（gate_status 将置 passed）但当前 `agent_completed=False` 时，先给一次**确认 drive**（回灌一条 user 消息："针对性测试已全绿，如确认完成请立即 task_complete(success=true)"，soft_limit 给 `total_rounds + 2`），drive 后重新计算 agent_completed，再决定 final。避免"修完没来得及重宣告"被误杀。注意确认 drive 也受总轮次 120 兜底。

### #4 [P0安全中] `\r` 绕过换行过滤伪造 section 头
- **位置**：`tools.py` `_update_agent_state`（约第 57 行 `if "\n" in cmd_stripped or len(cmd_stripped) > 160: return`）
- **问题**：含 `\r`（无 `\n`）的命令通过过滤；写盘后 `splitlines(keepends=True)` 按 `\r` 也分行 → 精确行匹配永久失效 + 可用 `\r## 已验证命令（exit=0）` 伪造 section 头破坏分类 + 跨 run 注入 system prompt。
- **修复**：改为过滤所有控制字符：`if any(ord(c) < 0x20 for c in cmd_stripped) or len(cmd_stripped) > 160: return`（覆盖 \n/\r/\t/ESC 染色序列）。

### #5 [P0安全低] snapshot 锁内未复查 exists → baseline 损坏
- **位置**：`snapshot.py` `_backup_file_if_needed`（约 85-98，锁内 `shutil.copy2` 前）
- **问题**：`if target.exists(): return` 在锁外。时序：B 通过 exists 检查 → A 持锁备份+其调用方写入修改后的 src → B 进锁把已修改内容 copy2 覆盖进备份 → /revert 还原到错误版本（静默数据损坏，非"双重备份无损"）。
- **修复**：锁内 `shutil.copy2` 之前补一行 `if target.exists(): return`（双重检查锁定）。

## 设计缺口（非 bug，需先定方案再做，不在本轮 sonnet 范围）

**无 `tests/test_smoke.py` 时 gate 测不到跨文件 CLI 调用链断裂**——当前最大假绿来源。targeted scope 全是 agent 自产单测，与实现共享同一错误假设；`__main__`/console_scripts/参数装配断裂单测结构性测不出，smoke 是唯一出口但其存在靠运气。
- 方案 a（推荐，贴现有架构）：`_SOLO_ROLE` 把「必须写 tests/test_smoke.py（subprocess 跑真实入口）」升为硬性完成判据；gate 配合：modified 含入口文件（`__main__.py`/`cli.py`/console_scripts）而 smoke 不存在时，gate_status 给新值 `no_smoke`（不算 passed），回灌一次要求补 smoke。
- 方案 b：gate 检测到包入口时跑 `python -m <pkg> --help`，非零即回灌。
- **待用户拍板 a/b 后单独做。**

## Fable 5 质量画像（供选型）

- 综合最强，可与 opus-4.8 比肩，**回归检测维度更强**——专盯"多个修复点叠加产生的新问题"，单看单函数发现不了（3 个高/中 P0 都是这类）。
- 严谨度对标 opus：主动核实 prompt 断言、纠正 prompt 错误假设（smoke 时序）、8 个"无须改"点全给裁决理由。
- 产品级洞察：指出 #2#3 会污染 AB 评估数据。
- 选型：gpt=控制流真实 bug；opus=跨文件链+裁决；fable5=opus 级跨文件链 + 专盯修复回归 + 评估污染视角。

## 待办

派 sonnet 修 #1-#5 + 测试用例（每条配回归用例：#1 验 gate drive 不捕获 anchor、#2 验根因变化时不误停 + test-id hash、#3 验 gate 绿+未重宣告时补确认 drive、#4 验 \r 命令被拒、#5 验并发首触不覆盖 baseline）→ opus review → 合入。
#6 no_smoke 待定方案。
