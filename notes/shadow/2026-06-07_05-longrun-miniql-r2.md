---
name: longrun-miniql-r2
description: yansh miniQL R2 结果 — 三条技术负债修复后（symbol_contract+震荡+否定约束）重跑 6644 行产物
metadata:
  type: project
---

# miniQL R2 基准（技术负债修复后）

**日期**：2026-06-07  **对应负债修复**：commit 658cccf

## 测试环境问题（本次跑法教训）

使用 shell `python -m main ... 2>&1 &` 后台启动方式存在严重问题：
- 多个并发 yansh 进程写入同一工作区 → 产物文件来自不同进程、不同 symbol_contract → 必然 import 不对齐
- 应使用 Python `subprocess.run(capture_output=False)` 单进程串行运行
- 有效 R2 数据取自 `20260607-170302-480898.jsonl`（17:03 时间戳，19文件完整 run）

## 有效 R2 数据（17:03 单进程 run）

| 项 | 值 |
|---|---|
| 文件数 | **19 个 .py 文件** |
| cost | **$9.57** |
| attempts | 6/6 耗尽 |
| yansh 自写 pytest | 105 items / 3 collection errors |
| 黑盒验收 | **0/10**（较 R1 2/10 更差） |

注：R1 的 2/10 是手工修复 `Catalog().load_dir()` 后才通过的，原始 R1 产物也是 0/10。

## 黑盒失败根因（R2）

| 错误 | 类型 |
|---|---|
| `from miniql.analyzer import analyze` — `analyzer.py` 只有 `Analyzer` 类无 `analyze` 函数 | 模块级函数名 import 不对齐 |
| `from .errors import RuntimeError` — `errors.py` 定义 `RuntimeError_` | 类名 import 不对齐 |

这两处与 R1 的 `evaluator.eval` → `ExprEvaluator.evaluate` 同属**跨模块符号名不对齐**，但发生在不同位置。

## 与 R1 对比

| 维度 | R1 | R2 |
|---|---|---|
| symbol_contract 覆盖 | 枚举成员 + dataclass 字段 | 同 + 方法名 |
| 黑盒通过（原始） | 0/10 | 0/10 |
| 黑盒通过（手工修后） | 2/10 | 未手工修 |
| 主要 import 错误 | evaluator.eval | analyze, RuntimeError |
| fix loop 轨迹 | 震荡 1↔82 | 105 items / 3 col. errors（未收敛） |
| cost | $6.04 | $9.57 |

## 新暴露的 yansh 能力缺口

**symbol_contract 还未覆盖模块级函数名**（`analyze`, `build_logical_plan`, `tokenize` 等跨模块调用的顶层函数）

- 现有覆盖：模块级 class/function 导出名（import 名）+ 枚举成员 + dataclass 字段 + 方法名
- 缺口：当 architect 把某个跨模块入口命名为 `analyze()`（函数），但 coder 实现为 `Analyzer` 类时，contract 未指定，coder 自行决定 → import 不对齐
- 修法方向：`_ARCHITECT_ROLE` 加强：**symbol_contract 里的 module exports 必须精确列出跨模块调用的函数名**（不只是类名）。这已在现有 schema 里可以做，只是 architect 没有充分利用。

## 下一步方向

1. 加强 `_ARCHITECT_ROLE` 提示：强调 symbol_contract 的 exports 必须包含**所有**被跨文件 import 的顶层函数名
2. 考虑在 fix loop 前主动调用 `python -c "import miniql"` 检测 import 是否自洽（作为额外质量门）
3. 重新测试 R3（在修完上述 prompt 后）
