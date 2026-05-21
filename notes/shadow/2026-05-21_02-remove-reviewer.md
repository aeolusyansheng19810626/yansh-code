# 2026-05-21 architecture: 砍掉独立 reviewer agent

## 背景

A/B 对比 [list-tools 笔记](./2026-05-21_01-list-tools.md) 后做了第二轮 bug 修复对比，
发现 yansh 在 reviewer 环节死循环——但 coder 第一遍其实就修对了。

进一步追问：**Claude Code 有 reviewer 这一步吗？** 答案是没有。

## Claude Code 的实际架构

```
单 agent（一个 LLM context）
   ↓
混合调用：Read / Grep / Edit / Bash / ...
   ↓
自己跑测试自己看
   ↓
报告完成
```

**关键**：所有信息在同一个对话历史里，agent 自己有完整记忆，自己有质量判断，
不需要把 diff 抠出来塞给"另一个我"做评审。

## yansh 当初为什么搞 reviewer

2023-2024 LLM agent 圈流行 multi-agent 架构（CrewAI/AutoGen/LangGraph）。
"分工协作 = 更专业"听起来很对，所以 yansh 设计成 architect → coder → reviewer → tester 四个角色。

## 实测发现的根本问题

实验任务："修一个被注入的 1 行 bug"。

### 第一轮（有 reviewer）

- 总耗时 272s，35 次工具调用
- coder 第 1 次就修对了，测试通过
- reviewer 拒绝："我看不到测试文件" → 它**没去 read_file**，但 read_file 工具就在它的 schema 里
- review→fix→review→fix... 6 轮上限
- reviewer 第 2 次返回非 JSON，触发 `_extract_json` 失败
- 最终判定**任务失败**（实际代码已经对了）

### 第二轮（删掉 reviewer 循环）

- 总耗时 164s（**-40%**），20 次工具调用（**-43%**）
- 任务**正确判定为成功**
- 但还是慢——剩余瓶颈在 architect 过度规划 + fix 循环

## 学到什么

### 1. Multi-agent 不是免费午餐

每多一个 agent，就多一个 LLM 调用 + 多一个上下文割裂点 + 多一个协议脆弱点。
yansh 的 reviewer 失败的所有原因都是架构层面的：

| 问题 | 根因 |
|---|---|
| "看不到测试文件" | reviewer 没有完整对话历史，缺乏调用工具的内在动机 |
| JSON 输出漂移 | reviewer 必须严格输出 JSON，模型一发挥就崩 |
| 死循环 | 多 agent 协议没有"我做完了"的统一信号 |

这些都不是 prompt 调整能根治的——是**架构错误**。

### 2. Claude Code 的"无聊架构"才是正确答案

单 agent + 工具 + 自我验证。听起来没有 multi-agent 那么"先进"，
但实际效果碾压 4 倍专业分工。

**深层原因**：LLM 的 reasoning 是基于完整 context 的。
人为切断 context（哪怕是为了"专业分工"），损失大于收益。

### 3. 工程文化对照工具设计

人类工程团队的 PR review 是有效的——因为 reviewer 是**真的另一个人**，
有不同视角、不同经验、不同盲区。

LLM 的"reviewer agent" 不是另一个 LLM，是**同一个模型用另一个 prompt**。
没有真正的视角差异。所谓的 review 等同于让 LLM 自己再看一遍——
但 yansh 用 JSON 协议割断了它的上下文，反而**比让它自己看**还差。

### 4. 简化架构的勇气

砍代码比加代码难。
我删了 47 行 review 循环代码 + 4 行注释代替。
yansh 整体行为立刻变好。**减法**比**加法**有时候 ROI 更高。

→ 对应 ROADMAP P1 #5（全局状态重构）的精神：很多复杂度是过度设计的产物。

## 这次保留了什么、删了什么

| 保留 | 原因 |
|---|---|
| `_REVIEWER_ROLE` 字符串 | 没占运行时成本，未来加 `/review` skill 可复用 |
| `review()` 函数 | 集成测试在用；保留作为独立可调用工具 |
| `_parse_review_response` | 单测在用 |
| `_TESTER_ROLE` | fix 循环依然用它分析测试失败 |

| 删除 | 原因 |
|---|---|
| `_run()` 中的 review_attempts 循环 | 架构错误，根本问题 |
| review 失败后弹用户确认 | 用户终止任务的逻辑是 review 派生的，一并去掉 |

## 后续 backlog

剩余 yansh 还慢的原因（按 ROADMAP 对应）：

| 现象 | 根因 | ROADMAP 项 |
|---|---|---|
| architect 计划 2 文件，实际只需 1 | prompt 没收敛"不改清单" | P0 #2（已加但未充分调） |
| fix loop 6 轮硬上限 | 状态机硬编码 | P0 #3（错误恢复闭环） |
| coder 顺手改了不相关测试 | 计划过宽 + 缺乏"任务边界"约束 | P0 #2 |

下一步**最 ROI**：实施 ROADMAP P0 #3（task_complete + token 预算软退出）。
这是"消灭 6 轮硬上限"的根本方案，跟今天砍 reviewer 一脉相承——都是
**让 LLM 自己声明状态**，而不是流程硬编码。

## Claude Code vs yansh 完整对比表

任务："tests/unit/test_tools.py 里 test_replace_in_file_multiple_matches 失败了，修一下"
（注入的 bug：`tools.py:287` 把 `count > 1` 改成 `count == -1`）

两边都用 **claude-sonnet-4-6**。

| 维度 | Claude Code | yansh 改前（有 reviewer） | yansh 改后（无 reviewer） |
|---|---|---|---|
| 耗时 | **30s** | 272s | 164s |
| 工具调用总数 | **5** | 35 | 20 |
| 工具组合 | Bash×3 + Read×1 + Edit×1 | execute×10 / search×9 / read×8 / symdef×3 / replace×3 / list×2 | read×6 / search×5 / execute×4 / symdef×3 / replace×2 |
| 任务结果 | ✅ 完成 | ❌ 失败（代码实际对了） | ✅ 完成 |
| 相对 Claude Code | 1x（基线） | 9.1x slower | **5.5x slower** |

### Claude Code 的工具序列（5 步）

| 步 | 工具 | 关键参数 |
|---|---|---|
| 1 | Bash | `pytest -v` 看具体失败信息 |
| 2 | Read | `tools.py:283-292`（10 行精读） |
| 3 | Edit | `count == -1` → `count > 1` |
| 4 | Bash | `pytest` 验证修复 |
| 5 | Bash | 全测试回归 |

### 改后 yansh 还慢 5.5x 的原因

虽然砍掉 reviewer 解决了"死循环"和"判定错误"，但底层效率瓶颈仍在：

1. **architect 过度规划**：计划要改 2 文件（test + tools），实际只需改 tools
2. **coder 顺手改不相关代码**：把 `for l in lines` 改成 `for line in lines`
3. **fix loop 6 轮硬上限**：即使任务早就修对，循环还在跑
4. **plan/code/test 三阶段串行**：每段都要新起 LLM 调用，相比 Claude Code 单 context 内自然展开多了协议开销

→ ROADMAP P0 #2（prompt 收敛 architect 范围）+ P0 #3（task_complete 软退出）是下一阶段重点。

---

## 三句话总结

1. Claude Code 没有独立 reviewer，是有意为之的架构选择
2. multi-agent 在文档里很美，在生产里**割裂上下文 + 协议脆弱**两个核心问题无解
3. yansh 砍掉 reviewer 后耗时从 272s 降到 164s，还正确收敛了——**减法的胜利**
