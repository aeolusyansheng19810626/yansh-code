# AB Test #4：bug 复现 / 修复任务 — yansh code mode vs CC 子 agent

**bug 题目来源**：`d95c87d fix: P1-2 memory.find_memory 路径穿越（slugify + resolve 双校验）`

**复现方式**：reverse-apply `d95c87d` 的 `memory.py` 部分（保留 `tests/unit/test_memory.py` 的 4 条新测试不动）。bug 状态下：

```
$ python -m pytest tests/unit/test_memory.py
1 failed, 34 passed
FAILED test_find_memory_slugify_consistent_with_save
```

只有 1 条 `slugify_consistent` 失败；3 条 `path_traversal_*` 测试因为 workspace 没 `../../README.md` 等真实文件**碰巧也过了**——bug 本身的安全性问题没被任何失败 case 直接 PoC 出来。

**提示词**（两边一致核心要求）：
> tests/unit/test_memory.py 有测试失败，定位 bug 并修复

**模型**：Claude Sonnet 4.6（两侧主模型）；yansh 子 agent 走 haiku
**日期**：2026-05-23
**任务类型**：bug 定位 + 修复（含失败测试 + 测试名暗示安全意图）

## 数据对比

| 维度 | yansh (code mode) | Claude Code 子 agent (general-purpose) |
|---|---|---|
| 用时 | **87.9s** | **31.3s** |
| 工具调用 | **24** | **6** |
| Token (in+out) | **249K** (sonnet 226K + haiku 23K) | **63K** |
| attempts | 1（test fix loop 一次过）| 1 |
| 文件改动 | `memory.py`（1 处加 `slugify`） | `memory.py`（1 处加 `slugify`） |
| 测试结果 | 35/35 pass | 35/35 pass |
| 修法深度 | ⚠ 只补 slugify（缺 resolve 双校验） | ⚠ 只补 slugify（缺 resolve 双校验） |

**yansh ≈ 2.8× CC 用时、4× 工具、4× tokens，结论修法完全相同** —— 这次 CC 完胜（速度 + 成本占优，质量打平）。

## 修法对比 vs baseline

baseline `d95c87d` 修法（标准答案）：
```python
def find_memory(name, workspace_dir=None):
    """...P1 安全：name 必须先 _slugify... resolve() + is_relative_to 再校验一次..."""
    name = str(name).strip()
    if not name:
        return None
    slug = _slugify(name)                          # ← 加 1：slugify
    for d, scope in (...):
        if d is None or not d.exists():
            continue
        f = d / f"{slug}.md"                       # ← 用 slug
        try:                                       # ← 加 2：resolve 双校验
            f_resolved = f.resolve()
            d_resolved = d.resolve()
            if not str(f_resolved).startswith(str(d_resolved)):
                continue
        except Exception:
            continue
        if f.exists():
            return parse_memory_file(...)
```

**两边 agent 都只补了第 1 层（slugify）**，没补第 2 层（resolve 双校验）。两边修法字面对齐：

```python
# yansh 和 CC 都改成：
+    slug = _slugify(name)
     for d, scope in (...):
         ...
-        f = d / f"{name}.md"
+        f = d / f"{slug}.md"
```

**为什么都漏了 resolve 双校验**？
- bug 状态下只有 1 条测试失败（`slugify_consistent`），3 条 `path_traversal_*` 都过了
- 加了 slugify 后，所有 35 条测试都过——LLM 没有任何信号要继续深挖
- 测试名 `test_find_memory_path_traversal_blocked_dotdot` 暗示了"path traversal 安全"，但既然测试已经过了，LLM 不会过度修改

这暴露了**测试驱动开发的死角**：测试覆盖力 < 安全意图时，LLM 只修到测试满意为止，不会主动加 defense-in-depth。

## yansh 的 24 个工具调用分布

| 工具 | 次数 | 用途 |
|---|---|---|
| `execute_command` | 10 | 跑 pytest / 验证测试 / 多次重跑 |
| `get_symbol_definition` | 5 | 看 `find_memory` / `save_memory` / `_slugify` 等符号定义 |
| `read_file` | 3 | memory.py / test_memory.py 上下文 |
| `task_complete` | 3 | architect/coder/tester 各一次 sentinel |
| `dispatch_subagent` | 1 | 派 haiku 子 agent 探查相关函数（23K haiku tokens） |
| `list_symbols` | 1 | memory.py 符号清单 |
| `replace_in_file` | 1 | 实际修改的一次 edit |

