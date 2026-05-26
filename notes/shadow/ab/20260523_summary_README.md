---
date: 2026-05-23
tasks: 5
subjects: [cc, yansh, yscode]
---

# AB Test 综合报告：cc / yansh / yscode — 5 次实测决策矩阵

**测试轮次**：两轮——2026-05-23（cc vs yansh，双方对比）+ 2026-05-25（三方对比，code mode 统一）
**模型**：Claude Sonnet 4.6（三方一致）
**任务数**：5（探索 / 写代码 / 写文档 / bug 修复 / 跨文件重构）

---

## 一、决策矩阵

> 单元格格式：符号 + 一句关键观察。✓ = 完成且干净；⚠ = 完成但有副作用/条件；✗ = 失败或根本未完成

| 工具 / 任务类型      | 探索（task #1）                             | 写代码（task #2）                                  | 写文档（task #3）                               | bug 修复（task #4）                              | 跨文件重构（task #5）                           |
|-----------------|------------------------------------------|-------------------------------------------------|----------------------------------------------|-----------------------------------------------|---------------------------------------------|
| **cc**          | ✓ 50K，4 工具，直接命中，最省               | ✓ 70K，正确识别已实现，self-report 字段误报      | ✓ 128K，论证合理，hidden trap 捕捉准          | ✓ 51K，1 行精准修复，性价比之王                 | ✓ 132K，64 工具一次跑通，稳健完成              |
| **yansh**       | ⚠ 167K，答对但顺手删无关 import（code mode 过度执行）| ✗ 915K，coder 已识别"无需修改"但 plan 不放手，烧 91 万 token 改范围外 | ⚠ 44K，凭脑子写文档零探索，1 错 2 偏          | ✗ 205K，baseline 识别机制误吞用户请求，静默失败 | ⚠ 692K，修改正确但 latent bug 触发 CLI crash  |
| **yscode**      | ⚠ 65K，答案塞 stderr 不上 summary，空 plan 软着陆 | ✓ 56K，最省且正确识别已实现，架构胜出            | ⚠ 426K，穷举式 read 探索，论证完整但昂贵      | ⚠ 592K / $1.85，修对但创建 4 个无关 shim 文件  | ✗ 789K / $2.58，cp932 编码错致 PlanFailed     |

**三方完成率**：cc 5/5 ✓ / yscode 3/5（#1⚠ #3⚠）/ yansh 2/5（#1⚠ #3⚠，#2 #4 #5 失败）

---

## 二、token / 时间 / 通过率核心数据

### Task #1 探索（找函数 + 行号）

| 维度        | cc      | yansh   | yscode  |
|-----------|---------|---------|---------|
| 用时        | ~8s     | 79.6s   | 47.9s   |
| tokens    | ~50K    | 167K    | 65K     |
| 工具调用数    | 4       | 7       | 5       |
| 答案正确     | ✓       | ✓       | ⚠ 在 stderr |
| 文件改动     | 无       | agent.py（无关 import 删除）| 无       |

### Task #2 写代码（trick：baseline 已实现）

| 维度        | cc      | yansh   | yscode  |
|-----------|---------|---------|---------|
| 用时        | 38.5s   | 189.9s  | 143.0s  |
| tokens    | ~70K    | 915K    | 57K     |
| 识别"已实现" | ✓（但 self-report 字段误报）| ✗（识别了，但 plan 仍推进）| ✓（空 plan，最快退出）|
| 文件改动     | 无（git diff 空）| agent.py + test_tools.py 范围外改动 | 无       |

> 注：历史 05-23 轮次 task #2 是真正写新代码（baseline 未实现）。yansh 641K tokens 完成功能 + schema 闭环；cc 25K tokens 完成功能但漏改 tools_schema.py。

### Task #3 写文档（架构论证）

| 维度        | cc      | yansh   | yscode  |
|-----------|---------|---------|---------|
| 用时        | 150s    | 92s     | 129s    |
| tokens    | 128K    | 44K     | 426K    |
| 工具调用数    | 12      | 50      | 大量 read |
| 输出质量     | 合理，捕 2 个 hidden trap | 快但 1 错 2 偏（零探索）| 完整但昂贵  |
| 推荐结论     | 不做     | 不做     | 不做（一致） |

### Task #4 bug 修复（memory.find_memory 路径穿越）

| 维度        | cc      | yansh   | yscode  |
|-----------|---------|---------|---------|
| 用时        | ~48s    | 64.4s   | 183.9s  |
| tokens    | 51K     | 205K    | 592K    |
| 成本        | n/a     | —       | $1.85   |
| 测试结果     | ✅ 35/35 | ✗ 仍 1 failed | ✅ 35/35 |
| 副作用      | 无       | 静默失败（0 改动）| +4 个无关 shim 文件 |

### Task #5 跨文件重构（_err 加 tool 参数，~65 调用点）

| 维度        | cc      | yansh   | yscode  |
|-----------|---------|---------|---------|
| 用时        | 293.6s  | 378.5s  | 358.8s  |
| tokens    | 132K    | 692K    | 789K    |
| 成本        | n/a     | —       | $2.58   |
| exit code | 0 ✓     | 1（CLI crash）| 1（PlanFailed）|
| 适配率      | 100%    | 90%+（已改但崩溃）| 0%      |
| 测试结果     | ✅ 41/46 pass（5 是 baseline）| ⚠ 同左（修改正确）| ✗ 未改    |

---

## 三、场景推荐

### 用 cc 的场景

