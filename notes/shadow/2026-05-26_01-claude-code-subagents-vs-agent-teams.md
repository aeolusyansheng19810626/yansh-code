# Claude Code 多 agent 机制：Subagents vs Agent Teams

**日期**：2026-05-26
**起因**：AB test 跑完后讨论"我（Claude Code）派子 agent 用的是哪种机制"，澄清两个易混淆概念
**关键意图**：用户之后给的任务**可能要使用 Agent Teams（方式二）**，本笔记防压缩丢失

---

## 三种相关机制对照

| | Claude Agent SDK | Subagents (Task tool) | Agent Teams |
|---|---|---|---|
| 调用方 | 开发者写 Python/TS 代码 import SDK | Claude Code 应用内置 Task / Agent tool | Claude Code 设 env var 启用 |
| 状态 | 稳定（独立 SDK 产品） | **稳定** | **experimental**（默认关） |
| 编排者 | 用户自己写代码 | Claude Code 主 agent | Claude Code 主 agent（"team lead"） |
| 通信 | 用户代码全控 | 子 agent 跑完返回**单条消息**给主 agent | teammates **互相**发消息（mailbox） |
| 任务调度 | 用户全控 | 主 agent 一对多分包 | shared task list（teammates 自己 claim） |
| 上下文 | 用户全控 | 子 agent 独立 context window | 每个 teammate 独立 context window |
| 子 agent 间通信 | n/a | ❌ 不能 | ✅ 可以（直接发消息） |
| 进程 | 用户独立进程 | 同一 Claude Code 进程内 spawn | 多个独立 Claude Code session |
| 启用条件 | 装 SDK 写代码 | 默认开 | `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` |

---

## AB test 用的是 Subagents（不是 Agent Teams）

本次 AB test（2026-05-25）跑 5 个 task × 3 方 = 15 次 dispatch，全部用 Claude Code 内置 Task tool（`subagent_type=general-purpose, model=sonnet`）—— **Subagents 机制**，稳定，不需要任何 env 配置。

15 次 dispatch 期间，**编排层（Claude Code 自己）从没出过问题**，只有目标 CLI（yansh / yscode）自身的 bug 暴露过。

---

## Agent Teams 是什么、什么时候用

### 启用

```json
// settings.json
{
  "env": {
    "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"
  }
}
```

要求 Claude Code v2.1.32+ (`claude --version` 确认)。

### 跟 Subagents 的核心差异 — Teammates 互相通信

```
Subagents:                       Agent Teams:
                                  
   主 agent                          lead
   ↓ ↓ ↓                           ↙ ↕ ↘
   sub sub sub                  team1 ⟷ team2 ⟷ team3
   ↓ ↓ ↓                           ↘ ↕ ↙
   results back                     shared task list + mailbox
```

Subagents 子 agent 之间**没有信道**——主 agent 收到所有结果再综合。Agent Teams 的 teammates 可以**直接互发消息**，可以 challenge 彼此观点（debate 模式）。

### 适合用 Agent Teams 的场景

文档明确列举：
1. **Research and review**：多 teammates 并行从不同角度调研 → 互相分享 + 挑战
2. **新模块/功能**：teammates 各拥一块独立部分
3. **Debugging with competing hypotheses**：teammates 测不同假设 → 像科学辩论那样互相反驳，活下来的假设更可能是真因
4. **跨层协调**：前端/后端/测试由不同 teammate owns

> **关键判断**：teammates 之间需要"通信、辩论、协作"才用 Teams；只是"分包+汇总"用 Subagents 即可。

### 启用方式

```text
（在主 session 里直接说）
Create an agent team to explore this from different angles: one
teammate on UX, one on technical architecture, one playing devil's advocate.
```

或更具体：

```text
Spawn 5 agent teammates to investigate different hypotheses. Have them
talk to each other to try to disprove each other's theories, like a
scientific debate. Update the findings doc with whatever consensus emerges.
```

### 控制操作

- **In-process 模式**：`Shift+Down` 切换到指定 teammate，输入消息直接发；`Enter` 进入 teammate session，`Esc` 中断当前 turn；`Ctrl+T` 切任务列表
- **Split-pane 模式**：每个 teammate 独立 pane（需要 tmux 或 iTerm2 + `it2` CLI）
- **强制 in-process**：`claude --teammate-mode in-process`

### 默认 teammate 模型

teammates **不继承** lead 的 `/model` 选择。在 `/config` 里设 "Default teammate model"。或在 spawn prompt 里指定：

```text
Use Sonnet for each teammate.
```

---

## Agent Teams 的已知 limitations（experimental 状态）

文档原文标 ⚠️ Warning：

| Limitation | 影响 |
|---|---|
| 不能 `/resume` 或 `/rewind` 恢复 in-process teammates | session 中断后 lead 可能去消息已不存在的 teammates |
| Task 状态可能滞后 | teammate 没标 completed → 阻塞依赖 task |
| Shutdown 可能慢 | teammate 等当前 request/tool 完成才退 |
| 一个 lead 同时只能管 1 个 team | 切团队前先 cleanup |
| 不能嵌套 | teammate 不能再 spawn team / teammate |
| Lead 固定 | session 谁创建 team 谁就是 lead，不能转交 |
| Permissions 在 spawn 时锁定 | teammate 起来之后可单独改 mode，spawn 时不能 per-teammate |
| Split-pane 不支持 VS Code 内置终端 / Windows Terminal / Ghostty | 用 tmux 或 iTerm2 |

### Token 成本

> Agent teams add coordination overhead and use significantly more tokens than a single session.

每个 teammate 是独立 Claude 实例，token 成本**线性增加**。3-5 个 teammates 是文档推荐起点，5-6 个 task / teammate 是合理任务数。

---

## 何时用哪个 — 决策表

| 场景 | 推荐 | 理由 |
|---|---|---|
| 给主上下文减负的"分包工"（搜代码、跑测试、调研） | **Subagents** | 稳定 + token 省 + 通信简单 |
| 5 个独立任务并行做完报告 | **Subagents** | 不需要互通 |
| 多角度 PR review（安全/性能/测试覆盖） | **Agent Teams** | teammates 看同一 PR 但各自焦点，最后由 lead 综合 |
| Debug 互相反驳假设 | **Agent Teams** | debate 机制 |
| 新模块各自实现独立部分 | **Agent Teams** | 减少同文件冲突 |
| 大型重构 / 跨层改动 | **Agent Teams** | 前端/后端/测试由不同 teammate owns |
| 一次性自动化（写脚本调 API） | **Agent SDK** | 不进 Claude Code 交互式 session |

---

## 数据来源

- Subagents 文档：https://code.claude.com/docs/en/sub-agents
- Agent Teams 文档：https://code.claude.com/docs/en/agent-teams
- AB test 实测笔记：`./ab/2026-05-25_01-task1-3way.md` ~ `2026-05-25_06-summary-3way.md`

---

## 待办

- 用户后续给的任务**可能要求用 Agent Teams**：到时记得：
  1. 先确认 `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` 已设
  2. 不要嵌套 team（teammate 不能再起 team）
  3. 任务设计成 teammates 可独立工作避免文件冲突
  4. 如果是 windows + 想要 split-pane，要装 tmux（**windows 兼容性差，可能只能用 in-process**）
