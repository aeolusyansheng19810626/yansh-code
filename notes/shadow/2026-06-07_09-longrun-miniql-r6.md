---
name: longrun-miniql-r6
description: yansh miniQL R6 结果 — 端到端自验证战略转向，机制验证成功
metadata:
  type: project
---

# miniQL R6 基准（战略转向：端到端自验证）

**日期**：2026-06-07  **对应修复**：commit 0bdf8a5

## 结果

| 项 | 值 |
|---|---|
| 文件数 | 20+ 个 .py（含 tests/test_smoke.py） |
| cost | **$51.31（触发 $50 熔断中断）** |
| attempts | 0（被费用熔断打断，非正常收尾） |
| duration | 1573s（26 min） |
| 黑盒验收 | 0/10 |

## 机制验证：成功 ✓

R6 三处改动全部按设计生效：

1. **A — architect 生成 smoke test ✓**：plan files 含 `tests/test_smoke.py`，且质量很高——通过 `subprocess.run([sys.executable, "-m", "miniql", ...])` 跑真实 CLI，有数据准备 fixture + 正常查询类 + 错误测试类，**不 import 内部函数**。
2. **B — test_command 含 smoke ✓**：`pytest tests/test_lexer.py ... tests/test_smoke.py`。
3. **C — fixer 被引向真实入口 ✓**（决定性）：

| 文件提及次数 | R5 | R6 |
|---|---|---|
| `__main__.py` | **0** | **11** |
| `test_smoke.py` | 0 | 22 |
| `analyzer.py` | — | 8 |

R5 fixer 6 轮 0 次读 `__main__.py`；R6 fixer 终于在真实入口路径上工作。

## 错误性质的根本变化

| 轮次 | 黑盒错误 | 性质 |
|---|---|---|
| R1 | evaluator.eval | 接口·方法名 |
| R2 | analyze 函数名 | 接口·import 名 |
| R3 | architect 跳过 contract | 接口·契约缺失 |
| R4 | register_csv 参数数量 | 接口·方法 arity |
| R5 | build_logical_plan 参数数量 | 接口·函数 arity |
| **R6** | **Table '('emp', None)' is not registered** | **业务逻辑 bug** |

R1–R5 全是**接口签名崩溃**（调用链对不上）。R6 第一次跨过接口层，CLI 调用链能跑通到语义分析阶段，暴露出真正的**业务逻辑 bug**：TableRef 用 `(name, alias)` tuple 当 key 去 catalog 查，但 catalog 用 str 注册。还有双重 `Error[SemanticError]:` 前缀等格式问题。

**结论：端到端 smoke test 打破了前 5 轮的接口签名死结。** 这是治本方向的验证——用真实 Python 执行替代 AST 静态模拟，fixer 终于看到真实失败信号。

## 黑盒仍 0/10 的原因（非机制问题）

1. **费用 $50 熔断**：fix loop 在逻辑 bug 修完前被中断（cost $51 > 默认 --max-cost $50）。
2. **剩余是深层逻辑 bug**：tuple-as-key、双重错误前缀——属 coder/fixer 的编码能力，不是框架机制缺陷。

## R7 方向（如继续）

1. **提高 --max-cost**（如 $80–100）重跑，看逻辑 bug 能否在熔断前被 smoke 驱动修完，验证黑盒分数能否突破。
2. 逻辑 bug 本身（tuple key、错误前缀重复）是 coder 代码质量问题，可观察 smoke 驱动下 fixer 能否自行收敛。
3. 费用效率：R6 $51 偏高，smoke test 每轮跑 subprocess 增加开销；可评估 fix loop 轮次/成本平衡。

## 关键判断

R6 不是"又一轮打地鼠"——它改变了 yansh 看待失败的方式（从内部单测信号 → 真实入口信号）。黑盒 0/10 的表象掩盖了机制层的成功：**接口对齐问题这条贯穿 R1–R5 的主线已被终结**，剩余是独立的逻辑/成本问题。
