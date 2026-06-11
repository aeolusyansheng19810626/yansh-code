# 给 Fable 5 的独立分析提示词：sonnet 在 solo agent 上为何烧满轮次

> 在本地 CC（模型选 Fable 5）里把以下内容作为提示词。你能直接读本仓库代码和 log，
> 凡是「位置指引」处请自己打开核对，不要只信我下面的转述。

---

## 背景与任务

本仓库（yansh）是一个自研 AI coding agent 框架。`solo` 模式 = 单一连续 context 的端到端
agent（自主规划→读写文件→跑命令→自测→收尾），对标 Claude Code。

同一任务（实现 6000 行级内存 SQL 引擎 miniQL，多模块 + 要求 pytest 测试），用两个模型各跑一次，
**框架代码相同、system prompt 相同、干净工作目录**：

- **opus-4.8**：51 轮（67 次工具调用）干净完成，主动建全套测试，`success=true`，黑盒 10/10，613s，$36.6
- **sonnet-4.6**：烧满 120 轮上限**从未主动 task_complete**，`success=false`，黑盒其实 9/10（功能基本对），758s，$12

我的判断：sonnet-4.6 是第一梯队编码模型，miniQL 不算高复杂任务，**它不该做不到，应是框架有优化空间**。
请你独立分析：**根因是什么？框架层面最该改哪里，让 sonnet 这类模型也可靠？** 不要停在"换 opus"，我要框架优化方向。

## 你可以直接读的材料（请自己打开）

- **两份完整运行日志**（每份末尾一行是含 `tool_calls` / `tokens` / `cost` 的大 JSON）：
  - sonnet：`C:/Users/ShengYan/Projects/AB-test/longrun-miniql-exp1-gatev2/run.log`
  - opus：`C:/Users/ShengYan/Projects/AB-test/longrun-miniql-exp3-opus/run.log`
  - 两个 workspace 目录本身（agent 产出的 miniql 包、tests/、根目录草稿脚本）也在那两个路径下，可对比。
- **核心代码**（agent.py / tools.py）：
  - `_SOLO_ROLE`（system prompt 本体）：agent.py:2344
  - `solo()` 启动与注入（system 组装、状态文件注入）：agent.py:4220 附近
  - `_solo_drive()` 主循环 + 轮次/无进展熔断 + gate 回灌
  - `execute_command`：tools.py:372，注意 `subprocess.Popen(..., shell=True, cwd=_get_workspace())`
  - 状态文件机制 `.yansh/agent_state.md` 的生成/注入逻辑（grep `agent_state`）
- **本轮已有结论**（我的，供对照，不要全信，请独立判断）：
  - `notes/shadow/2026-06-11_01-exp1-v2-result.md`（实验1：enforcement 硬卡被旁路）
  - `notes/shadow/2026-06-11_02-exp3-opus-result.md`（实验3：opus vs sonnet 对比）

## 我已做的加工结论（你自己从 log 不一定想立刻复现，先给你）

**工具调用归因**（同任务）：

| sonnet (120 calls) | n | opus (67 calls) | n |
|---|---|---|---|
| **环境/解释器/编码探路** | **50** | 命令行跑真实入口验证 | 24 |
| read_file 定位 | 21 | 写实现模块 | 14 |
| 写实现模块 | 14 | replace_in_file | 7 |
| search_in_files | 9 | 写正规 tests/ | 5 |
| 写根目录草稿脚本 | 8 | delete_file 等 | 9 |
| 目录摸索 / replace | 14 | 目录摸索 | **1** |

- 两模型「写实现模块」都是 14 次（实现工作量相同）。差距全在别处。
- sonnet **50 次（42%）烧在环境/解释器/编码探路**，opus ≈ 0。

**sonnet 灾难开局**（命令序列前 22 条要点，请去 log 核对全貌）：
- 轮1 起反复 `cd /workspace && ...`——但 `/workspace` 是它脑补的占位路径，**根本不存在**（真实 ws 是
  `/c/Users/ShengYan/Projects/AB-test/longrun-miniql-exp1-gatev2`）。`&&` 让 cd 失败后命令在错误 cwd 静默跑。
- 误以为代码坏 → 轮4 切 `python3`（Linux 习惯，Windows 上行为异常）→ 轮8 试 `PYTHONUTF8=1` →
  轮9-16 逐个 `py_compile` 排查 + `pwd && ls` 找自己在哪 → **直到轮22 才用完整绝对路径 cd**。

**opus 开局**：轮1 即 `cd /workspace 2>/dev/null; python -c ...`（用 `;`+`2>/dev/null` 容错、用 `python` 不是
`python3`），之后全程 `python -m miniql ...` 零探路。

**框架真相**：`execute_command` 已经 `cwd=_get_workspace()`，**agent 根本不需要 cd**。但 `_SOLO_ROLE`
全程没告诉它：①命令已在项目根、无需 cd；②用哪个解释器；③Windows 控制台编码要 UTF-8。状态文件
`.yansh/agent_state.md` 有环境先验，但**干净 ws 首跑时是空的**（历史：有先验时探路 28→7 轮）。

## 请回答（结构化、可落地，别泛泛）

1. **根因排序**：按对总轮次的贡献排序 sonnet 的根因。哪些框架可消除（环境/契约/提示词），哪些是模型固有？
2. **opus 为何不踩**：它的容错/用 python/批量验证/及时收尾，是"能力"还是"习惯先验"？框架能否显式注入给 sonnet？
3. **最高杠杆 3 处改动**：只许改 3 处会改哪？具体到"prompt 哪句 / 加什么探测 / 改 execute_command 什么行为"。
4. **确定性 vs 概率性**：对"环境探路"，哪些改动是确定性的（命令改写/自动注入），哪些仍是概率性的（靠 prompt）？
5. **收尾问题**：sonnet"从不 task_complete、烧满轮次"是独立于环境探路的另一根因吗？框架如何让"不知何时算够"的模型可靠收尾，又不误杀仍在正常工作的 agent？
6. **盲点**：读完代码和 log，有没有我没意识到、可能是更深根因的东西？

请独立得出结论，与我上面的加工结论冲突时直接指出。
