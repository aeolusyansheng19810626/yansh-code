---
name: longrun-miniql-r5
description: yansh miniQL R5 结果 — methods参数签名+arity检测修复后重跑
metadata:
  type: project
---

# miniQL R5 基准

**日期**：2026-06-07  **对应修复**：commit 302be25

## 结果

| 项 | 值 |
|---|---|
| 文件数 | 20 个 .py 文件 |
| cost | $30.71（R4 $27.80，持续上涨） |
| attempts | 6/6 耗尽 |
| 黑盒验收 | **1/10**（用例10 解析错误通过，+1 较 R4） |

## 黑盒失败根因（R5）

```
Error[RuntimeError]: build_logical_plan() missing 1 required positional argument: 'catalog'
```

- `logical_plan.py` 定义 `def build_logical_plan(resolved_or_stmt, catalog: Catalog)` — 2参数
- `__main__.py` 调用 `build_logical_plan(resolved)` — 只传了 1 个参数

## R5 新机制分析

**arity 检测未触发**：`build_logical_plan` 是模块级函数调用（`ast.Name(id='build_logical_plan')`），不是方法调用（`ast.Attribute`）。R5 的 `_scan_member_mismatches` arity 检测只覆盖 `obj.method()` 形式，直接函数调用被遗漏。

**fixer 做了尝试**：输出日志显示 fixer 多轮尝试修改 `build_logical_plan(resolved, catalog)`（2参正确调用），但最终 `replace_symbol` 操作把 `__main__.py` 的调用退回了 1 参版本。震荡未能收敛。

## 与前轮对比

| 维度 | R1 | R2 | R3 | R4 | R5 |
|---|---|---|---|---|---|
| 黑盒（原始） | 0/10 | 0/10 | 0/10 | 0/10 | **1/10** |
| symbol_contract | 有(漏fn) | 有(漏fn) | 无 | 有 | 有 |
| 主要失败原因 | evaluator.eval | analyze/RE | load_csv参数 | register_csv参数 | build_logical_plan参数 |
| cost | $6.04 | $9.57 | $5.79 | $27.80 | $30.71 |

## 新暴露缺口

**模块级函数 arity 检测盲区**：
- `from miniql.logical_plan import build_logical_plan` 后的直接调用 `build_logical_plan(resolved)` 不经过 `ast.Attribute` 节点
- 现有 `_scan_member_mismatches` 的 arity 检测只覆盖 `ast.Attribute`（`obj.method()`）
- 需要扩展到 `ast.Name`（直接函数调用）

## R6 方向

**主修法**：扩展 `_scan_import_mismatches` 或新增 `_scan_function_arity_mismatches`：
- 从 symbol_contract 提取模块级函数的参数数量（`{"build_logical_plan": {"params": ["stmt", "catalog"]}}`格式）
- 扫描调用文件中对这些函数的直接调用（`ast.Name` + `ast.Call`）
- 对比 arity 不符时报缺口

**同时解决**：plan schema 需要支持模块级函数带 params 信息（当前 `{}` 占位值不含参数信息）。

**或更实用方案**：在 fixer context 里注入"契约声明的跨模块函数签名"，让 fixer 知道应该用 2 参调用。
