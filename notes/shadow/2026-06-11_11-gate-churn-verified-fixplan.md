# gate churn 真根因核实 + 最终 fixplan（采纳 Fable 5，2026-06-11）

> 承接 ./2026-06-11_07（minire 现象）、_08（opus 初判，部分错）、_09（Fable5 提示词）、_10（Fable5 分析）。
> 本文 = opus 对 Fable 5 论断的逐条核实（全部成立）+ 采纳后的最终 fixplan。**待批准执行**。

## 一、Fable 5 论断核实（全部成立，log/源码逐条确认）

| 论断 | 核实 |
|---|---|
| gate 命令混入已删根目录脚本 `test_lexer_quick.py`/`test_parser_quick.py`/`test_match_quick.py`/`test_cli_quick.py`/`test_final_check.py` | ✅ run.log 458-470 实锤；pytest collected 0 / no tests ran |
| 8 轮全是同一条「强制测试：改了实现但无正规 tests/」假回灌，0 次真测试红/绿 | ✅ 8 次，行号 476/659/845/1296/1900/2092/2750/3562 |
| `_infer_test_scope` 规则1（agent.py:1557-1563）对 test_*.py 文件名直接入 scope，**不查存在性** | ✅ |
| 台账 append-only：`record_file_modified`（task_log.py:162-165）只 append，`delete_file` 不移除 | ✅ |
| **opus Bug 2（收敛被「爱加测试」击穿）不成立** | ✅ collected-0 强制分支 `continue`（4493）在收敛检测（4562）**之前** → 收敛一行没执行过 |
| opus Bug 1（else 硬编码 failed）成立但只是直接原因，**Fix1 单独救不回** | ✅ `_final_gate_verdict`（1692-1693）用同一 stale snapshot 重推 scope → 同坏命令 → collected-0 → minire 有 smoke → 1715 `return no_command` ≠ passed → 仍 False |

## 二、opus 被纠正处（Fable 5 第三次纠我）

1. **Bug 2 完全错**：收敛代码从未执行；失败 test-id 集合从不存在（gate 从没真正跑起测试，全是 collected-0）。
2. **误读 churn 内容**：不是 agent 爱加测试致测试集变化，而是 **gate 命令拼了幻影文件**，agent 在理性调试一个**只存在于 gate 命令里的幻影**（只收到「collected 0」文案、看不到 gate 命令与 stderr → 删 `__init__`/改 pytest.ini/`pip install -e` 五重保险）。套件几乎全程绿。
3. **过拟合点判断偏**：不是收尾/复核机制，而是 **scope 隐含「modified 文件还在」的假设**。miniQL 改既有少删→不触雷；minire 建草稿→删草稿→必触雷。

## 三、真根因链

append-only 台账 × greenfield 删草稿 → `_infer_test_scope` 把已删的 `test_*_quick.py` 仍拼进 gate 命令 → pytest collected 0 → `_no_tests_collected` 误判「项目无正规测试」→ collected-0 强制分支（无一次性标志）8 轮重复同一假回灌 → gate_round 耗尽 → while-else（4601）硬编码 failed → success=False。**功能轮32 已 100%（175 绿 + 黑盒 15/15），全程是 gate 自造幻影。**

## 四、最终 fixplan（采纳 Fable 5，按杠杆排序）

### Fix A【杀根因，必做】scope 只收still存在的文件
- 位置：`_infer_test_scope` agent.py:1557-1563（规则1）。加存在性过滤：`(ws / fn).is_file()` 才入 scope。
- 一处修两条路径（主循环 gate + `_final_gate_verdict` 共用此函数）。
- 效果：minire 的已删草稿不再入 scope → gate 命令只剩真 tests/ → 轮33 targeted passed + agent 已 task_complete → **直接 success=True，成本砍半以上**。

### Fix B【防御层】targeted collected-0 ≠ 项目没测试（矛盾仲裁）
- 位置：collected-0 分支前（4472-4473 附近）。coverage=="targeted" 且 collected-0 时，**先回退全量命令重测**再下结论（agent 宣告绿 vs gate collected-0 = 观测矛盾 → 先疑 gate 自己的命令，而非默认 agent 错）。
- rc=4 / "file or directory not found" / "no tests ran" 在 targeted 下识别为 gate 内部错误，不当作「项目无测试」。

### Fix C【止损层】collected-0 强制分支加一次性标志（对称 _smoke_demanded）
- 位置：4475-4493。加 `_collected0_demanded` 标志，第二次命中不再重复同一文案回灌 → 转全量兜底或诚实落 verdict。防 8 轮空转。

### Fix D【配套】else→_final_gate_verdict 对称化（原 opus Fix1）
- 位置：4600-4602。gate 轮耗尽改调 `_final_gate_verdict` 重测 + `_ever_completed` 认可。单独救不回（需 A），但与总轮次路径对称是对的，配套做。

### Fix E【透明化，廉价高回报】gate 回灌文案带命令原文 + clip 输出
- collected-0 / 失败回灌的文案附 `test_cmd` 原文 + clip(stdout/stderr)。agent 看到那行「file or directory not found / no tests ran」一轮就能自解，不再五重保险瞎调。

### 判据（不采纳 opus 的「只动 tests/ 止损」——会误伤）
用**矛盾仲裁**：agent 刚自测全绿宣告完成 ∧ gate collected-0/红 → 先全量重测仲裁，而非默认 agent 错。

## 五、优先级与验证
- **Fix A 必做（杀根因）**；B/C/E 防御+止损+透明化（都低风险、复用现有函数）；D 配套对称化。
- 三处均不碰「judge 失败且 collected>0」的正常红回灌路径，不伤真 bug 多轮修复。
- 验证：补单测（scope 排除不存在文件；targeted collected-0 回退全量；collected-0 一次性标志）；minire 干净 ws 重跑，看是否轮~33 success=true、不再 churn 到119、黑盒维持 15/15、成本砍半。miniQL 干净 ws 回归确认不破坏（高耦合路径）。
