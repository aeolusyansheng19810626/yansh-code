# gate churn fixplan 验证结果（2026-06-11）

> 承接 ./2026-06-11_11（最终 fixplan A/B/C/D/E）。
> 本文 = 修复落地 + minire-2 干净 ws 验证结果。

## 修复落地（agent.py，sonnet 执行 + opus review）

| Fix | 位置 | 内容 | 状态 |
|---|---|---|---|
| A【杀根因】| `_infer_test_scope` ~1558 | 规则1 加 `(ws/rel).is_file()` 过滤，已删草稿不入 scope | ✅ |
| B【防御】| gate 主循环 ~4474 | targeted collected-0 → 回退全量重测仲裁 | ✅ |
| C【止损】| gate 主循环 ~4407+4492 | `_collected0_demanded` 一次性标志，防 8 轮空转 | ✅ |
| D【对称化】| while-else ~4623 | gate 轮耗尽改调 `_final_gate_verdict` 重测，不再硬编码 failed | ✅ |
| E【透明化】| collected-0 回灌 ~4501 | 文案附 `test_cmd` + clip 输出，agent 可自解幻影文件 | ✅ |

- 编译通过（`py_compile`）
- 新增单测 `tests/unit/test_gate_churn_fix.py`（4 passed）
- `test_solo_fixplan_stage1.py` 无回归（11 passed）

## minire-2 验证结果

| 指标 | minire-1（修复前） | minire-2（修复后） | 变化 |
|---|---|---|---|
| success | False（假阴性） | True ✅ | 假阴性消除 |
| 总轮次（tool_calls 代理） | ~119轮 | 64 次 tool_calls | -46% |
| gate churn | 8 轮假回灌 | 无 churn | ✅ |
| cost_usd | $18.58 | **$6.87** | **-63%** |
| duration | ~1522s | **703s** | **-54%** |
| 黑盒（vs re.fullmatch） | 15/15（修复前已满分） | **15/15** | 维持 ✅ |
| agent 自测 | 175 passed | 181 passed | 更多用例 |
| gate_status | failed（假阴性） | passed（通过 task_complete_signal.success=True） | ✅ |

## churn 消除验证

- **不再有幻影文件入 scope**：Fix A 让 `_infer_test_scope` 只收 `(ws/rel).is_file()` 为真的测试文件，已删的 `_test_*_quick.py` 草稿不再拼进 gate 命令。
- **gate 第一轮 targeted 直接 passed**（Monitor 显示 `181 passed in 16.23s`），无 collected-0 假回灌。
- **一次 smoke 强制前移触发**（轮40，正常：agent 漏 smoke）→ agent 补好，gate 绿。
- **总成本从 $18.58 → $6.87，降 63%**；对比实验1 miniQL（$10.7-19），minire 作为低耦合任务成本低于 miniQL 中位，符合预期。

## 结论

gate churn 假阴性修复验证成功。「框架优化让弱模型可靠」在**正则引擎**（低耦合、greenfield、多模块）任务上成立，不局限于 miniQL（高耦合、已有文件）。

下一步：miniQL 回归确认高耦合路径未破坏（待跑）。
