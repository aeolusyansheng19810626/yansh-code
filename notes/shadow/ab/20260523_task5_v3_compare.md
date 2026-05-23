# Task #5 v2/v3 验证：4 条 yansh 重构上限改法 → LLM 修完了，框架还差一步

承接 [`./20260523_task5_compare.md`](./20260523_task5_compare.md)。task #5 v1 yansh 翻车 → 实施 4 条 backlog 改法 → v2 跑 → 发现 detector 阈值/edit 策略提示不够 → v3 再调。

## 4 条改法（commit 不在本次还未提交）

### 改 1：5 轮上限 plan-driven 动态调整

`agent.py:code()` 把每文件 `attempts_left = 5` 改成：
```python
attempts_left = max(coder_rounds_per_file, ceil(expected_edits / coder_edits_per_round) + 2)
```

config 加 `coder_rounds_per_file: 5`、`coder_edits_per_round: 3`。tools.py expected_edits=60 时拿到 22 轮（vs 之前硬 5 轮）。

### 改 2：plan() 输出 expected_edits 字段

- `PlanFile` schema 加 `expected_edits: int`
- plan system prompt 加字段说明 + 估算指南（"1 for new file write, 1-3 for tweaks, 5-20 for medium refactor, 30+ for sweeping signature changes; overestimate by 50%"）

### 改 3：_CODER_ROLE + 用户消息双层 edit 策略提示

- `_CODER_ROLE` Tool-call efficiency 节加"Batch dense edits aggressively"——同 pattern 用 `replace_all`，不同 pattern 一轮发多个并行，>20 处用 `write_file` 全文重写
- code() 构造 user_content 时按 expected_edits 加 `【改动规模提示】`：
  - `>= 15` → 推荐 `write_file` 全文重写
  - `5-14` → 一轮多个并行 `replace_in_file`
  - `< 5` → 单点改

### 改 4：fix loop 上限可配置 + 机械错检测

- config 加 `fix_soft_limit: 12`、`fix_mechanical_error_bonus: 12`
- fix() 进入时 regex 扫 error_info：`r"TypeError:.+?missing\s+\d+\s+required\s+(?:positional|keyword)\s+argument"`
- v1 阈值 ≥5（v2 实测没触发因为本次 stderr 只 2 处），调成 ≥1

## v1 → v2 → v3 数据对比

| 维度 | v1（无修法） | v2（改法 1.0，detector ≥5） | v3（改法 1.1，detector ≥1 + edit_strategy_hint） | CC（参考） |
|---|---|---|---|---|
| duration | 499s | 460s | 581s | 294s |
| tool_calls | 130 | 129 | 92 | 54 |
| 总 tokens | 1.85M | **2.95M** ⚠ | 2.14M | 184K |
| sonnet input | 1.05M | 1.61M | 1.92M | 184K |
| haiku input | 778K | 1.35M | 190K | 0 |
| attempts | 3 max | 3 max | 3 max | 1 |
| yansh `test_result` | fail | fail | fail | pass |
| **实际 _err 适配率** | **4/56 (7%)** | 46/56 (82%) | **56/56 (100%)** ✓ | 100% + 辅助函数 |
| replace_in_file 调用 | 7 | 23 | 9 | n/a (用 Edit) |
| **write_file 调用** | 0 | 0 | **2** ✓ | n/a |
| 5 轮警告文件数 | 3 | 3 | 2（agent.py 不再耗尽） | n/a |
| fix scheduler 触发？ | n/a | ✗（阈值太严） | ✗（fix 阶段 stderr 不含 TypeError） | n/a |

## v3 关键转折：LLM 真改完了

**实际状态**：
- tools.py 56 处 `_err` 调用全部加 tool 参数 ✓
- agent.py 4 处 ✓
- subagent.py 1 处 ✓
- test_tools.py 加新单测 + 适配旧 _err 直接调用 ✓
- pytest **5 failed = baseline pre-existing**（test_execute_command_timeout / test_replace_in_file_path_traversal / test_path_traversal_protection / test_move_file_path_traversal / test_build_diff_lines_exactly_50_no_truncation）
- 41 passed（baseline 40 + 新 1 条 test_err_includes_tool_field）
- **0 个 TypeError 缺 tool 参数**

LLM 用了 2 次 `write_file`（小文件 subagent.py、test_tools.py）。tools.py 60 处太大没整文件重写——继续用 22 轮 replace_in_file 改完了。

## 但 yansh 框架仍报 fail

`test_result: fail / attempts: 3 max` 是因为：

