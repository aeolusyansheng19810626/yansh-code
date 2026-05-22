# P0 #3 第二波：task_complete 信号全流程贯通

承接 [./2026-05-21_09-p0-3-live-validation.md](./2026-05-21_09-p0-3-live-validation.md) 实操
发现的漏洞——LLM 主动 task_complete 后，信号只在 fix loop 内退出，**没传到外层 run() 的
attempts 循环和最终 result**。这一波把信号链通到顶。

## 改了什么

### fix() 返回 dict

```python
def fix(...) -> {"early_exit": bool, "success": bool, "summary": str}
```

三个 return 点：
- LLM 调 task_complete → `{"early_exit": True, "success": ..., "summary": ...}`
- 沉默退出（兜底已追问过仍沉默） → `{"early_exit": False, ...}`
- 软上限耗尽 → `{"early_exit": False, ...}`

### code() 返回 Optional[dict]

per-file inner loop 里逐个 dispatch 后检查 `_task_complete` sentinel：
- `success=False` → 立即 return（Coder 主动放弃整个任务，跳过后续文件）
- `success=True` → 跳出本文件 inner loop，继续下一文件，最终 return signal
- 完全没遇到 sentinel → return None（兼容旧调用）

### run() 接两路信号

阶段 2 后：`coder_signal.success=False` → 跳过 review/test/fix 直接标失败。

阶段 3 attempts 循环里 fix() 返回 fix_signal：
- `early_exit=True, success=True` → "LLM 主动声明任务完成（剩下是 pre-existing）"，标 success 退出
- `early_exit=True, success=False` → "LLM 主动放弃"，标 fail 退出
- `early_exit=False` → 正常 attempts += 1 继续 retry

linter 阶段 fix() 调用同理识别 success=False 终止。

### report() 加 task_complete_signal 字段

可选字段，None 时不输出，避免污染历史 result schema。

### 顺手修了一个 bug

Coder/linter 早退时 `report(False, None, ...)` 让外层 main.py 的
`res["test_result"].get("stderr")` 崩。改成 dummy dict
`{returncode: -1, stdout: "", stderr: "<放弃理由>"}`，统一 test_result 接口。

## 验证

### 单元测试（新建 tests/unit/test_agent_loop.py）

8 条用例全通过：
- fix() task_complete(true/false) → early_exit dict 形态正确
- fix() 两轮沉默退出 → early_exit=False
- code() Coder 放弃 → 立即 return early_exit signal
- code() Coder 完成 → return signal；Coder 沉默 → return None
- report() task_complete_signal 字段有/无两种形态

### 集成验证（重跑场景 A/B）

| 场景 | 修复前 | 修复后 |
|---|---|---|
| A pre-existing | attempts=3 → fail，47s | **attempts=0 → success**，33s |
| B 矛盾任务 | attempts=0 → pass（错的），31s | **attempts=0 → fail**（正确）+ stderr 含放弃理由，2s |

场景 B 时长从 31s → 2s——一旦 Coder 主动放弃，外层立即终止，不再做无用的测试 + fix retry。

### Pre-existing 失败保留

`python tests/run_unit.py`：10/10 文件通过（新加 test_agent_loop.py）。

## 评估

**这一波让 task_complete 协议从"内部退出 loop 的私有信号"升级为"全流程外部决策的公共契约"**。

最小改动、最大杠杆：

- fix() / code() 各加 ~10 行 return signal
- run() 加 ~30 行识别 signal
- report() 加 1 个可选字段

**实操 + 单测 + 集成验证三层确认**：
- 单测覆盖各 return 点形态
- 集成覆盖端到端行为差异（场景 A/B 前后对比）
- 真实 LLM 调用确认 Sonnet 4.6 的 task_complete 调用行为符合预期

下一波（不在这次范围）：把 task_complete signal 写进 `task_log` 持久化，让回放/统计能追溯
LLM 主动声明的历史。
