---
name: longrun-miniql-r1
description: yansh 长程生成测试 R1 — miniQL 内存 SQL 引擎（5000+行，19文件）首次运行结果与分析
metadata:
  type: project
---

# 长程生成测试：miniQL 内存 SQL 查询引擎

**日期**：2026-06-07  **运行编号**：R1（首次）

## 背景

继 mini 解释器（3536行，9/9 黑盒通过）后，升级为更复杂的 7 层管线项目：
词法 → 语法 → 语义分析 → 逻辑计划 → 优化器 → 物理执行 → 格式化。
验收要求：10 个黑盒用例，覆盖三值逻辑/NULL聚合/LEFT JOIN/GROUP BY/HAVING/子查询/EXPLAIN 谓词下推/错误分类。

材料位置：
- spec：`AB-test/longrun-miniql/PROMPT.md`
- 黑盒验收脚本：`AB-test/longrun-miniql-accept/run_accept.py`（10 用例）

## 运行参数

`python -m main --cwd AB-test/longrun-miniql --mode code --max-cost 100 --json "$PROMPT"`

## 产物数据

| 项 | 值 |
|---|---|
| 文件数 | 20 个 .py 文件 |
| 总行数 | **6644 行**（目标 ≥5000，超出） |
| cost | **$6.04**（预算 $100，远未熔断） |
| 耗时 | 1248 秒（~21 分钟） |
| tokens | input 1,550K / output 93K |
| fix attempts | 6/6 耗尽 |
| yansh 自写 pytest | 156 passed（最好轮次） |
| 黑盒验收 | **2/10 通过**（用例 9/10：语义错误+解析错误） |

## fix loop 轨迹

```
首轮:  150 passed / 88 failed   ← import 全自洽，首轮直接跑功能测试（好兆头）
尝试1: 1 failed                 ← 极速收敛
尝试2: 82 failed                ← 震荡（fixer 改坏某核心逻辑）
尝试3: 1 failed → 82 failed    ← 反复横跳
尝试4: 1 failed
尝试5: 124 failed → 1 failed   ← 反复
尝试6: 1 failed（耗尽）
```

## 黑盒验收结果（2/10）

| 用例 | 结果 | 错误 |
|---|---|---|
| 1 投影+过滤(三值逻辑) | ❌ | `ExprEvaluator` has no attribute `eval` |
| 2 INNER JOIN | ❌ | 同上 |
| 3 LEFT JOIN + NULL | ❌ | 同上 |
| 4 GROUP BY + AVG | ❌ | `COUNT(*)` ParseError（`*` 被当参数分隔符） |
| 5 HAVING | ❌ | 同上 |
| 6 ORDER BY + LIMIT | ❌ | `ExprEvaluator` has no attribute `eval` |
| 7 子查询 IN | ❌ | `COUNT(*)` ParseError |
| 8 EXPLAIN 谓词下推 | ❌ | 结构断言失败（Filter 节点存在但判断逻辑有问题） |
| 9 语义错误格式 | ✅ | — |
| 10 解析错误格式 | ✅ | — |

注：用例 8 EXPLAIN 实际输出了正确的计划树（Filter 在 HashInnerJoin 下，谓词下推生效），
但验收脚本结构断言判断有误（需检查断言逻辑）。

## 根因分析

### Bug 1：`Catalog().load_dir()` 返回值被丢弃（已手工修）
- `__main__.py` 写成 `catalog = Catalog(); catalog.load_dir(data)` 但 `load_dir` 是 classmethod，返回新对象
- 修法：`catalog = Catalog.load_dir(data)`
- 修后语义/解析错误用例从 0 → 2

### Bug 2：`ExprEvaluator` 方法名不对齐（核心问题，未修）
- `executor.py` 调用 `evaluator.eval(expr, row)`
- `expression.py` 中 `ExprEvaluator` 实际方法名可能是 `evaluate()` 或 `__call__()`
- 这是跨模块**方法名**不对齐，与 mini 解释器的 `TokenType.IDENT` 属同一类问题
- 根本原因：symbol_contract 覆盖了枚举成员名/dataclass 字段名，但**方法名**还未纳入契约

### Bug 3：`COUNT(*)` ParseError（parser 实现 bug，未修）
- parser 不能正确解析 `COUNT(*)`，把 `*` 当参数列表分隔符处理
- 属 parser 实现缺陷，与 symbol_contract 无关

### Bug 4：EXPLAIN 验收断言逻辑问题（验收脚本 bug，未修）
- 实际计划树已正确输出（Filter 节点在 Scan 之上、Join 之下）
- 验收脚本的 "Filter 行索引 < HashJoin 行索引" 断言逻辑有误
- 需修验收脚本而非产物

## 与 mini 解释器对比

| 维度 | mini 解释器 R3 | miniQL R1 |
|---|---|---|
| 总行数 | 3536 | **6644** |
| import 自洽（首轮） | ✅ | ✅（symbol_contract 生效） |
| 首轮 pytest passed | 76 | **150**（更好） |
| 黑盒通过率 | 9/9 | **2/10** |
| 主要阻塞 | 枚举成员名不对齐 | 方法名不对齐 + parser COUNT(*) bug |

## 暴露的 yansh 能力缺口（新技术负债）

1. **symbol_contract 未覆盖方法名**（中优先级）
   - 现状：覆盖枚举成员（`members`）和 dataclass 字段（`fields`），不覆盖类方法名
   - 影响：跨文件调用 `obj.method()` 时方法名不对齐，运行时 AttributeError
   - 修法方向：symbol_contract 增加 `methods` 字段；`_scan_member_mismatches` 扩展到方法调用 `node.attr()` 形式

2. **1 failed ↔ 100+ failed 震荡模式仍存在**（中优先级）
   - 6 轮预算里 1 failed 出现 5 次但始终无法清零
   - 根因：fixer 改一个文件后引入另一个文件的 regression，没有全局视角
   - 修法方向：fix loop 跑多个测试失败时，同时注入所有 _scan_member_mismatches 发现的缺口（而非只看当前报错）

## 下一步方向

1. 修 symbol_contract 覆盖方法名 → 重跑 miniQL R2
2. 修验收脚本 EXPLAIN 断言逻辑
3. 手工修 miniQL 产物 2 处 bug 验证"修好后能通过几个用例"（可选，仅评估产物质量）
