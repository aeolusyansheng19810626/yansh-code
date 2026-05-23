# yansh 笔记总览（一个文件读懂全部）

把 `notes/` 下 40 篇笔记浓缩成一份。事件按时间走，数字保留。

---

## 一句话

yansh 是个命令行编程助手，5/21 理流程、5/22 加能力 + 大体检、5/23 跟 Claude Code 子 agent 打 5 场 AB 对比 —— 写代码场景 yansh 慢且费 token（25×），论证场景差距收窄到 4×，跨文件重构靠改框架硬上限才能跑通。

---

## Day 1：5/21 — 把基础流程理顺

这一天主要解决"流程能不能跑通、跑通对不对"。

1. **删了独立 reviewer**（笔记 02）—— 单独的 reviewer 因为看不到上下文反复死循环，删掉后耗时从 272s 降到 164s。结论：单 agent 自检 > 独立 reviewer 拆 context。
2. **修了 dispatch 暗依赖盲区**（笔记 04）—— 在架构师/码农 prompt 里强加一句"改函数签名必须 grep 所有调用点（尤其 dispatch 表）"，yansh 第一次在某个具体维度反超 Claude Code。
3. **加了"先识别归属"机械规则**（笔记 06）—— LLM 老去改 pre-existing 失败相关的代码，加了"这个 assert 引用的符号在不在本次 plan 文件里"判断后，5 个无关失败全部老老实实跳过。commit `34f22ce`。
4. **错误恢复基础设施**（笔记 07-10）—— 加 `task_complete(success, summary)` 工具、token 预算警告、错误标准化（21 个工具 36 处）、fix/audit 软上限提到 12/16，把 task_complete 信号从 fix loop 内部贯通到外层 attempts 循环。场景 B 时长从 31s 降到 2s。commit `7d1b399`。

---

## Day 2：5/22 — 加能力 + 大体检 + 大修

上半天加了一堆功能，下半天拉三家 LLM review 整改。

**前半天加的能力（笔记 11-22，按顺序）：**

- 11：分层符号索引，audit 系统 prompt 注入量 -74.5%
- 12：让新工具"自然被选中"，验证 LLM 不靠强推销也能用
- 13：task_complete 信号持久化进 task_log
- 14：JSON 解析健壮性 + 全局状态重构（Session）+ 沙箱模式
- 15：Plan Mode 状态机
- 16：Skills 系统（项目级 + 全局，frontmatter 触发）
- 17：Skills 加 LLM 语义匹配（关键词不命中时兜底）
- 18：子 agent 派发（dispatch_subagent），独立 messages 不污染父 context，10× token 节省
- 19：子 agent 改并发（ThreadPoolExecutor），3 并发实测加速 2.4×
- 20：MCP 协议接入
- 21：Hooks（4 个事件，3 种动作）
- 22：跨 session 持久记忆（4 种 type，MEMORY.md 索引）—— ROADMAP 收官

**后半天大体检 + 修问题（笔记 23-28）：**

- 23：拉 Gemini / Claude Opus / Codex 三家 review 同一份代码，发现 19 个问题
- 24：修 P0（audit 模式被 general subagent 绕过写文件、项目级配置无 trust 确认存在 RCE）
- 25：修 P1 4 个（mcp 死等、孙进程泄漏、memory 路径穿越、tree-sitter 并发）
- 26：修 P2 5 个（抽 procutil、加 size cap 和锁、stderr_buffer 限长）
- 27：修 P3 测试质量 3 个 + 加 GitHub Actions ubuntu+windows CI
- 28：P4 架构整理（抽 frontmatter / append_active_prompts / 拆 subagent.py），全 ROADMAP 完结

---

## Day 3：5/23 — 跟 Claude Code 子 agent 打 5 场 AB

一份 token 削减计划，加 5 场对比实验。

**Token 削减改造（笔记 2026-05-23_01）：**
- 探测发现 ICA 网关不透传 prompt cache，跳过
- system prompt 全部英文化（末尾加"Always respond in Chinese"）
- fix loop 测试范围精确化（按改动文件推断 test_*.py，不再跑全套）
- read_file 命中检测（thread-local cache）
- 子 agent explorer/auditor 切 haiku
- 翻车教训：英文化削弱了"无关失败早退"的隐性 heuristic，得在 `_TESTER_ROLE` 加反例 few-shot 才恢复

**5 场 AB 结果（yansh vs Claude Code 子 agent）：**

| Task | 类型 | yansh | CC 子 agent | 结论 |
|---|---|---|---|---|
| #1 | 纯探索（查并发条件） | 25s / 2 工具调用 | 22s / 4 工具调用 | 平手；yansh 走符号工具拿到设计意图，CC 走 grep 路径方案更可移植 |
| #2 | 写代码 + 单测 | 254s / 61 / 641K | 72s / 15 / 25K | yansh 慢 25×，因为跑全套测试触发 5 个 pre-existing 失败逐个查 |
| #3 | 纯论证（评估方案） | 730K | 169K | yansh 慢 4×，论证任务优势减弱；CC 抓到 yansh 没提的两个隐藏 trap |
| #4 | bug 修复（路径穿越单测） | 88s / 24 / 249K | 31s / 6 / 63K | 修法字面相同；都漏了 resolve 双校验（没失败信号就停了） |
| #5 v1 | 跨文件重构（64 处调用适配） | 499s / 130 / 1.86M / **fail** | 294s / 54 / 184K / **pass** | yansh 框架硬上限切碎密集修改，56 处只改 4 处（7%） |

**Task #5 后续修了 4 版（v2-v4）：**
- v3 改 plan-driven 动态轮次上限 + expected_edits + edit 策略提示 + 机械错检测追加预算 → LLM 把 56 处全改完，但框架仍判 fail（baseline 误识别 + LLM 假设 docker-style `/workspace` 路径）
- **v4** 加了"baseline pre-existing 失败识别"（commit `1e3ce5f`）→ yansh 第一次 pass，attempts=1，1.80M tokens

---

## 当前位置

- 22 单测全绿 + ubuntu/windows CI
- 全 ROADMAP P0-P4 完结
- 5 场 AB 跑完，跨文件重构终于跑得通
- token 仍是 CC 的 ~10×，主要是没有 prompt cache（ICA 不透传）+ 中文 system prompt + 每轮重发完整 messages

## 还剩的活（按硬度排）

**P1 三条小活（半天能清）：**
1. LLM 对 `/workspace` docker-style 路径假设 —— plan prompt 注入实际 WORKSPACE_DIR（< 2h）
2. Coder "用尽轮次"假警告 —— 已 task_complete 就不报 warning（< 1h）
3. Detector 扩 NameError / AttributeError —— regex 加几条 pattern（< 30min）

**P3 两条（半天能清）：**
4. 5 次 AB 综合 README —— 把 task#1-5 合一张决策矩阵
5. read_cache 命中度量 —— 加一行 log

**P2 一条硬活（1-2 天）：**
6. Coder 单文件循环历史压缩 —— 22 轮每轮重发整文件 messages，是 token 暴涨的大头。难点是 messages 序列结构合法性（tool_use/tool_result 配对）+ 没简单单测路径，得真跑长任务回归。

## 已经做过的（避免重复探讨）

- ✓ Prompt cache 探测过 ICA **未透传**，跳过
- ✓ System prompt 英文化（commit `6d99a70`）
- ✓ Fix loop test scope（commit `6d99a70`）
- ✓ Subagent 切 haiku（commit `a6fad9c`）
- ✓ read_file 命中检测（commit `a6fad9c`）
- ✓ baseline failure 识别（commit `1e3ce5f`，task #5 v4 验证 pass）
