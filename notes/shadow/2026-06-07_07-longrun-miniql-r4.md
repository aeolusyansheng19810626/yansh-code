---
name: longrun-miniql-r4
description: yansh miniQL R4 结果 — 强制 symbol_contract 生成后重跑
metadata:
  type: project
---

# miniQL R4 基准

**日期**：2026-06-07  **对应修复**：commit 1892442

## 结果

| 项 | 值 |
|---|---|
| 文件数 | 20 个 .py 文件 |
| cost | **$27.80**（R3 $5.79，大幅上涨） |
| attempts | 6/6 耗尽 |
| yansh 自写 pytest | 57 failed, 175 passed（较 R3 退步） |
| 黑盒验收 | **0/10** |

## 关键进展

**R4 的 symbol_contract 强制生成机制有效**：
- retry 未触发（无 `[plan] JSON 解析失败，自动 retry` 消息）= architect 首轮就生成了 symbol_contract
- coder task_complete 摘要中明确提到"按 symbol_contract 精确定义"
- $27.80 费用来自 fix loop 6 轮耗尽（非 retry 导致）

## 黑盒失败根因（R4）

```
Error[RuntimeError]: Catalog.register_csv() takes 2 positional arguments but 3 were given
```

- `catalog.py`: `def register_csv(self, path: str)` — 只接受 1 个参数
- `__main__.py`: `catalog.register_csv(table_name, csv_path)` — 传了 2 个参数
- **方法参数数量不对齐**（与 R3 `load_csv` 同类，方法名不同）

## 与前轮对比

| 维度 | R1 | R2 | R3 | R4 |
|---|---|---|---|---|
| 黑盒（原始） | 0/10 | 0/10 | 0/10 | 0/10 |
| symbol_contract 生成 | 有（漏函数名） | 有（漏函数名） | **无** | **有** |
| 主要失败原因 | evaluator.eval | analyze/RuntimeError | load_csv 参数 | register_csv 参数 |
| cost | $6.04 | $9.57 | $5.79 | **$27.80** |
| yansh 自写 pytest | — | 105 err | 43f/200p | 57f/175p |

## 新暴露缺口

**methods 契约只有方法名，无参数签名**：
- 当前 symbol_contract 的 methods 字段：`["register_csv", "query", ...]`（只列名字）
- 方法名正确但参数数量/顺序不一致时，无法在契约层面检测到
- 根因：architect 定义接口为 `register_csv(self, path)`，调用端假设为 `register_csv(self, table_name, path)`

## R5 方向

**主修法**：methods 字段扩展为含参数签名的格式：
```json
"Catalog": {
  "methods": [
    {"name": "register_csv", "params": ["table_name", "path"]},
    {"name": "query", "params": ["sql"]}
  ]
}
```

同步更新：
1. `_ARCHITECT_ROLE`：methods 条目需附带参数名列表
2. plan schema：methods 示例改为含 params
3. `_render_contract`：渲染时显示方法签名（让 coder/fixer 看到完整接口）
4. `_scan_member_mismatches`：methods case 对比调用端参数数量 vs 定义端

注：这是最常见的跨文件接口不对齐之一。
