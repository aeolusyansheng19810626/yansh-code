# 2026-05-21 yansh 在某项指标上首次反超 Claude Code subagent

## 背景

之前的三方公平对比（[fairness 笔记](./2026-05-21-fairness-and-max-depth.md)）发现：

- **dispatch 漏修是 Anthropic Claude Code subagent 也有的共性盲区**
- Opus 和 Sonnet subagent 都没主动检查 agent.py 里的 dispatch 表
- yansh 之前靠 review/fix 死循环才碰巧捞到

如果 prompt 能教会 LLM agent 主动检查全链路，理论上 yansh 能在这一项**胜过 Claude Code subagent**——这是个反直觉的实验：业余工具靠针对性 prompt 反超成熟工具。

## 实验：写一段"全链路意识" prompt

加到 `_ARCHITECT_ROLE` 和 `_CODER_ROLE`，关键设计点：

1. **直接点名具体形状**：不写抽象原则"修改函数签名要审视全链路"，写
   "dispatch 表（agent.py 里 `if name == "X"` 的分支）"
2. **反对默认假设**：明确说"用户列出的文件清单不一定完整"
3. **给典型陷阱**：导入语句、文档示例、dispatch 三类暗依赖

## 结果

同任务（list_files 加 max_depth）：

| 维度 | 之前 yansh | **本轮 yansh（新 prompt）** | Sonnet subagent | Opus subagent |
|---|---|---|---|---|
| 模型 | Sonnet | Sonnet | Sonnet | Opus |
| 耗时 | 242s | **175s** | 134s | 99s |
| 工具调用 | 39 | 43 | 23 | 16 |
| **Plan 阶段是否包含 agent.py** | ❌ | **✅** | — | — |
| **dispatch 实际修复** | ✅ fix loop 副产品 | **✅ plan→code 直接** | ❌ | ❌ |
| 实现是否正确 | ❌ | ❌（同 off-by-one） | ✅ | ✅ |
| 任务判定 | 失败 | 失败 | 完成 | 完成 |

## 三个证据明确这是 prompt 的功劳

**证据 1：plan 文件清单变化**
- 之前：`['tests/unit/test_tools.py', 'tools.py']` (2 文件)
- 现在：`['tools.py', 'tools_schema.py', 'agent.py', 'tests/unit/test_tools.py']` (4 文件，主动包含 agent.py)

**证据 2：plan 阶段就改对，不是 fix loop 副作用**
- 之前 yansh 是因为单测失败 → review/fix 多轮 → 才追到 dispatch
- 现在 yansh 在 architect 输出 plan 时就直接列出 agent.py，code 阶段一次性改对

**证据 3：subagent 同任务依然漏掉**
- Anthropic 的 Claude Code subagent（Opus/Sonnet 都试了）的内置 prompt 里**没有**这条
- yansh 加了这条 prompt → 这一项就反超了

## 历史性意义

**这是 yansh 首次在某个具体维度上胜过 Anthropic 的成熟工具**。

不是因为 yansh 整体好——它仍然慢、仍然 off-by-one、仍然顺手改不相关代码。
但**针对性 prompt 改进**让它在"全链路意识"这一项上跑赢了。

这印证了 ROADMAP P0 #2 的核心判断：

> 调一周 prompt 的效果，超过加 5 个工具。

而且更重要的：

> **针对自己代码库具体形状的 prompt**，能让小工具在专项上击败大工具。
> Claude Code 的 prompt 必须通用——它不能写"agent.py 的 dispatch 分支"这种
> 跟 yansh 代码库强绑定的话。这给业余项目留了一片真实的优势空间。

## 但 yansh 还输在两个老问题

1. **同样的 off-by-one**：实现 `dirs.clear() when current_depth >= max_depth`
   - max_depth=1 时，root 不触发清空（0<1），子目录文件被加进去
   - 已经栽过两次还在栽——说明 prompt 没解决"递归剪枝控制流"的能力问题
2. **顺手改不相关代码**：把 `files.append(rel_path)` 改成
   `files.append(rel_path.replace("\\", "/"))`，破坏 `test_list_files`

## few-shot 思维：用具体形状打具体问题

这次 prompt 改进最值得记的一条：

**抽象原则的勇气** vs **具体名词的有效**——

写 "修改函数签名时要全链路审视" 是抽象原则，可能 LLM 听了也不当回事。
写 "dispatch 表（agent.py 里 `if name == "X"` 的分支）" 是具体名词，
LLM 立刻有锚点知道要看什么。

这是 [list-tools 笔记](./2026-05-21-list-tools.md) 里 Claude Code 那条
"Do NOT re-read a file you just edited" 的同型——**一句具体的反向警告**
比一段抽象指南强 10 倍。

## 后续

- 解决 off-by-one：可能需要给 _CODER_ROLE 加 few-shot example，
  专门展示 max_depth 这类剪枝控制流的正确写法（"对每个文件计算 path_parts
  数，超过 max_depth 直接 continue"——简单直接，不靠 dirs.clear() 巧妙剪枝）
- 解决"顺手改不相关代码"：_CODER_ROLE 加 "diff 应只覆盖任务描述的功能；
  any 'while you're at it' 重构必须先停下来问用户"
- 这两条都是 P0 #2 子任务

## 一句话总结

**yansh 用一段 30 行的针对性 prompt，在 dispatch 暗依赖检查这一项上
反超了 Anthropic Claude Code 的两档 subagent**。整体仍落后，
但单点突破证明了 ROADMAP P0 #2 的杠杆。
