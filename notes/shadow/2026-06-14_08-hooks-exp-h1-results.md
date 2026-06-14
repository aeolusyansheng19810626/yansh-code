# H1 实验结果：UserPromptSubmit TDD 注入

参考计划：[./2026-06-14_01-hooks-exp-plan.md](./2026-06-14_01-hooks-exp-plan.md)

## 配置

| 项 | 值 |
|---|---|
| 任务 | 实现中缀表达式求值器 `eval_expr(expr) -> float` |
| 模型 | claude-sonnet-4-6 |
| mode | solo，SOLO_TEST_ENFORCEMENT=off |
| hook | UserPromptSubmit → tdd_inject.py → 注入 TDD 约束 system_message |
| 对照组 | 无 hook，其余完全相同 |

## 排查过程

**第一次跑（失败）**：hook 脚本打印中文时 `UnicodeEncodeError: 'cp932'`，被 yansh 静默吞掉，hook 未注入。
**修复**：tdd_inject.py 头部加 `sys.stdout.reconfigure(encoding="utf-8")`，重跑。

## TDD 分析结果

| | **Hook 组（treatment）** | **Baseline（无 hook）** |
|---|---|---|
| tdd_complied | **✅ True** | **❌ False** |
| reason | 测试先于实现 | 实现先于测试 |
| first_test_idx | 1（tool_calls[1]）| 5（tool_calls[5]）|
| first_impl_idx | 3（tool_calls[3]）| 0（tool_calls[0]）|
| 黑盒验收 | 16/16 ✅ | 16/16 ✅ |
| 总轮次 | 10 | 10 |
| total_tool_calls | 10 | 10 |

## 执行轨迹对比

**Hook 组（TDD 遵守）**：
```
tool[0]: execute_command("mkdir tests")     ← 准备目录
tool[1]: write_file(tests/test_expr_eval.py) ← 先写测试 ✅
         → pytest 立即跑 → ModuleNotFoundError: No module named 'expr_eval'（红灯）
tool[3]: write_file(expr_eval.py)            ← 再写实现
tool[6]: write_file(_quick_test.py)          ← 额外验证脚本
task_complete summary: "TDD 流程完成：先写 tests/test_expr_eval.py（48 个断言，红灯），
                        再实现 expr_eval.py（递归下降解析器）"
```

**Baseline（违反 TDD）**：
```
tool[0]: write_file(expr_eval.py)            ← 直接写实现 ❌
tool[2]: write_file(_verify_basic.py)        ← 内部验证脚本（非标准测试）
tool[5]: write_file(tests/test_expr_eval.py) ← 测试后补
tool[6]: write_file(tests/test_smoke.py)
task_complete summary: "在 expr_eval.py 中实现了 eval_expr..."（无 TDD 字眼）
```

## 核心发现

### H1 假设成立 ✅

UserPromptSubmit hook 注入的 TDD 约束**有效改变了 agent 的工具序列**：
- Hook 组：先建测试骨架（红灯）→ 后写实现（绿灯）
- Baseline：直接写实现 → 测试后补（或不补）

**单次实验 TDD 遵守率：hook 100%（1/1）vs baseline 0%（0/1）**

### 注入机制验证

Hook 触发后，requirement 头部会被加上 `[hook 注入] 本次为编码任务，采用测试驱动开发...`，
LLM 将其作为强约束。Agent 的 task_complete summary 中明确提到"TDD 流程"，
说明 LLM 理解并执行了注入的约束。

### 质量无损

两组 `run_accept.py` 均 16/16 通过——TDD 顺序不影响最终代码质量。

## 已知局限

- 单次实验，有 LLM 采样随机性，需 N≥3 才有统计显著性
- baseline 自带了比较充分的测试（49 用例），说明 sonnet 默认会写测试，只是**顺序不同**
- H1 验证的是"TDD 顺序（先测后实现）"而非"测试覆盖率"

## H1 vs H2 对比

| 维度 | H1（UserPromptSubmit）| H2（PostToolUse）|
|---|---|---|
| hook 注入时机 | 任务开始前 | 每次写文件后 |
| 是否改变 agent 行为 | **是（顺序改变）** | 是（策略从批量→增量）|
| 是否提供必要信息 | **是（LLM 不知需先写测试）** | 否（静态分析够用）|
| 实验效果 | **清晰对比** | 模糊（需复杂场景）|

**结论：UserPromptSubmit hook 是目前最有力的行为干预工具——在任务起点注入约束，
效果直接且可量化。**