- **日常 coding 助手**：5 类任务全部能稳定完成，token 消耗最低且稳定（50K–132K）
- **bug 修复（含失败测试）**：有测试做信号锚点时，cc 单线程串行 1 次跑通，无需 plan overhead
- **快速探索 / 信息检索**：路径最短，不需要专门工具也能覆盖
- **任何需要可靠结果的时候**：cc 是唯一 5/5 完成的工具，是真正意义上的"程序员替代品"

### 用 yansh 的场景

- **中型功能完整落地**（含 schema、文档、清理）：plan agent 划 scope 准时，yansh 会做"语义闭环"——顺手改 tools_schema.py、清死代码等 cc 不做的事
- **需要 AST 级别探索的场景**：`get_symbol_definition` 一击带 docstring 返回，能给出"为什么这么设计"而不只是"代码做什么"
- **audit 模式下的只读分析**：强制只读 + dispatch_subagent 自分子任务，适合不熟悉的代码库
- **修完 P1 技术债之后**：P1 全部修完后（当前状态截至 2026-05-26 已完成），跨文件重构场景预期有显著改善

### 用 yscode 的场景

- **从零写新功能 / 多阶段开发**：plan→code→test 阶段划分清晰，architect 能正确识别"无需修改"并空 plan 退出，比 yansh 更节制
- **写代码任务 + 测试验证**：task #2 / #4 修法完整，是 yscode 的强项
- **不需要写文档或探索的纯实现任务**：避开 yscode 的弱区（探索 / 论证 / windows 编码）

---

## 四、yansh 暴露的关键问题（P1/P2/P3）

**P1（已全部修复，2026-05-26）**

- **P1 #4 plan 不接受 coder "无需修改"信号**（task #2）：coder 报 task_complete 后，plan 仍按 expected_edits 推进；修法：coder 早退时显式向 plan 传 "no_change" 信号，plan 跳过剩余子任务
- **P1 #5 plan 写代码细节文档前零探索**（task #3）：plan agent 凭脑子生成技术文档，1 错 2 偏；修法：写代码细节文档前强制派 explorer 子 agent 扫码
- **P1 #6 baseline failure 识别吞用户请求**（task #4）：将用户 prompt 明确要求修的失败误判为 pre-existing 放过，静默 task_complete；修法：过滤时区分"在 prompt 中提及"和"完全无关"的失败
- **P1 #7 主仓 _err 签名 agent.py/tools.py 不一致**（task #5）：agent.py 已按新签名调用，tools.py 未适配，工具异常时触发 CLI crash；修法：统一签名（30 分钟内可修）

**P2**

- **P2 #1 `--json` stdout 被 Rich console 污染**（task #4 v1）：多模块各自 `Console()` 输出到 stdout，`json.loads` 直接挂；修法：抽 `console_shared.py` 单例，已在 task #4 v2 修复，但未完整回归

**P3**

- **P3 #1 Coder 阶段"用尽轮次"假警告**：LLM 已 task_complete(success=true) 但仍打"已用尽 N 轮"警告，增加 log noise
- **P3 #2 LLM 对 `/workspace` docker-style 路径假设**：execute_command 里 LLM 会试 `cd /workspace` 但 yansh 不 chroot，导致 pytest 拿不到输出反复重试

---

## 五、三方"画像"总结

**cc — 稳定的程序员替身**
- 5/5 完成率，token 方差最小（50K–132K），日常使用最可靠
- 局限：self-report 字段不可信（只信 git diff），缺 AST 工具，修复深度偶尔偏浅（漏 defense-in-depth）

**yscode — 写代码任务的强手**
- 写代码场景（task #2 完美 / task #4 修对）表现突出，plan→code→test 阶段划分是优势
- 局限：探索 / 论证场景退化严重；architect 在 windows cp932 环境容易转圈 PlanFailed；倾向"工程洁癖"过度补结构；token 方差极大（56K–789K）

**yansh — 高上限但机制脆弱（修前）**
- 专门工具（get_symbol_definition / dispatch_subagent）+ replay 可观测是架构优势
- 修前：4 个 P1 级机制问题导致 2/5 完成率，token 方差 21×（44K–915K），不适合生产使用
- 修后（P1 全部修完）：预计在中型功能落地 + 跨文件重构场景优势回归

---

## 六、关键教训（2 条最重要）

**1. plan/coder 解耦是双刃剑**

plan 准确时（task #3，44K tokens）是 yansh 最省的跑法；plan 错误时（task #2，915K tokens）会把"识别到无需修改"的 coder 推进到空跑 20 轮 fix loop。yansh 的 token 消耗几乎完全由 plan 准确性决定——优化 plan agent 是 yansh 的最高 ROI 方向。

**2. self-report 字段不可信，只信 git diff + pytest**

cc 的 `files_modified`、yansh 的 `task_complete summary`、yscode 的 `success: true` 都出现过与实际结果不一致的情况。任何 AB 评估必须现场跑 `git diff --stat` + `pytest` 验证客观结果，不能依赖 LLM 的自我报告。

---

## 数据来源

- 历史两方对比（2026-05-23）：`./20260523_task{1-5}_compare.md`（含 task #4/#5 多版本 v2/v3/v4）
- 三方对比（2026-05-25）：`../../../notes/shadow/ab/2026-05-25_0{1-5}-task{1-5}-3way.md`
- 汇总笔记（2026-05-25）：`./2026-05-25_06-summary-3way.md`
- yansh 技术负债清单：`~/.claude/projects/.../memory/project_yansh_tech_debt.md`
