# P0 #1 实操验证：让 yansh 自己跑分层索引

承接 [./2026-05-22_11-hierarchical-symbol-index.md](./2026-05-22_11-hierarchical-symbol-index.md)。
笔记 _11 把分层索引代码改完后，直接拿 yansh-code 自身当 workspace 跑两次 audit 验证：

- 新默认（top 模式）注入是否真的不撑爆 prompt
- LLM 是否会主动调 `directory_summary` / `workspace_symbols(path=...)` 深挖
- audit 流程是否仍然顺畅

模型：claude-sonnet-4-6；workspace：yansh-code 自身；mode=audit。

## 实验 1：测试文件清单

**任务**：「tests/unit/ 目录下有哪些测试文件？其中 test_audit.py 都测了哪些功能？」

**LLM 选择**：

| 轮 | 工具调用 |
|---|---|
| 1 | `glob_files(pattern="tests/unit/*.py")` + `list_symbols(file_path="tests/unit/test_audit.py")` |
| 2 | `task_complete(success=true, summary="...")` |

duration 17.67s，attempts=0。

**观察**：
- LLM 完全没用 `directory_summary` / `workspace_symbols(path="tests/unit")`——选了它最熟悉的 `glob_files` + `list_symbols`
- 这其实**合理**——顶层注入已告诉它 `tests/ (23 py / 265 sym)`，glob_files 拿清单更直接
- 报告完整：10 个文件 + test_audit.py 的 19 个测试函数 + 覆盖功能域归类

## 实验 2：目录整体结构

**任务**：「给我看看 notes/shadow/ 目录的整体结构：文件数、扩展名分布、有哪些关键文件（README 之类的标识文件）、有没有子目录」

**LLM 选择**：

| 轮 | 工具调用 |
|---|---|
| 1 | `directory_summary(path="notes/shadow")` |
| 2 | `task_complete(success=true, summary="...")` |

duration 11.14s，attempts=0。

**观察**：
- LLM **一次调用 `directory_summary`** 拿到全部需要信息（文件数 11 / 扩展名 .md×11 / 子目录无 / 关键文件无）
- 任务描述跟新工具描述**高度匹配**——LLM 自然就选它
- 输出报告含表格 + 完整文件清单

## 关键结论

**新工具不是强迫 LLM 用，而是任务匹配时自然被选**：

- 实验 1：要"清单 + 符号"，glob_files+list_symbols 是最直接路径——LLM 没用新工具，**这是对的**
- 实验 2：要"目录整体感知（文件数/扩展名分布/关键文件/子目录）"，跟 directory_summary description 完美对齐——LLM 直接用，一轮搞定

这说明：

1. **顶层注入已经够用**——LLM 看到 `tests/ (23 py / 265 sym)` 这种摘要后能自主决策深挖路径
2. **新工具是补充不是替代**——保留 list_symbols / glob_files 等老路径不冲突
3. **prompt 不需要强行推销新工具**——工具 description 写清适用场景就够了

## 整体收益

| 维度 | 之前 | 现在 |
|---|---|---|
| audit 系统 prompt 注入体量 | 12,975 chars（全 40 文件全符号） | 3,314 chars（顶层 12 文件 + 2 子目录摘要） |
| 缩减 | — | **74.5%** |
| 大项目能不能跑 | 3000 文件直接撑爆 200K | 顶层 + 按需深挖，规模无关 |
| LLM 行为 | 看一眼整张符号表后扎进特定文件 | 看顶层 → 用合适工具深挖 |

## 评估

实操验证说明 P0 #1 改动**就是要做的事**——既不破坏现有可用工具（glob_files/list_symbols 还能用），又给"目录整体感知"这个之前没法直接表达的需求开了高效通道（directory_summary 一轮搞定）。

prompt 加固和 loop 兜底是 P0 #3 那波的方法论；这一波是**工具层的信息密度优化**——把"扫全树并塞 prompt"这种粗暴方式换成"按需取用 + 摘要先行"。两波合起来让 yansh 在大项目里真的可用。
