# AB-test 新 task 设计（task6-10）

日期：2026-06-04

## 背景

现有 task1-5 覆盖面太窄：readonly×2、trick×1、bug fix（有 test）×1、多文件重构×1。
设计 5 个新 task 扩大覆盖，3 个中等难度，2 个中高难度。

---

## Task 6（中等）— 从零新建功能，有明确 spec

**类型**：新功能，纯创建，无现有代码参考

**Prompt**：
```
给 yansh 新增 --mode summary 子命令，读取 .yansh/task_logs/ 下最近 10 条
任务记录，用 rich 表格展示：task_id / elapsed_sec / success / cost_usd /
files_modified 数量。无记录时输出"暂无历史任务"。

要求：
- main.py 处理 --mode summary 分支
- task_log.py 加 get_recent_logs(n=10) -> list[dict] 函数
- 加 3 个单测：有记录 / 无记录 / 记录数不足 n
```

**预期改动文件**：main.py、task_log.py、tests/unit/test_task_log.py（或新建）

**难点**：纯新建，LLM 需要理解 task_log 现有数据格式后再设计接口

**成功标准**：test_result=pass，files_modified 包含 main.py + task_log.py

---

## Task 7（中等）— bug fix，无 failing test 引导

**类型**：bug 定位 + 修复，没有 test 直接报错，只有症状描述

**Prompt**：
```
yansh 的 search_in_files 工具在搜索包含正则特殊字符的字符串时会崩溃：
搜索 "config.get(" 会因括号未转义抛 re.error，搜索 "**extra" 同理。
用户只知道报错现象，没有对应单测。

定位根因，修复，加 2 个单测：
- 含括号/点号的搜索串能正常工作（当作字面量搜索）
- 含 .*+? 等其他特殊字符也能正常工作
```

**预期改动文件**：tools.py（search_in_files 函数）、tests/unit/test_tools.py

**难点**：无 test 引导，要靠症状描述定位代码，理解"字面量搜索 vs 正则搜索"的语义选择

**成功标准**：test_result=pass，修复后 search_in_files 对特殊字符做字面量转义

---

## Task 8（中等）— 重构/抽取，行为不变

**类型**：代码重构，不改行为，只改结构

**Prompt**：
```
agent.py 里的 auto-compact 相关代码（_compact_messages /
_estimate_messages_tokens 函数，以及 code() 内的 _compact_disabled /
_compact_consecutive_over 状态变量，共约 80 行）目前散落在模块级和函数内。

把这部分逻辑抽取到新文件 compact.py：
- 暴露 CompactState dataclass（包含 disabled / consecutive_over 字段）
- 暴露 estimate_tokens(msgs) -> int 函数
- 暴露 compact_messages(msgs, keep_recent_pairs=2) -> list 函数
- 暴露 maybe_compact(msgs, state, threshold, max_consecutive, console) -> list 函数

agent.py 改成从 compact.py import 并使用 CompactState。
确保所有现有单测通过，不引入任何行为变化。
```

**预期改动文件**：compact.py（新建）、agent.py

**难点**：行为不变的重构，import 顺序、状态管理、circular import 风险，容易遗漏边界情况

**成功标准**：test_result=pass，新建 compact.py，agent.py import 正确

---

## Task 9（中高）— 跨文件新子系统，多处协同

**类型**：横跨 4 个模块的新功能，有明确 spec 但集成点多

**Prompt**：
```
给 yansh 加 token budget 上限功能：
1. CLI 加 --budget <K> 参数（如 --budget 500 表示上限 500K tokens）
2. llm_client.py 在每次 call_llm 后检查 session 累计，
   超限时 raise BudgetExceededError（自定义异常）
3. agent.py 的 _run() 捕获此异常，task_log 写入
   success=False + summary="token 预算超限中断"，然后正常退出（不 crash）
4. JSON 输出加 budget_exceeded: true 字段
5. 加 3 个单测：正常不触发 / 超限中断 / task_log 收尾正确
```

**预期改动文件**：main.py、llm_client.py、agent.py、task_log.py

**难点**：4 个模块协同，异常传播路径复杂，遗漏任一模块就会功能不完整

**成功标准**：test_result=pass，4 个文件都在 files_modified 里

---

## Task 10（中高）— 需求模糊，需要 LLM 做设计决策

**类型**：spec 故意留白，LLM 需要自己做设计决策

**Prompt**：
```
yansh 对"任务完成"的判断只看 test_result，但文档生成、配置修改等无测试任务
的 test_result 永远是 skip，质量无法判断。

设计并实现一个"无测试任务自检机制"：
当 test_command 为空或测试 skip 时，用 LLM 对 files_modified 的实际改动
做简短自检（不超过 3 个工具调用），判断改动是否符合 requirement，
输出 self_review: pass/fail/skip 到 task_log 的 JSON 结果。

约束：自检 token 消耗 < 20K，不能修改 workspace 文件。
```

**预期改动文件**：agent.py（主要），task_log.py

**难点**：spec 故意模糊（用什么模型？怎么构造 prompt？），LLM 需要自行设计，不同决策导致很不一样的实现质量

**成功标准**：test_result=pass（或 skip），task_log JSON 里有 self_review 字段

---

## Workspace 准备说明

task6/8/9/10：新功能，workspace 直接用 HEAD（dc51541）即可，功能不存在。
task7：bug 存在于当前 HEAD，workspace 同样用 HEAD。

workspace 初始化命令（每个 task 跑前执行）：
```powershell
git -C yansh-code worktree add --detach AB-test/yansh/taskN_ws HEAD
```

task7 不需要注入 bug（search_in_files 的正则问题是真实存在的）。