1. **fix loop 没识别 baseline 5 failures 是 pre-existing**——`_TESTER_ROLE` 的 Investigation order 第 1 条说"失败符号是否在 plan files 范围"，但本次 plan files 包含 tools.py，而 baseline failures 也在 tools.py 里——LLM 错判为本次回归
2. **fix loop attempt 2/3 LLM 还在反复试 `cd /workspace && pytest`**（task #4 暴露的 docker-style 路径假设老问题）—— 拿不到测试输出
3. fix scheduler detector 没触发：fix 阶段读到的 stderr 都是 path_traversal 类 AssertionError，**没有 TypeError**——detector 设计只针对"signature 改了 + 调用未全适配"的机械错，对当前场景无效

也就是说，**改法 1-3 把"LLM 把 56 处全改完"这件事解锁了**，但 yansh 的"成功判定 + baseline 识别"还有一步没修。

## 这次成功是怎么做到的

观察 v3 的 `replace_in_file=9` vs v2 的 `=23` —— v3 LLM 调用 replace_in_file 反而少了，但**改完了** —— 说明 v3 LLM 用了**更多并行调用**或**整 hunk replace_in_file（一个 old_str 含多处需要改的 _err 调用，包了整段 context）**。

具体看 v3 LLM 的策略：
- 部分 replace_in_file 的 old_str 跨 5-10 行，一次替换里改了多处 _err
- 加上 2 次 write_file 直接整文件刷
- expected_edits 提示让 LLM "心里有数"，知道这是大改动要批量

## 剩余 backlog（写在这）

1. **fix loop baseline failure 识别**：进入 fix() 前，记录修改前的 pytest baseline failures；fix 时对比当前 failures \\ baseline → 只对增量失败 fix；如果增量为空但 fix loop 还要跑，说明全是 pre-existing，直接 task_complete(success=true)
2. **LLM 对 `/workspace` 路径的 docker-style 假设**：plan 阶段 system prompt 注入 `WORKSPACE_DIR` 绝对路径；execute_command 工具 description 说明 yansh 不 chroot 到 /workspace
3. **Coder 阶段"用尽轮次"假警告**：v3 tools.py 实际改完了但还报"已用尽 22 轮"——警告该看 LLM 是否在最后一轮 task_complete(success=true)，若是就不报警告
4. **detector 误报**：当前 detector 只看 TypeError missing argument。可以扩到 NameError / AttributeError 这类"signature/属性改了导致全 caller 挂"的机械错

## token 涨的原因（v2 比 v1 涨 60%）

v1 attempts max 用尽时累计 1.85M tokens；v2 改 1.0 让 Coder 阶段拿到更大的轮次预算（22 轮），每轮 LLM 都重发完整 messages（包含全文 tools.py）→ 22 轮的 input 加权和爆炸到 2.95M。
v3 因为 LLM 用了 write_file 更早结束部分文件 + 整 hunk replace 减少调用次数，token 降到 2.14M。

**潜在优化**：在 Coder 单文件 loop 里做轻量历史压缩（只保留最近 3 轮的工具结果，老的折叠成"已 read tools.py L1-200"）。但这是另一个 P 工作。

## 总结：4 条改法 vs 实际效果

| 改法 | 设计目标 | 实际效果 |
|---|---|---|
| #1 plan-driven 5 轮上限 | 大改动文件不被 5 轮切碎 | ✓ tools.py 拿到 22 轮（vs 5），改完率 100% |
| #2 plan 输出 expected_edits | 调度器有数据可用 | ✓ LLM 估得不算太差（60 / 6 / 2 / 6） |
| #3 edit 策略提示 | LLM 主动用 write_file/replace_all | ✓ 部分（v3 出现 2 次 write_file，0 次 replace_all） |
| #4 fix detector + bonus | TypeError 类机械错追加预算 | ✗ 本次 fix 阶段 stderr 没 TypeError → 没触发 |

**改法 #1-3 解锁了"LLM 把跨文件重构改完"——这是 task #5 v1 翻车的核心症结**。改法 #4 在本次没触发但设计是对的（针对未来"signature 改了，全 caller 没适配，跑测试爆 TypeError"的场景）。

## 数据文件

- `20260523_task5_v2_yansh.json/_stderr.log` — v2 (改法 1.0) 数据
- `20260523_task5_v3_yansh.json/_stderr.log` — **v3 (改法 1.1) 数据，重构 100% 成功**
- v1 数据见 `20260523_task5_compare.md`

## 状态

- ✓ 4 条改法落地（agent.py + config.py 修改未 commit）
- ✓ task #5 v3 LLM 把 56 处 _err 调用全部改对，pytest 实际 5 failed = baseline
- ⚠ yansh 框架的"成功判定" + "baseline 识别"还差一步（写入 backlog）
