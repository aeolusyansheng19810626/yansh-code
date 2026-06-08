---
name: longrun-miniql-r9-opus
description: yansh miniQL R9 换 opus-4.8 — 暴露 3 个 sonnet-tuned 框架参数对 opus 不适配（非能力问题）
metadata:
  type: project
---

# miniQL R9：换 opus-4.8 coder 验证「是否模型能力瓶颈」

**日期**：2026-06-08  **目的**：R8 元判断「瓶颈已转向 LLM 代码质量」→ 换 opus-4.8 跑，验证是否模型能力卡点。

**配置改动**（实验性，跑完待定去留）：
- `config.py`：`TIER_TOP=CLAUDE_OPUS`、`CLAUDE_OPUS="claude-opus-4-8"`、价格表补 opus-4-8、`coder_no_progress_rounds` 4→8
- `llm_client.py`：`LLM_MAX_TOKENS=16384`，两处 kwargs 注入（**通用 bug 修复，应保留**）

opus-4.8 走 `/ica/v1` OpenAI 协议（id `claude-opus-4-8`），实测可用。

## 核心结论：暴露的全是「框架对 sonnet 过拟合」，没有一个是 opus 能力问题

换 opus 后连撞 3 个 sonnet-tuned 参数，opus 代码能力其实够。

### 不适配点 1：`max_tokens` 缺省 → ICA 默认 8192 截断 opus 长 plan（已修）

- `llm_client.py` 的 kwargs **从不设 max_tokens** → ICA 网关用默认 8192。
- sonnet 的 plan JSON 碰巧 <8192 闭合，所以 R1–R8 没暴露；opus plan 更详细（成员级 symbol_contract、19 文件）撞 8192 截断 → JSON 解析失败 → `plan=[]` → coder「处理 0 个文件」→ 无测试 →「测试通过」**假绿 exit 0**。
- 修复：加 `LLM_MAX_TOKENS=16384` 常量，两处 kwargs 注入。修后 opus 写出完整 19 文件 plan ✓。
- **这是通用 bug**（任何模型 plan 超 8192 都中招），应永久保留。

### 不适配点 2：`coder_no_progress_rounds=4`（→8 仍不够）误杀 opus 探索

- 熔断判定（agent.py:2696）：仅 `write_file/replace_in_file/append_to_file/replace_symbol` 成功才重置；**read / git / bash / delete 都算「无进展」**。
- opus 工作模式：写复杂文件前先探索（`git status`、写 `CHECK.txt`/`does-not-exist-check.txt` 探测、`cat`/read 已写模块看接口）。sonnet 直接梭哈写，opus 谨慎先理解全局。
- 4 轮 → executor/optimizer/expression 三个执行层全熔断（一行没写）。
- 调 8 → **expression 写出来了**（还跑 `py_compile` 自验证），但 **executor 仍 8 轮熔断**。

### 不适配点 3：第一轮强制 write_file **不限定 path**（治本要改这里）

- agent.py:2629：新建文件第一轮 `tool_choice` 强制 `write_file`，但**只约束「调 write_file 工具」，不约束「写哪个文件」**。
- opus 被强制 write_file，却写了 `CHECK.txt` / 覆盖已完成的 `logical_plan.py`（满足强制但目标文件没写）→ 后续 auto 轮纯 read 探索 → 熔断。

## opus 实际能力（关键：够）

| 文件 | 结果 |
|---|---|
| 10 个模块（errors/tokens/types/lexer/ast_nodes/parser/catalog/analyzer/logical_plan/expression） | ✅ 全写对，expression 还自跑 py_compile |
| optimizer.py | ❌ 熔断（第一轮写偏成覆盖 logical_plan） |
| executor.py（依赖最重：Volcano 算子，依赖 logical_plan/expression/catalog/types 全部） | ❌ 8 轮纯探索从不下笔，熔断 |

- 黑盒 R9（no_progress=4 版）：**0/10**，根因单一 = `ModuleNotFoundError: No module named 'miniql.executor'`（executor 没生成，连锁 optimizer/expression 也曾缺）。**不是逻辑写错，是文件没落地**。
- cost：R9 **$21.0**（opus，input 1.08M/output 63.6K），约 sonnet R7 $9.9 的 2×；duration 760s。

## executor 卡点本质（为何调数值无解）

executor 是依赖最重文件，opus 想读全所有依赖接口再下笔，但：
1. 强制 write_file 不限定 path → opus 第一轮写偏（CHECK.txt）
2. yansh read 缓存「跳过实读」（命中即返旧内容）→ opus 重读依赖拿不到新信息，陷探索循环不敢写
3. no_progress 把纯探索判为空转 → 任何固定上限都可能不够（且调高浪费简单文件 cost）

→ 单纯调高 no_progress 不治本，需改 coder 调度策略本身。

## 修正 R8 元判断

R8 说「瓶颈已从框架机制转向 LLM 代码质量」**不完全对**。换大模型反而暴露：yansh 的 coder 调度（max_tokens / no_progress / 强制写不限 path / read 缓存）**全是按 sonnet 行为调的隐藏参数**，对 opus 的「先探索后下笔」式行为不适配。opus 能力够（10/11 模块写对），卡在框架适配，不是代码质量。

## 待决策修复方向（每次 opus 重跑约 $20）

1. **第一轮强制写目标文件**：强制 write_file 后校验 path==目标文件，写偏则丢弃+重注入「必须写 {filename}」。最对症 #3。
2. **read 探索不计入 no_progress**：只读/探测轮不累加，加独立「连续 K 轮无 write_file 调用」更高上限兜底空转。对症 #2。
3. **prompt 引导**：新建文件提示「直接 write_file 输出完整文件，symbol_contract 已给全部跨文件签名，禁止先 cat/读其他模块/写探测文件」。最轻量。
4. **read 缓存对 coder 放开**：让 coder 的 read 真实返回（不"跳过实读"），消除 opus 信息焦虑。影响面大。
5. 接受现状：opus 能力够，框架适配 opus 需额外投入，先切回 sonnet。

倾向 3+1 组合（轻量 prompt 引导 + 强制写目标 path），一次跑验证。
