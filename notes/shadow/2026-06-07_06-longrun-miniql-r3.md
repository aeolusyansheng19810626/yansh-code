---
name: longrun-miniql-r3
description: yansh miniQL R3 结果 — 绝对import检测+契约导出反向校验修复后重跑
metadata:
  type: project
---

# miniQL R3 基准

**日期**：2026-06-07  **对应修复**：commit 610d229

## 结果

| 项 | 值 |
|---|---|
| 文件数 | 20 个 .py 文件 |
| cost | $5.79 |
| attempts | 6/6 耗尽（early_exit success=True 虚假收尾） |
| yansh 自写 pytest | 43 failed, 200 passed |
| 黑盒验收 | **0/10** |

## 黑盒失败根因

```
Error[RuntimeError]: Catalog.load_csv() takes 2 positional arguments but 3 were given
```

- `catalog.py:136` 定义 `def load_csv(self, path: str)`（1参数）
- `__main__.py:245` 调用 `catalog.load_csv(table_name, csv_path)`（2参数）
- 方法签名参数数量不对齐，所有 10 用例统一崩溃

## R3 新机制未触发的原因

architect **没有生成 symbol_contract**（plan 是纯文件 list，无 `symbol_contract` 字段）
→ `_scan_contract_export_mismatches` 和绝对 import 检测均无契约可对比，形同虚设

## 与前轮对比

| 维度 | R1 | R2 | R3 |
|---|---|---|---|
| 黑盒（原始） | 0/10 | 0/10 | 0/10 |
| yansh 自写 pytest | — | 105/3 col.err | 43 failed, 200 passed |
| cost | $6.04 | $9.57 | $5.79 |
| 主要失败原因 | evaluator.eval | analyze / RuntimeError | load_csv 参数数量 |
| symbol_contract 生成 | 有（但漏函数名） | 有（但漏函数名） | **无**（architect 跳过） |

## 新暴露缺口

1. **architect 可以跳过 symbol_contract**：prompt 强调不够，≥3 新文件时 architect 仍可直接输出纯 plan list，无硬性校验
2. **方法签名（参数数量）不在契约中**：即便契约有 `methods: ["load_csv"]`，也不包含参数列表，无法检测参数数量不对齐

## R4 方向

**优先修法**：plan 解析后加强制校验——若 ≥3 新文件且无 symbol_contract，直接重试 architect（或注入强制要求）而非继续执行。
可行方案：`_parse_plan_with_status` 里若 plan_files ≥3 且 `symbol_contract` 缺失，将 plan 标记为 invalid，触发 _call_with_json_retry 重试，并在重试 prompt 中注入"你上一轮遗漏了 symbol_contract，这是 MANDATORY 字段"。

次优：contract 的 methods 条目扩展为含参数签名的 dict（但复杂度较高，且方法签名问题少于命名问题）。
