# gate 确认-回灌循环 churn + 假阴性：根因分析 + 修改方案（2026-06-11）

> 承接 ./2026-06-11_07（minire 实验：黑盒15/15、自测175绿，但 success=False、churn 到轮119/$18.58）。
> 本文 = 我（opus）对根因的分析 + 修改方案。另见 ./2026-06-11_09（给 Fable 5 的独立分析提示词，对照验证）。

## 一、现象复述

minire（正则引擎，耦合度低于 miniQL）：sonnet **轮32 就全绿主动 task_complete**。但进入 gate 循环后 churn 到 **总轮119 / gate 满8轮**，最终 success=False（test_result=fail）。而进程结束后实测 agent 自测 **175 passed 全绿**、黑盒 **15/15**。= gate 自己制造的假阴性 + 成本翻倍。

## 二、根因（两个叠加 bug，均在 agent.py gate 主循环 4409-4616）

### Bug 1（决定假阴性）：gate 轮耗尽路径**硬编码 failed，不重测**——与总轮次耗尽路径不对称
- `while gate_round < _SOLO_GATE_MAX_ROUNDS` 的 `else` 子句（**4600-4602**）：gate_round 到 8 时直接 `gate_status="failed"`，**不跑任何最终测试**。
- 对照：总轮次耗尽路径（**4413-4421**）调 `_final_gate_verdict()`（**1687-1722**）**重跑测试**并诚实判定（可返回 passed / coverage_unknown / no_smoke）。
- minire 是 gate_round 先到 8（run.log 实锤打印「回灌已达 8 轮上限」= 4601），走 else → 硬编码 failed。**agent 最后一次 drive 已把套件修绿，但 else 不重测 → 用了「failed」这个凭空结论** → `final_success = agent_completed and gate_status=="passed"` = False。**这是假阴性的直接原因。**

### Bug 2（决定 churn + 成本翻倍）：收敛/止损条件被「爱加测试的 agent」击穿
- 同错收敛（**4579**）只在 `_cur_gate_key == _prev_gate_key AND not _new_writes` 时停。
- minire 的 sonnet 每个 gate 轮被回灌后，把复核当成「再加测试」的邀请：扩到175个测试、改自写测试、引入又修复自身回归（`[\n]` 字符类转义 / `{...}` 计数量词的测试与实现不符）、甚至跑题 `pip install -e`。
- 后果：**①每轮都有新写 → `_new_writes` 恒非空 → 收敛永不触发**；**②失败的 test-id 集合每轮在变（加/改/修测试）→ `_cur_err_hash` 每轮变 → `_cur_gate_key` 永不重复 → 收敛永不触发**。
- 于是烧满全部 8 gate 轮 ×（每轮 ~15 drive）→ 总轮119。每轮 gate test() 跑全量+smoke(subprocess) 30-40s，成本累积到 $18.58。

### 为什么不是「绿就 break」直接成功？
- gate 主循环里只要某轮 `judge(test_result)` 绿就 break（4498-4543）。minire churn 说明**每个 gate 轮的 test() 都是红的**——因为 agent 在 drive 里加的新测试（如断言 `[\n]` 可用）与它自己的实现不符，gate 的 targeted scope 跑到这些就红。agent 下一轮修一部分又加一批 → 永远红→回灌→修+加→红。

## 三、修改方案

### Fix 1【必做，直接修假阴性，低风险】gate 轮耗尽改为重测裁定，与总轮次路径对称
- 位置：4600-4602 的 `else` 子句。
- 改：不再 `gate_status="failed"`，改为调 `_final_gate_verdict(_ws_path, _timeout_gate)` 重跑当前状态 + 套用与 4418 相同的 `_ever_completed` 认可逻辑（passed 且曾宣告完成 → 认 agent_completed）。
- 效果：裁定反映**真实最终状态**而非凭空 failed。minire 最终绿 → _final_gate_verdict 返回 passed（targeted+smoke 存在）→ success=True。
- 风险：极低，与既有 4413-4421 同构（复用同一函数）。

### Fix 2【churn/成本，止损】「爱加测试」不算修复进展
- 位置：收敛检测 4575-4583。
- 改：把 `_new_writes`（4576）的「是否有新写」判定，从「任意新写」收紧为「**新写里有没有实现文件改动**」——复用 `_has_impl_files`。即：两轮之间只新增/修改 `tests/` 而无实现文件改动 → 视为**非进展 churn**，不阻止收敛。
  ```python
  _new_writes = set(_task_log_mod.snapshot_files_modified()) - _prev_gate_modified
  _new_impl_writes = [f for f in _new_writes if <非 tests/ 的 .py 实现文件>]  # 仿 _has_impl_files
  # 收敛：err 集合相同 AND 无新实现改动 → 停（加测试不算修复进展）
  if _prev_gate_key is not None and _cur_gate_key == _prev_gate_key and not _new_impl_writes:
  ```
- 但 err 集合每轮在变会让 `_cur_gate_key` 不重复，单 Fix 2 仍可能不收敛。**补充**：加 `_gate_red_rounds` 计数——agent 已 task_complete 过（`_ever_completed`）后，gate 仍连续 N 轮（如 2）红且每轮仅 tests/ 变动无实现改动 → 判定 agent 在 churn 自己的测试，止损 break（走 Fix 1 的重测裁定）。

### Fix 3【根上防 churn，可选】gate 对「已交付绿」的 agent 只读不催
- 现 #3 确认 drive（4531-4543）与红→回灌都会驱动 agent 继续动。eager agent 把每次驱动当扩测试邀请。
- 方向：gate 一旦观察到过一次全绿（`judge` True），后续仅做「确认一次」，不再因 agent 新引入的红反复回灌（那些红是它自己加测试造成的，非任务需求未达成）。需区分「任务需求测试红」vs「agent 新增测试红」——较难确定性区分，列为后续。

## 四、优先级与验证
- **先做 Fix 1**（必做，直接把这类假阴性→真阳性，~8 行，复用 _final_gate_verdict）。
- **再做 Fix 2**（止损 churn 成本，~10 行，复用 _has_impl_files）。
- Fix 3 留待评估（需确定性区分两类红）。
- 验证：补单测（gate 轮耗尽时套件绿 → success=true；只加 tests/ 无实现改动 → 收敛止损）；minire 干净 ws 重跑，看是否轮~33 收尾 success=true、不再 churn 到119、黑盒维持15/15。