**关键观察**：yansh 在 `code mode` 下仍走了 plan 步骤（`[Agent: Architect]`，bug 题目轻量但走完整流水线）。`P1.3 fix loop scope` 工作正常——`pytest tests/unit/test_memory.py` 命中相关测试，linter attempt 1 早退（识别 218 条 ruff 错误为 pre-existing），test attempt 1 修复后直接 35 pass，**没出现任务 #2 那样的弱化断言行为**。修法稳定。

## CC 的 6 个工具调用

CC 报告的：Read 1 + Grep 1 + Edit 1 + Bash 3。

CC 路径极短：grep `find_memory` 和 `save_memory` → 看出 save 用 slugify、find 没用 → Edit → 跑 pytest 验证。**单线程串行，无 plan / no subagent**。

## 完成质量差异：本次基本无差

| 维度 | yansh | CC |
|---|---|---|
| 测试通过 | ✓ | ✓ |
| 修法字面 | 一致 | 一致 |
| docstring | 删了原 docstring 里的"P1 安全"说明 | 同样删了 |
| 触及 resolve 双校验 | ✗ | ✗ |
| 触及 `_slugify` 函数本身 | ✗（保持 ASCII-safe 行为） | ✗ |
| 副带改动 | 0 | 0 |

**这次没有 task #2 那种"yansh 闭环更深"的差异**——任务太局部，LLM 看到 1 条失败 → 加一行 → 通过 → 收尾。两边路径几乎同构。

## 这次 task 的特殊性

跟 task #1-3 不同：

- **task #1**（探索）：CC 路径短赢、yansh 输出深赢
- **task #2**（写代码 + 加测）：yansh 闭环深 + 顺手清理 vs CC 25× 便宜
- **task #3**（架构论证）：yansh 派 subagent 拆解 + 行号准 vs CC 抓 hidden trap
- **task #4**（bug 修复）：**两边修法字面相同，CC 4× 便宜**

**bug 修复任务（含失败测试）反而是 yansh 价值最薄的场景**：
- 失败测试已经把 bug 定位窗口压到极小（不需要全 repo 探索）
- "改动小 + 测试驱动验证"是 CC 的强项
- yansh 的 plan→code→fix 流水线在这种小改动上是 overhead

## 共同盲点：defense-in-depth

两边都漏了 resolve 双校验。这不是模型能力问题，是**测试驱动信号的极限**：

- 失败测试只 1 条（`slugify_consistent`）
- 通过测试 34 条，包括 3 条名字带 `path_traversal_blocked` 但实际不会触发的 case
- LLM 看到 "1 fail → fix → 35 pass" 的明确闭环就停手

要让 LLM 加 resolve 防护，prompt 需要：
- (a) 显式说"加 defense-in-depth 安全防护"
- (b) 或者把 path_traversal 测试改成真 PoC（如 `os.symlink` 制造跨目录、resolve 后落盘外）

baseline d95c87d 是人类 + Codex review 的产物，写注释明确"留一层防御不亏"——这种"超出测试要求的工程审美"暂时是 LLM 弱项。

## 总结：什么场景选什么（更新）

| 任务类型 | 推荐 | 倍率 |
|---|---|---|
| 探索 / 信息检索（task #1）| **CC** | yansh ≈ 1.5× |
| 写代码 + 加测（task #2）| **CC**（25× 便宜） | yansh ≈ 25× |
| 完整功能落地 + 文档清理 | **yansh** | 多花但语义闭环 |
| 架构论证 / 纯只读分析（task #3） | **看深度需求** | yansh ≈ 4× |
| **bug 修复（含失败测试，task #4）** | **CC**（4× 便宜，修法相同） | **yansh ≈ 4×** |
| 不熟悉的代码库 | **yansh**（plan/audit 强制只读，更安全） | — |

**新结论**：bug 修复（特别是有失败测试做信号锚点的）选 CC——yansh 的 plan 流水线是浪费。yansh 的相对优势出现在**没有明确 fail-signal、需要全 repo 探索 / 派子 agent / 多文件配套改动**的任务里。

## 数据文件

- `20260523_task4_yansh.json` — yansh batch JSON（含 baseline diff prelude + 末尾 task_log JSON 行）
- `20260523_task4_yansh_stderr.log` — yansh stderr 完整跑测过程
- CC subagent transcript：在父对话里，未单独保存

## 下一步

- task #5 跨文件重构（改广泛使用的函数签名 + 全 repo 适配，看 yansh 的 plan→code→fix vs CC 的"读 + 改 + 验证"循环）— **预计 yansh 在这种场景反弹**，因为多文件配套是 yansh 强项
- 综合 4 次 AB 整合 README
