---
name: longrun-mini-r3
description: yansh 长程生成修复 R3 — symbol_contract 扩展到类内部成员 + fixer 方向约束（2026-06-07）
metadata:
  type: project
---

# 长程生成修复 R3：成员级符号契约 + fixer 方向约束

**日期**：2026-06-07  **commit**：283a6a6（接续 d2afc99）

## 背景

第二次重跑 mini 解释器（13 文件 3040 行）仍 success=False，黑盒 0/9。
d2afc99 修复了 import 名对齐，但运行时崩在 `AttributeError: IDENT`：
- `token.py` 定义 `TokenType.IDENTIFIER`，`parser.py` 两处写了 `TokenType.IDENT`
- `ast_nodes.py` 定义 `LetDecl.value`/`IfStmt.cond`/`then_block`/`else_block`，`interpreter.py` 访问 `.initializer`/`.condition`/`.then_branch`/`.else_branch`
- fix loop 6 轮在两端震荡：某轮改 interpreter 对齐 ast_nodes，下轮又改 ast_nodes 对齐 interpreter，永远无法同时对齐两条轴

## 根因（双 opus 分析汇总）

symbol_contract 粒度不足：只覆盖「跨模块导出名（import 层）」，不覆盖「类内部成员名（运行时层）」。
fixer 无方向约束：把符号名当双向可改变量，来回选择对齐方向。

## 改动（283a6a6）

### plan schema 扩展
symbol_contract 的 value 从扁平 list 升级为成员级 dict（向后兼容 list）：
```json
{
  "mini/token.py": {"TokenType": {"members": ["IDENTIFIER", "INT", ...]}},
  "mini/ast_nodes.py": {"LetDecl": {"fields": ["name", "value"]}, "IfStmt": {"fields": ["cond", "then_block", "else_block"]}}
}
```

### _ARCHITECT_ROLE
新增：凡 Enum/dataclass 的成员/字段被另一文件访问，必须在契约里展开成员级登记。附真实失败案例（TokenType.IDENT vs IDENTIFIER）。

### _render_contract（模块级函数）
兼容旧 list 和新 dict 格式渲染，注入 coder sys_prompt。提为模块级便于测试。

### _scan_member_mismatches（新函数）
两类检查：
1. **枚举成员精确检查**：`TokenType.IDENT` → 契约 members 里无 IDENT → 报缺口，带权威名。绑定 node.value.id 为枚举类型名，不跨对象误报。
2. **dataclass 字段保守检查**：只在 error_info 中出现 `has no attribute 'xxx'` 时才激活对应同义词，+Load 上下文守卫。避免 `request.test`/`lexer.token` 等正常代码误报。

### fix() 方向约束
检测到成员引用缺口时，注入：「契约是唯一真值源，只允许把引用端改成契约名，严禁修改定义端（ast_nodes/token.py）」。直接掐断两端震荡。

## 过程

opus 根因分析 → opus 出实施计划（P0-A/B/C/D 两个原子对）→ 我实现 → 3 轮 opus review：
- R1：发现 dataclass 同义词无类型绑定会大量误报 → 改为 error_info 过滤保守模式
- R2：发现 _render_contract 内嵌不可测 → 提为模块级；补误报防守单测
- R3：通过（2 个 minor：残留低概率误报通道 + 无效 regex 已清理）

## 单测

81 个全过，新增 8 个行为断言（_render_contract 三格式 + _scan_member_mismatches 枚举/字段/无误报/C1防守）。

## 第三次重跑结果（验证通过）

| 指标 | 第1轮 | 第2轮 | 第3轮 |
|---|---|---|---|
| import 自洽 | ❌ | ✅ | ✅ |
| 首轮 pytest | 0 tests ran | 5 pass | **76 pass** |
| fix 有效轮次 | 3/3耗尽 | 6/6耗尽 | **2轮** |
| yansh 自写 pytest | 0/0 | 54/169 | **268/268 全过** |
| 黑盒验收 | 0/9 | 0/9 | **9/9 ✅** |
| cost | $2.90 | $7.45 | $6.60 |
| 产物 | 13文件 2936行 | 13文件 3040行 | 13文件 3536行 |

**达到验收要求**：黑盒 9/9 全过（算术/变量/控制流/递归/闭包/字符串/内置/错误格式）。
修复链路完整生效：成员级 symbol_contract → coder 第一次就用对名字 → fix 收敛快。
