---
name: longrun-miniql-r8
description: yansh miniQL R8 — arity检测+震荡熔断修复后重跑，暴露元问题（瓶颈转向LLM代码质量）
metadata:
  type: project
---

# miniQL R8 基准（裸函数 arity 检测 + 震荡熔断修复后）

**日期**：2026-06-08  **对应修复**：commit 01a42bf  **max-cost**：默认 $50

## 结果

| 项 | 值 |
|---|---|
| cost | $17.01（R6 $51 / R7 $9.9） |
| input/output | 5.90M / 107K，ratio 55:1 |
| attempts | 6/6 用尽 |
| 黑盒验收 | 0/10 |

## R8 改动是否生效

| 改动 | 触发情况 | 原因 |
|---|---|---|
| 裸函数 arity 检测 | **0 次** | 见下「治本组失效」 |
| 震荡警告注入 | 0 次 | 这次 build_logical_plan 调用本来就对，无 91↔1 横跳可拦 |

### 治本组失效（设计缺陷）

裸函数 arity 检测依赖 architect 在 symbol_contract 写 `{"params": [...]}`，但实测 **契约里 params: False**——architect 只在 description 写自然语言 `build_logical_plan(stmt, catalog)`，没用结构化 params 字段。schema 把 params 设成"建议"，architect 没采纳。

**正确方向（R9 候选）**：arity 真值源应是**函数定义本身**（AST 可直接提取 `def build_logical_plan(stmt, catalog)`），不该依赖 architect 在契约声明。应做独立的 `_scan_function_arity_mismatches`：扫所有 plan .py 的 FunctionDef 建 `{func: required_arity}` → 比对所有调用点 → 不一致报。完全不依赖契约，自动覆盖。

## 错误演进 + 元问题（关键）

| 轮 | 黑盒主错 | 层次 |
|---|---|---|
| R5 | build_logical_plan 少参 | 接口 |
| R6 | TableRef tuple 当 key | 逻辑 |
| R7 | build_logical_plan 少参（震荡） | 接口 |
| R8 | `Token.isdigit`（_peek 返回 Token 非 char） | 逻辑 |

**这次 build_logical_plan 调用是对的（2 参一致）**——R5/R7 错、R8 对，纯属 LLM 轨迹方差。每轮 coder 生成不同代码，bug 落在不同位置。

### 元判断：瓶颈已从「框架机制」转移到「LLM 代码质量本身」

- **R1–R5 接口对齐**（打地鼠）→ R6 端到端 smoke test 治本，接口死结终结 ✓
- **费用** → fix loop compact 修复 ✓
- **R7–R8 暴露的是逻辑 bug**（tuple key / Token.isdigit / 双重错误前缀）——这些需要 fixer **真正理解代码语义**去修，是 coder 生成质量 + fixer 修复能力的上限，**不是某个可静态检测的接口模式**。

smoke test 解决了「接口对不上」（能跑通调用链），但解决不了「逻辑写错」。继续针对 Token.isdigit 这类加检测 = 在更深的逻辑层打地鼠，价值递减——因为每轮 bug 位置随轨迹随机。

## 进展正面信号

- 失败数不再 91↔1 剧烈横跳（稳定 61）——可能止损组（去 prev_changed 依赖）有帮助，也可能本轮本就无 arity 震荡。
- fixer 深入真实入口（__main__.py 提及 27 次）——smoke 机制持续生效。
- 错误层次整体下沉（接口 → 逻辑），说明浅层问题在被逐步清除。

## 待决策方向

1. **arity 检测改为从定义 AST 提取**（不依赖契约）——正确但只覆盖 arity 一类，对 Token.isdigit 类逻辑 bug 无效。
2. **接受瓶颈是 LLM 代码质量**：miniQL（7 层 SQL 引擎、6000+ 行）对 sonnet coder 可能本就偏难；逻辑 bug 的修复依赖模型能力，框架侧能做的边际收益下降。
3. 换更小/更聚焦的 AB 任务验证框架改进，而非死磕 miniQL 这个高难度样例。
