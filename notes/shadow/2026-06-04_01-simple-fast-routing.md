# simple-fast 路径：跳过 plan() 直接进 coder

日期：2026-06-04

## 背景

yansh 的 simple 路由档之前只注入了规模 hint，architect（plan）阶段仍走完整一轮 LLM 调用。对于"修复单文件 bug"类任务，这是纯 overhead——约 30-50s + 1 次 LLM RTT。

## 改动内容（commit da49cbb）

### 新增工具函数（agent.py ~375 行）

**`_extract_filename_from_requirement(requirement)`**
- 正则提取 requirement 中的第一个目标文件名
- 反引号包裹优先（`` `tools.py` ``），其次裸文件名
- 过滤：URL（`://` 前缀）、`.bak` 后缀（`(?![\w.])` 尾部断言）
- 返回 None 时 simple-fast 不触发

**`_simple_fast_eligible(requirement)`**
- 有明确文件名 + 无 complex 关键词 + 无 exploration 信号 + 单文件（多文件回退 plan）
- False → 回退现有 hint 注入路径

### _run() 路由修改（agent.py ~3285 行）

- `plan_result = None` 哨兵，保证 `plan` 模式不崩溃（M1）
- simple-fast eligible 时：提前推断 test_command 保证 baseline 捕获有效（M2）
- test_command 回填在 early_exit 检查之后（M3）
- 不 eligible 时：回退 hint 注入 → plan()

### opus-4.8 review 发现并修复的问题

| # | 级别 | 问题 | 修复 |
|---|------|------|------|
| M1 | Major | `mode=="plan"` NameError 崩溃（plan_result 未赋值） | plan_result=None 哨兵 |
| M2 | Major | baseline 捕获时 test_command="" → pre-existing 误判回归 | simple-fast 进 plan 前提前推断 test_command |
| M3 | Major | 回填在 early_exit 前，污染 task_log | 移到 early_exit 检查之后 |
| m1 | Minor | URL 中的 .py 被当文件名 | 检查 `://` 前缀 |
| m2 | Minor | `foo.py.bak` 截断为 `foo.py` | 尾部断言改为 `(?![\w.])` |
| m5 | Minor | 多文件 requirement 误走 fast | `finditer` 数量 >1 时回退 |

## 效果数据

**task4（修复 memory.py slug bug）**：

| 版本 | elapsed | tokens | cost | 路由 |
|------|---------|--------|------|------|
| 无路由 | 123s | 225K | — | 完整 pipeline |
| 本次 | **79s** | **33K** | $0.156 | complex（LLM 判"定位 bug"需探索） |

本次走的是 complex 路由（prompt 含"定位 bug"触发 LLM 判 complex），simple-fast 未触发。但 token 降幅 -85% 主要来自 simple hint + 之前其他优化的叠加效果。

**simple-fast 真正触发条件**：prompt 需要明确点名文件 + 具体修改意图，不含"定位/探索/调用链"等信号。
例：`"修复 memory.py 里 find_memory 未调用 _slugify 的问题"` → 触发 simple-fast，跳过 plan()。

## 关键经验

- `_classify_task`（LLM 兜底）和 `_simple_fast_eligible`（关键词+文件名）是两层独立判断，前者决定路由档，后者决定是否走 fast。两者不一致时以 `_classify_task` 为准。
- "有测试失败，定位 bug 并修复"→ LLM 正确判 complex（需要探索），不是误判。
- simple-fast 的适用场景：用户已知目标文件和具体改法，直接描述改动，不需要 agent 先探索。
- opus-4.8 review 效果显著：3 个 Major 全部是真实 bug，尤其 M1 是确定性回归，作者单测完全覆盖不到（只测纯函数，不测 _run 集成路径）。
