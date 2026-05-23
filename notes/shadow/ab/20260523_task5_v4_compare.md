# Task #5 v4：backlog #1 baseline 识别 → yansh 终于 pass

承接 [`./20260523_task5_v3_compare.md`](./20260523_task5_v3_compare.md)。v3 已经把"LLM 把 56 处 _err 全改完"解锁了，但 yansh 框架还在报 `test_result: fail` —— fix loop 把 baseline pre-existing 失败误判为本次回归。v4 修了 backlog #1 之后，yansh 第一次跑通这个跨文件重构 task。

## 改法（commit `1e3ce5f`）

`agent.py`：
- 模块全局 `_BASELINE_FAILURES: set`
- `_parse_pytest_failures(text)` 抽 `^FAILED <id>` 行
- `_capture_baseline_failures(test_command)` 在 `_run` 进入 code() 前跑一次 pytest 抓 baseline
- test+fix 循环：`returncode != 0` 但 `current \ baseline` 为空 → 直接判 pass
- `fix(baseline_failures=...)` 把 baseline 列表注进 user content（LLM 看到也跳过）

只对 `pytest` 命令开启 baseline 捕获，其他 test_command 跳过。捕获失败 best-effort 不抛。

## v1 → v2 → v3 → v4 完整对比

| 维度 | v1（无修法） | v2（改法 1.0） | v3（改法 1.1） | **v4（+ baseline 识别）** | CC（参考） |
|---|---|---|---|---|---|
| `test_result` | fail | fail | fail | **pass** ✓ | pass |
| `attempts` | 3 max | 3 max | 3 max | **1** ✓ | 1 |
| duration | 499s | 460s | 581s | 541s | 294s |
| tool_calls | 130 | 129 | 92 | **57** | 54 |
| 总 tokens | 1.85M | 2.95M | 2.14M | **1.80M** | 184K |
| sonnet input | 1.05M | 1.61M | 1.92M | 1.59M | 184K |
| haiku input | 778K | 1.35M | 190K | 178K | 0 |
| _err 适配率 | 4/56 (7%) | 46/56 (82%) | 56/56 (100%) | **100% (subset baseline)** | 100% |
| baseline 短路触发 | n/a | n/a | n/a | **✓ "15 条全在 baseline 内 → 视为通过"** | n/a |

v4 在 v3 改法基础上加了 backlog #1 → 第一次 attempt = 1，无 fix 循环，无机械错 detector，直接 pass。

## v4 详细过程

```
阶段1：制定计划
[baseline] 跑一次 pytest tests/unit/test_tools.py tests/unit/test_subagent.py 记录 pre-existing failures...
[baseline] 记录 16 条 pre-existing failures（fix 阶段会忽略）
阶段2：生成代码
（57 个 tool_call 完成，包括 2 处文件级 write_file + 多处 replace_in_file）
阶段3：测试与修复
执行测试：pytest tests/unit/test_tools.py tests/unit/test_subagent.py
（15 failed, 74 passed）
[baseline] 当前 15 条失败全部在 baseline 内（16 条 pre-existing）→ 视为通过
```

`task_complete_signal`:
> tests/unit/test_tools.py 中的所有 _err 相关改动均已完成：1) test_err_helper_attaches_error_kind 已补入第三参数 "read_file"；2) test_err_helper_rejects_unknown_kind 已补入第三参数 "some_tool"；3) test_err_helper_attaches_tool 新增单测验证 e["tool"] == "read_file"。

注意：v4 实测 baseline 16 条，运行后 15 条——LLM 的改动顺手修好了 1 条 pre-existing 失败（多半是 path_traversal 类的某条因 _err dict 变化恰好不再 fail）。subset 判定仍然成立，不影响 pass 判定。

## 4 文件 attempts 用尽？为什么还 pass

警告：`agent.py 已用尽 5 轮（expected_edits=5）` + `tests/unit/test_tools.py 已用尽 5 轮（expected_edits=6）`。

但 task_complete 是 LLM 在最后一轮显式调的（success=true），warning 是基于"轮次计数到上限"的副作用——这其实是 backlog 第 3 条"用尽轮次假警告"的活样本。功能上不影响 pass，但 noise 多了一行警告日志。

## v3 → v4 涨幅

- tool_calls：92 → 57（-38%）— 因为 v4 attempts=1 不进 fix 循环，省了 fix loop 的所有调用
- tokens：2.14M → 1.80M（-16%）— 同上
- duration：581s → 541s（-7%）— 主要是没跑 fix 循环

## v4 vs CC（参考）

CC 在 task #5 用 184K / 54 tools / pass。v4 是 1.80M / 57 tools / pass。v4 token 仍是 CC 的 ~10×。

差距来源：
- yansh 每轮重发完整 messages，CC 用 prompt cache（gpt5 plan §P1.0/P1.1 的题目）
- yansh 的 _CODER_ROLE / _PLANNER_ROLE 是中文 + few-shot，CC 系统提示更紧凑
- yansh 的 22 轮 plan-driven 意味着每轮都重发整 file 上下文

token 削减是另一个 P 工作（gpt5-5-review-1-structured-cloud.md 计划里的 P1.1 / P1.2 / P3.1）。本次只解锁了"跨文件重构 task 跑得通"这一项基础能力。

## 5 次 AB 完整轨迹

| 版本 | 改法 | yansh test_result | attempts |
|---|---|---|---|
| v1 | 无 | fail | 3 max |
| v2 | plan-driven 22 轮 + expected_edits + edit_strategy_hint + detector(≥5) | fail | 3 max |
| v3 | detector 阈值 ≥1 | fail | 3 max |
| **v4** | **+ baseline 识别** | **pass** ✓ | **1** ✓ |

v1-v3 的核心问题是"LLM 改完了但框架不认账"。v4 的 backlog #1 把"框架的成功判定"对齐到"LLM 实际工作"——只看增量回归，pre-existing 一律放过。

## 剩余 backlog（task5_v3 的 #2/#3/#4 仍未做）

1. ~~**fix loop baseline failure 识别**~~ ✓ 本次完成
2. **LLM 对 `/workspace` docker-style 路径假设**（task #4 暴露，task #5 v3 fix 循环也撞上）—— v4 因为没进 fix 循环未触发
3. **Coder "用尽轮次"假警告**：v4 仍有 2 条 warning（agent.py / test_tools.py 5 轮上限），实际 LLM 已 task_complete(success=true)
4. **Detector 扩 NameError / AttributeError**：v4 没用到 detector

## 数据文件

- `20260523_task5_v4_yansh.json` / `_stderr.log` — v4 数据
- v3 数据见 `20260523_task5_v3_compare.md`
- v1/v2 数据见 `20260523_task5_compare.md`

## 状态

- ✓ backlog #1 落地（commit `1e3ce5f`）
- ✓ task #5 v4 yansh 第一次 pass，attempts=1
- ✓ 22 单测全绿（除 baseline pre-existing 外）
- 跨文件重构 task 至此 yansh 结构上能干了
