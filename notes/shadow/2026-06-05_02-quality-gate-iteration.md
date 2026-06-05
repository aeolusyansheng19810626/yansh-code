# 质量门设计迭代记录

日期：2026-06-05  
前置：./2026-06-05_01-task21-fix.md

---

## 背景

task21 修复过程中引入了质量门，经历了 4 轮迭代才稳定。

---

## 迭代过程

### v1（commit fddc326）：质量门 + 调 fix()

检测到计划文件未被修改 → 调 `fix(_gate_tr, plan_result)`

**问题**：fix() 看到 pytest 通过就返回，不会补做编码工作。fix() 设计用途是修测试失败，不是补完未完成的编码。

---

### v2（commit fb1b751）：拦截 judge()，强制失败

移除 fix() 调用，改为在 `judge(test_result)==True` 时二次检查，计划文件仍缺 → `return report(False, ...)`。

**结果**：质量门正确拦截，yansh 内部报 false ✓  
**新问题**：runner 的 success 基于独立 pytest，与 yansh 内部无关，仍显示 true。

---

### v3（task21_runner.py + 清晰 prompt）

- runner 改为 `success = pytest_pass AND yansh_internal_pass`
- prompt 去掉"先 benchmark"要求，直接说"O(n²) 嵌套循环，改成 O(n)"

**结果**：59s / 103K / success=true ✓（review 前）

---

### v4（commit a4d849a）：opus review 修复

opus review 发现两个问题：
1. `f in a` 无边界子串匹配（`add.py` 命中 `myadd.py`）→ 改为 `_path_match()` 按 `/` 边界后缀匹配
2. baseline-pass 旁路绕开了质量门 → 补二次检查

**结果（review 后重跑）**：**81s / 177K / success=false（误判！）**

根因：architect 过度规划了 `datakit/indexer.py`（task21 不需要），质量门拦截了它。
search.py 实际已正确修改，任务实质完成，但被误判为失败。

---

### v5（commit ae1caae）：质量门改为 warning-only

**结论**：质量门"强制失败"在 architect 过度规划时会误判成功任务。当前 architect 计划的准确性还不够，硬拦截太早。

改法：两处强制失败 → 仅打印 `[质量门⚠]` 黄色警告，不影响结果。

**最终结果**：59s / 144K / success=true ✓

---

## 最终架构（warning-only 质量门）

```
阶段3前：
  _planned_files = plan 里 expected_edits>0 的文件
  _missing_files = _planned_files - files_modified（用 _path_match 按边界匹配）
  若有缺失 → [质量门⚠] 警告

judge(test_result)==True 时：
  重新计算 _still_missing（fix loop 可能已补改）
  若仍有缺失 → [质量门⚠] 警告（不阻断）
  → 正常 report(True)

baseline-pass 旁路：同样仅警告
```

---

## 经验教训

### 1. 质量门的正确定位

质量门是**观测工具**，在 architect 计划准确性不稳定时不应作为硬门。
等 architect 过度规划的问题被修掉后，再考虑收紧为 hard fail。

### 2. fix() 不适合补做编码工作

fix() 的职责是"修测试失败"，不是"完成 coder 未做完的编码"。
用错工具会导致 fix() 静默退出（tests pass → 什么都不做）。

### 3. runner success 应联动 yansh 内部结果

```python
success = pytest_pass AND yansh_internal_pass
```
yansh 质量门/task_complete 报失败时，runner 也应报失败。

### 4. prompt 是第一道防线

去掉"先 benchmark 再优化"的歧义表达后，task21 直接通过，无需任何 pipeline 补救。
好的 prompt > 复杂的 pipeline 保护机制。

### 5. review 发现 + 修复 + 验证要形成闭环

本次 review → 修复 → 验证的闭环发现了 `_path_match` 的正确性改进，
同时也暴露了"强制失败"策略太激进的问题，推动了 warning-only 的设计决策。

---

## 当前未决问题（积累 case 后再处理）

- 何时将质量门从 warning 升级为 hard fail？
  - 条件：architect 过度规划率降低到可接受水平（观察 task22-25 / task16-20 是否有质量门警告）
- 是否需要区分"architect 过度规划"和"coder 真的没改"？
  - 可行方案：只对 requirement 里明确提到的文件名做 hard fail，其余 warning

---

## 相关 commits

| commit | 内容 |
|--------|------|
| fddc326 | 质量门 v1 + pattern 5（benchmark） |
| fb1b751 | 质量门 v2：拦截 judge() 强制失败 |
| a4d849a | review 修复：_path_match + baseline-pass 补门 |
| ae1caae | 质量门 v5：改为 warning-only |
