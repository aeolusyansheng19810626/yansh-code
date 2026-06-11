# 给 Fable 5 的独立分析提示词：gate 确认-回灌循环 churn + 假阴性

> 在本地 CC（模型选 Fable 5）里把以下内容作为提示词。你能直接读本仓库代码和 log，
> 凡「位置指引」处请自己打开核对，不要只信我下面的转述。与我的结论冲突时直接指出。

---

## 背景与任务

本仓库（yansh）是自研 AI coding agent 框架。`solo` 模式 = 单一连续 context 端到端 agent（规划→读写跑修→自测→收尾），跑完后有一道**外部 test gate**：跑测试，红则把失败回灌进同一 context 继续驱动 agent 修，直到绿或耗尽（gate 最多 8 轮回灌，主 loop 最多 120 轮）。

最近一次实验（任务 = 让 agent 从零实现一个正则引擎 `minire`，纯标准库，多模块 lexer/parser/compiler/matcher）暴露一个问题：

- agent **轮 32 就全绿、主动 task_complete**（自测 + smoke 全绿）。
- 但进入 gate 循环后 **churn 到总轮 119 / gate 满 8 轮**，最终框架判 **success=False（test_result=fail）**。
- 而进程结束后实测：agent 自测 **175 passed 全绿**，独立黑盒（用 Python `re.fullmatch` 当 golden，15 组）**15/15 全过**。
- 即：**功能 100% 正确，却被 gate 自己 churn 成假阴性 + 成本翻倍（$18.58 / 1522s，对照同框架 miniQL 高耦合任务只要 $10.7-12 / ~80轮）**。

**请独立分析：为什么会 churn 到耗尽？为什么最终判 failed（而功能其实满分）？框架该怎么改才能既不假阴性、又不浪费轮次/成本，且不破坏对「真正有 bug、需要回灌修复」场景的有效性？**

## 你可以直接读的材料（请自己打开）

- **完整运行日志**：`C:/Users/ShengYan/Projects/AB-test/minire-1/run.log`（末行是含 tool_calls/tokens/cost 的大 JSON；中间有 gate 回灌、task_complete、测试 passed/failed 的演变）。
- **agent 产出的 ws**（含它写的 minire 包 + tests/，可在里面 `python -m pytest tests/ -q` 看当前真实状态）：`C:/Users/ShengYan/Projects/AB-test/minire-1/`
- **题目与独立 oracle**：`C:/Users/ShengYan/Projects/AB-test/minire-template/PROMPT.md`、`run_accept.py`（oracle=re.fullmatch）。
- **核心代码**（agent.py）：
  - gate 主循环：**agent.py:4409-4616**（`solo()` 内）。重点看：
    - 总轮次耗尽路径：**4413-4421**（调 `_final_gate_verdict` 重测）。
    - 绿→break：**4498-4543**（含 #3「绿但未重宣告→确认 drive」4531-4543）。
    - 同错收敛检测：**4562-4583**（`_cur_gate_key` = (test_cmd, err_hash)，`_new_writes`，停止条件）。
    - 红→回灌→re-drive：**4585-4599**。
    - **gate 轮耗尽 else 子句：4600-4602**（`gate_status="failed"`）。
    - 最终 `final_success = agent_completed and gate_status=="passed"`：4604-4605。
  - `_final_gate_verdict`：**agent.py:1687-1722**（重跑测试诚实判 gate_status）。
  - `_has_impl_files`（区分实现文件 vs 测试/数据）：约 agent.py:1594。
  - `_solo_drive`：约 agent.py:4091（每次回灌驱动一段，有自己的 soft_limit）。

## 我已做的加工结论（供对照，**不要全信**，请独立从代码和 log 判断）

我（opus）初判是**两个叠加 bug**：

1. **gate 轮耗尽（else, 4601）硬编码 failed、不重测**——与总轮次耗尽路径（4413 调 _final_gate_verdict 重测）**不对称**。minire 是 gate_round 先到 8 走 else（log 有「回灌已达 8 轮上限」），agent 最后一次 drive 已把套件修绿，但 else 用了凭空的 failed → 假阴性。
2. **收敛条件被「爱加测试的 agent」击穿**——收敛（4579）要求 `_cur_gate_key 重复 AND not _new_writes`；agent 每轮加新测试 → `_new_writes` 恒非空、失败 test-id 集合每轮变 → err_hash 变 → key 不重复 → 收敛永不触发 → 烧满 8 轮。churn 的内容是 agent 自己加的测试红了又修、跑题 pip 打包。

我的修改方向：Fix1 让 else 改调 _final_gate_verdict 重测裁定（对称化）；Fix2 收敛判定把「有无新写」收紧为「有无**新实现文件**改动」（只加 tests/ 不算修复进展，复用 _has_impl_files）+ agent 已宣告完成后连续 N 轮只动 tests/ 的红则止损。

## 请回答（结构化、可落地，别泛泛）

1. **churn 根因**：为什么每个 gate 轮的 test() 都是红的、且收敛从不触发？请从 log + 代码精确指出触发链（哪行让它停不下来）。我的「Bug 2 两条」对吗？有没有我漏的更深机制（如 _solo_drive 的 soft_limit 与 gate 轮的交互、_prev_gate_modified 快照时机、targeted scope 与 agent 全量绿的差异）？
2. **假阴性根因**：最终判 failed 是否就是 else(4601) 硬编码？_final_gate_verdict 重测能否救回（它对 minire 会返回 passed 还是 coverage_unknown？targeted scope 命中吗）？若返回 coverage_unknown，Fix1 是否还不够、要怎么补？
3. **最高杠杆改动**：只许改 2-3 处，会改哪？具体到行为。要兼顾：不假阴性、不浪费轮次、**不破坏「真有 bug 需要多轮回灌修复」的正常场景**（别为了止 churn 把该回灌的也砍了）。
4. **如何确定性区分**「agent churn 自己的测试」vs「任务需求测试真红需要继续修」？我用「只动 tests/ 无实现改动」当信号，可靠吗？有更好的判据吗？
5. **盲点**：这个 churn 是否还有比「agent 爱加测试」更深的框架设计问题（如 gate 的语义定位——它到底该「验证一次」还是「驱动到完美」？#3 确认 drive 是否本身就是 churn 之源）？这次 minire（低耦合早收尾）暴露的问题，是否说明 fixplan 阶段1 的收尾/复核机制过拟合了 miniQL（高耦合接近上限才收尾）的行为剖面？

请独立得出结论，与我上面的加工结论冲突时直接指出。
