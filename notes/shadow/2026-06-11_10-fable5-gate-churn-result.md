# Fable 5 独立分析结果：minire gate churn + 假阴性

> 对应提示词：[./2026-06-11_09-fable5-gate-churn-prompt.md](./2026-06-11_09-fable5-gate-churn-prompt.md)
> 结论：**与 opus 初判（Bug 1 部分对、Bug 2 不成立）显著冲突**。真根因是 scope 推断引用了已删除文件。

## 决定性证据

run.log 462-476 行，gate 实际命令：

```
pytest tests/test_lexer.py test_lexer_quick.py tests/test_parser.py
       test_parser_quick.py ... test_cli_quick.py ... test_final_check.py
```

`test_*_quick.py` / `test_final_check.py` 是 agent 轮 ~30 已 `del` 的根目录临时脚本。
pytest 对不存在路径报 usage error（rc=4）→ `collected 0 items / no tests ran`，
stderr = `ERROR: file or directory not found: test_lexer_quick.py`（即末行 JSON 的 error 字段）。

**8 轮 gate 回灌全部是「强制测试：改了实现但无正规 tests/」同一条假消息**
（log 行 476/659/845/1296/1900/2092/2750/3562）。没有任何一轮真正的「测试失败回灌」或「测试通过」。
收敛检测（agent.py:4562-4583）在本次 run 中一行都没执行过。

## 触发链（逐行核对）

1. task_log.py:162-176 — `_task_files_modified` 只追加；`delete_file` / shell `del` 不移除条目。
2. agent.py:1558-1562 — `_infer_test_scope` 规则 1：文件名 `test_*.py` 直接进 scope，**不检查存在性**。
3. linter.py:88 — scope 原样拼进 pytest 命令 → rc=4、collected 0。
4. agent.py:1668 — `_no_tests_collected` 把「gate 自己命令拼错」误判为「项目没有正规测试」。
5. agent.py:4475-4493 — collected-0 强制分支：回灌与事实相反的文案（实际 175 全绿）后 `continue`；
   该分支**无一次性标志**（不对称于 `_smoke_demanded`），且 `continue` 在收敛检测之前 → 收敛永不执行。
6. 8 轮烧满 → while-else（4600-4602）硬编码 `gate_status="failed"` → final_success=False。

churn 内容澄清：agent 套件几乎全程绿。pip install -e / pytest.ini / 删 tests/__init__.py /
重写全部测试的"五重保险"，是 agent 在理性调试一个**只存在于 gate 命令里的幻影收集问题**——
它只收到 "collected 0" 文案，看不到 gate 跑的命令和 stderr，不可能发现真因。

## 对 opus 初判的裁定

- **Bug 1（else 硬编码 failed，不对称）**：成立，但只是直接原因。
  **Fix1 单独上救不回 minire**：`_final_gate_verdict`（agent.py:1692-1693）用同一个 stale
  `snapshot_files_modified()` 重推 scope → 同样坏命令 → collected-0 → smoke 存在 → 返回
  `no_command` ≠ passed，final_success 照样 False。
- **Bug 2（收敛被「爱加测试的 agent」击穿）**：**不成立**。收敛代码从未被执行到；
  失败 test-id 集合也从未存在（gate 从未真正跑起任何测试）。
- **opus Fix2（只动 tests/ 判止损）**：本次会更早止损但**误伤**——判 agent 的错（failed），
  假阴性照旧，且掩盖框架自身 bug。

## 最高杠杆改动（3 处）

1. **scope 只收还存在的文件**（杀根因）：agent.py:1558 规则 1 加 `(ws / fn).is_file()`。
   gate 循环与 `_final_gate_verdict` 共用 `_infer_test_scope`，一处修两条路径。
   修好后 minire 轮 33 即 targeted passed + 已 task_complete → 直接成功，成本砍半以上。
2. **targeted collected-0 ≠ 项目没测试**（防御层）：进 collected-0 分支前，coverage=="targeted"
   时先回退全量命令重测再下结论；rc=4 / "file or directory not found" 识别为 gate 内部错误。
3. **collected-0 强制分支加一次性标志**（止损层，对称 `_smoke_demanded`）：第二次命中不再
   重复回灌同一文案，转全量兜底或诚实落 verdict。
   （Fix1 else→`_final_gate_verdict` 对称化仍做，定位是配套。）

三处均不碰「judge 失败且 collected>0」的正常红回灌路径，不伤真 bug 多轮修复场景。

## 判据问题（prompt Q4）

「只动 tests/ 无实现改动」不可靠（本次 churn 轮改的全是 conftest/pyproject/pytest.ini/tests，
会触发误伤）。更可靠的是**矛盾仲裁**：agent 刚自测全绿宣告完成而 gate collected-0/红 →
两个观测矛盾 → 先怀疑 gate 自己的命令（全量重测仲裁），而非默认 agent 错。

## 盲点（prompt Q5）

- **append-only 台账 × greenfield 文件生命周期**：scope 推断隐含「modified 的文件还在」假设。
  miniQL（改既有文件、少删除）不触雷；minire（建草稿→删草稿）必触雷。
  fixplan 阶段 1 过拟合 miniQL 的点在此假设，不在收尾/复核机制。
- **gate 反馈不透明**：强制分支固定文案不带 `test_cmd` 与输出。每条回灌应附命令原文 +
  clip 输出——agent 看到那行 "file or directory not found" 一轮就能解。
- **gate 语义**：#3 确认 drive 本次未执行到，非 churn 之源，不必动。真问题是 gate
  无条件信任自己拼的命令、不信 agent 演示过的绿——加矛盾仲裁即可，不需降级为「只验一次」。

## 成本注脚

8 轮幻影回灌 × 全 context re-drive = 5.83M input tokens / $18.58 / 1522s，
而功能在轮 32 已 100%（175 passed + 黑盒 15/15）。
