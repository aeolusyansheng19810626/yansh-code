---
name: longrun-mini-test
description: yansh 长程生成测试（mini 解释器，~2000 行多文件）——首次运行结果与诊断
metadata:
  type: project
---

# 长程生成测试：mini 解释器

**日期**：2026-06-07

## 背景

用 yansh 一次性生成一个全新多文件项目，压测长程能力（费用熔断/compact/多文件协调）。
题材：小语言解释器 `mini`（tokenizer→parser→AST→interpreter→builtins，多模块 + pytest）。
题材选择理由：黑盒验证最干净（输入脚本 → 比对 stdout），self-contained 不依赖外部库。

材料位置：
- spec：`AB-test/longrun-mini/PROMPT.md`（3369 字符，钉死包名/入口/错误格式）
- 黑盒验收脚本：`AB-test/longrun-mini-accept/run_accept.py`（9 用例，独立于 yansh 自写测试）

## 运行参数

```
python -m main --cwd AB-test/longrun-mini --mode code --json "$PROMPT" --max-cost 50
```

## 结果

| 项 | 值 |
|---|---|
| 产物 | 14 文件 / 2936 行（源码 mini/ 1690 行 + tests/ 1246 行） |
| cost | $2.90（未撞 $50 熔断） |
| tokens | input 753K / output 43K（全 sonnet-4-6，haiku 仅 550/4 tokens） |
| 耗时 | 550 秒（~9 分钟） |
| fix loop | 3/3 耗尽 → success=False |
| 黑盒验收（run_accept.py） | **0/9**（入口就崩，一行用户代码未执行） |
| yansh 自写 pytest | collection error，0 tests ran |

## 根因分析

### 直接原因：6 处跨模块符号名不对齐

interpreter.py 的 import 引用了 ast_nodes/builtins 中不存在的名字：

| interpreter.py 导入名 | ast_nodes.py 实际名 |
|---|---|
| LetStmt | LetDecl |
| AssignStmt | Assignment |
| BlockStmt | Block |
| BinaryExpr | BinaryOp |
| UnaryExpr | UnaryOp |
| BUILTINS（from .builtins） | 未定义（无模块级变量） |

Python import 级联报错：每次 pytest collection 只暴露第一个 import 错误，fix 改完后下一个才浮现。fix loop 3 轮只消了 2 个（`LetStmt` → 修后又暴露 `MiniSyntaxError`），还剩 6 处时轮次耗尽。

### 深层原因：无全局符号契约

coder 分文件生成时，parser.py 和 interpreter.py 引用的 AST 节点名字，与 ast_nodes.py 实际定义的名字不一致。多文件长程任务的典型缺陷——每个文件单独生成时 LLM 用了不同的命名习惯（`XxxStmt` vs `XxxDecl`，`BinaryExpr` vs `BinaryOp`），没有全局约束保持一致。

### fix loop 轮次瓶颈

- **coder_max_rounds_per_file**（R2 改 20→40）是 Coder 生成阶段的参数
- **fix loop max_attempts = 3** 是另一个参数，R2 未动
- 6 个 import 缺口至少需要 6 轮（每轮一个）；3 轮根本不够
- 改名只是简单的 `replace_in_file`，不复杂——只是没有足够轮次

## 未触发的熔断机制

| 机制 | 状态 |
|---|---|
| $50 费用熔断 | 未触发（$2.90） |
| 无进展熔断（连续 4 轮无有效编辑） | 未触发（fix 每轮都在改） |
| compact（80K tokens 阈值） | 未触发（753K input 但分布在多轮） |

## 技术负债（新暴露）

**fix loop max_attempts 太低（3 轮不够应对多文件 import 级联错误）**

- 现状：`run()` 中 `max_attempts` 写死为 3
- 问题：多文件项目 import 级联 N 个错误，fix 每轮消一个，需要 N 轮。3 轮对 6 个缺口完全不够
- 修法方向：① 提高 max_attempts（5-8）；② 或在 fix 阶段对"import error"做批量扫描一次性全修
- 优先级：中（仅多文件生成场景触发，单文件改动不受影响）

**coder 多文件生成缺全局符号契约**

- 现状：plan 生成 14 个文件列表，coder 逐文件生成，各文件的 AST 节点命名不一致
- 修法方向：plan 阶段在文件列表旁附 "关键跨模块符号名约定"（如 `LetDecl / Assignment / BinaryOp`），coder 开始每个文件前读取约定
- 优先级：中（多文件新建场景触发，改动类任务不受影响）

## 结论

代码体量（2936 行）和结构（14 文件覆盖完整 lexer→parser→AST→interpreter 链路）是对的，代码逻辑本身看起来写得不差（interpreter.py 394 行，有完整求值逻辑）。失败点是**最后 1 公里**——模块间 import 名字没对齐，导致整包无法 import，0/9 黑盒用例全挂。

如果手工修复 6 处 import 名（10 分钟的事），大概率黑盒用例能通过若干。但这是 yansh 该自己做的。

## 下一步可选方向

1. **修 fix loop max_attempts**（中优先级，1-2小时）
2. **修 plan 缺全局符号约定**（中优先级，半天）
3. **分阶段 spec 重跑**：先 core（token/lexer/parser/ast），再 eval（env/interpreter/builtins），再 integration（main/tests）——绕开符号对齐问题
4. 暂不修，把这两项加到技术负债
