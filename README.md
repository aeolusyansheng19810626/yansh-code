# yansh-code

命令行编程助手：输入自然语言需求，LLM 自动完成 plan → code → test → fix 全流程。

---

## 目录

- [简介](#简介)
- [核心特性](#核心特性)
- [安装](#安装)
- [快速开始](#快速开始)
- [运行模式](#运行模式)
- [配置](#配置)
- [工具集概览](#工具集概览)
- [子 agent 派发](#子-agent-派发)
- [Skills / Hooks / MCP / Memory 扩展点](#skills--hooks--mcp--memory-扩展点)
- [slash 命令清单](#slash-命令清单)
- [安全机制](#安全机制)
- [任务日志和 replay](#任务日志和-replay)
- [跨平台支持](#跨平台支持)
- [测试和 CI](#测试和-ci)
- [架构简介](#架构简介)
- [开发状态](#开发状态)

---

## 简介

yansh-code 是一个本地运行的命令行编程助手 CLI。用户输入自然语言需求，yansh 调用 LLM 自动拆解成文件级计划，逐文件写代码，跑测试，失败就进 fix 循环，直到测试通过或耗尽重试次数。

支持交互模式（多轮对话、实时 slash 命令切换状态）和批处理模式（`--json` 输出结果，适合 CI 管道）。默认 workspace 为 `./workspace`，可通过 `--cwd` 指向任意项目目录。

---

## 核心特性

**主工作流**

- plan → code → test → fix 四阶段串行，每阶段由独立角色的 system prompt 驱动
- 计划以结构化 JSON 输出，可解析后按文件逐一执行
- fix 循环进入前自动识别 baseline 失败（pre-existing），只对增量失败修复
- 自动检测 ruff lint 错误并并入 fix 循环

**4 种运行模式**

auto / code / plan / audit，可在命令行或交互中动态切换（见[运行模式](#运行模式)）

**5 种 agent 角色**

架构师（plan）、码农（code）、测试员（fix）、审计员（audit）、子 agent（explorer / auditor / general）

> 注：早期还有独立的"审查员（review）"角色，因为割裂上下文 + JSON 协议脆弱导致死循环，已从主流程移除（见 `notes/shadow/2026-05-21_02-remove-reviewer.md`）。`_REVIEWER_ROLE` system prompt 与 `review()` 函数作为独立可调用工具保留，未来可绑到 `/review` skill。

**25+ 工具**

文件读写、AST 符号检索（tree-sitter）、全文搜索、命令执行、git 操作、网页抓取、长期记忆等（见[工具集概览](#工具集概览)）

**多模型支持**

Claude Opus/Sonnet/Haiku（IBM ICA 网关）、DeepSeek V4 Flash（OpenRouter）、Gemini 2.5 Flash/Pro（Vertex AI）；主模型失败自动降级到 Claude Haiku

**子 agent 并发派发**

主 agent 可把子任务 dispatch 给最多 4 个并发子 agent；explorer/auditor 自动切 Haiku 降成本（见[子 agent 派发](#子-agent-派发)）

**扩展点**

Skills（自定义 prompt 片段）、Hooks（7 个事件点注入 shell 命令）、MCP server（stdio JSON-RPC）、跨 session 持久记忆

---

## 安装

Python 版本要求：`>= 3.9`（CI 在 3.11 测试）

```bash
# 1. 克隆并创建虚拟环境（可选）
python -m venv venv
# Windows
.\venv\Scripts\activate
# Linux / macOS
source venv/bin/activate

# 2. 安装（可编辑模式，entry point：yansh）
pip install -e .
```

主要依赖（来自 `pyproject.toml`）：

| 包 | 用途 |
|---|---|
| `openai` | 调用 Claude / DeepSeek / Gemini（OpenAI 兼容协议） |
| `tree-sitter` + `tree-sitter-python` | AST 符号检索 |
| `rich` | 彩色终端输出 |
| `prompt_toolkit` | / 命令补全辅助 |
| `python-dotenv` | 读取 `.env` |
| `requests` + `beautifulsoup4` | 网页抓取 |
| `ddgs` | 文档检索 |
| `Pillow` | 图片注入（视觉任务） |
| `psutil` | 子进程组管理（跨平台） |
| `pathspec` | `.gitignore` 风格路径匹配 |

---

## 快速开始

**1. 配置密钥**

在项目根目录创建 `.env`：

```env
# Claude（IBM ICA 网关）
CLAUDE_API_KEY=sk-...
CLAUDE_BASE_URL=https://api.nextgen-beta.ica.ibm.com/ica/v1

# 或 OpenRouter（用于 DeepSeek 等）
OPENROUTER_API_KEY=sk-or-...

# Gemini（Vertex AI，可选）
GEMINI_API_KEY=...
GOOGLE_CLOUD_PROJECT=your-project-id
GOOGLE_CLOUD_REGION=us-central1
```

优先级：`OPENROUTER_API_KEY` > `CLAUDE_API_KEY` > `ANTHROPIC_AUTH_TOKEN` > `ANTHROPIC_API_KEY`

**2. 常用调用方式**

```bash
# 交互模式（进入 REPL，默认 workspace/ 目录）
yansh

# 指定项目目录
yansh --cwd /path/to/your/project

# 批处理模式：一次性传入需求，JSON 输出结果
yansh --mode code --json "给 utils.py 的 parse_date 函数加单元测试"

# 只看计划不执行
yansh --mode plan "重构 agent.py 的错误处理"

# 只读审计，输出 markdown 报告
yansh --cwd /path/to/project --mode audit "审计 src/ 目录，找潜在安全问题"

# 指定模型
yansh --model claude-opus-4-7 "优化 llm_client.py 的重试逻辑"
```

**3. 交互模式下的典型对话**

```
> 给 foo.py 里的 calculate_tax 函数加入边界检查，负数收入返回 0
正在处理新任务...
[Architect] 制定计划 → foo.py（预计改动 2 处），tests/test_foo.py（新建）
确认执行计划？[y/n] y
[Coder] 修改 foo.py...
[Tester] 运行 pytest tests/test_foo.py...
任务完成！

> /stats
本次 tokens: input 4521 / output 812 | 估算费用: $0.02
```

---

## 运行模式

通过 `--mode` 参数或交互内 `/mode <name>` 切换，四种模式互斥：

| 模式 | 说明 | 适用场景 |
|---|---|---|
| `auto` | plan → 人工确认 → code → test → fix（默认） | 日常使用，改动前可审阅计划 |
| `code` | 跳过人工确认，直接执行 plan → code → test → fix | 批处理、CI、受信任任务 |
| `plan` | 只生成计划，不写代码，不执行测试 | 快速预览 LLM 拆解思路 |
| `audit` | 只读审计——不写文件、不执行命令，输出 markdown 报告 | 接手老项目、PR 前体检 |

audit 模式的工具白名单仅包含只读工具，分发层同时拒绝任何写/执行调用（双重防护）。

---

## 配置

### 环境变量

| 变量 | 说明 |
|---|---|
| `CLAUDE_API_KEY` | Claude / IBM ICA 网关密钥 |
| `CLAUDE_BASE_URL` | ICA 网关端点（不填用默认 ICA 地址） |
| `OPENROUTER_API_KEY` | OpenRouter 密钥（也可复用给 ICA，取第一个非空值） |
| `ANTHROPIC_AUTH_TOKEN` | 备用 Claude API key 变量名 |
| `ANTHROPIC_API_KEY` | 备用 Claude API key 变量名 |
| `ANTHROPIC_BASE_URL` | 备用 base URL 变量名 |
| `GEMINI_API_KEY` | Google Cloud API key |
| `GOOGLE_CLOUD_PROJECT` | GCP 项目 ID（默认 `yansheng-project`） |
| `GOOGLE_CLOUD_REGION` | GCP 区域（默认 `us-central1`） |
| `HUMAN_IN_LOOP` | `true` 则默认开启 HIL 模式 |
| `YANSH_TRUST_PROJECT_CONFIG` | `always` / `never` / `auto`（控制是否加载项目级 mcp/hooks 配置） |

### 项目级配置（`<workspace>/.yansh/config.json`）

| 键 | 默认值 | 说明 |
|---|---|---|
| `model` | `claude-sonnet-4-6` | 默认 LLM 模型 |
| `mode` | `auto` | 默认运行模式 |
| `max_attempts` | `3` | fix 循环最多重试次数 |
| `test_command` | `null` | 覆盖自动检测的测试命令 |
| `safe_mode` | `true` | 危险命令拦截开关 |
| `compress_threshold` | `6000` | 历史超过此字符数触发自动压缩 |
| `keep_recent_turns` | `3` | 压缩时保留最近 N 轮原文 |
| `human_in_loop` | `false` | HIL 默认状态 |
| `coder_rounds_per_file` | `5` | 单文件 coder loop 工具调用轮次基线 |
| `coder_edits_per_round` | `3` | 每轮预估 edit 数（用于 expected_edits 动态调整轮次） |
| `fix_soft_limit` | `12` | fix loop 单次 attempt 工具轮次上限 |
| `fix_mechanical_error_bonus` | `12` | 检测到机械错误时追加的额外轮次 |

### 优先级

CLI 参数（`--model`、`--mode` 等）> `.yansh/config.json` > 默认值

---

## 工具集概览

约 25 个工具，按功能分组：

**文件操作**
`read_file`、`write_file`、`replace_in_file`、`append_to_file`、`move_file`、`delete_file`、`apply_patch`、`list_files`、`glob_files`

**内容搜索**
`search_in_files`（跨文件 grep，支持正则 + 文件类型过滤）

**AST 符号检索（tree-sitter）**
`list_symbols`、`get_symbol_definition`、`replace_symbol`、`find_references`、`workspace_symbols`（扫全项目，按 mtime 缓存）

**命令执行**
`execute_command`（带超时 + 危险命令拦截）

**项目导航**
`directory_summary`、`git_diff`、`git_log`

**网络 / 查阅**
`fetch_webpage`（抓网页转 markdown）、`search_docs`（文档检索）

**控制信号 / 元工具**
`task_complete`、`update_plan_draft`、`exit_plan_mode_signal`、`dispatch_subagent`、`save_memory`、`recall_memory`

audit 模式下只暴露只读工具子集；MCP server 的工具以 `mcp__<server>__<tool>` 前缀加入列表。

---

## 子 agent 派发

主 agent 通过 `dispatch_subagent` 工具把独立子任务委托给子 agent，子 agent 运行独立的 messages 历史，不污染父 context。

**role 类型**

| role | 模型 | 用途 |
|---|---|---|
| `explorer` | Claude Haiku 4.5 | 探索代码、查找信息（只读） |
| `auditor` | Claude Haiku 4.5 | 只读审计 |
| `general` | 主模型 | 通用子任务 |

**参数**
- `task`：自然语言任务描述
- `role`：`explorer` / `auditor` / `general`
- `max_steps`：最多工具调用轮次，clamped 到 `[1, 16]`

**约束**
- 子 agent 不能再嵌套 dispatch（防递归）
- 最多 4 个子 agent 并发（ThreadPoolExecutor）
- 只返回最终 summary，不暴露内部 messages

查看本 session 的子 agent 统计：`/subagent stats`

---

## Skills / Hooks / MCP / Memory 扩展点

### Skills

在 `<workspace>/skills/` 或 `~/.yansh/skills/` 下放 markdown 文件定义自定义指令。文件用 YAML frontmatter 声明触发词和适用模式，正文作为 prompt 片段拼入 system prompt。

```yaml
---
name: code-review
description: 代码审查工作流
triggers: ["review", "审查", "code review"]
modes: ["audit", "plan"]
---
## 审查清单
- 命名 / 边界 / 错误处理 / 测试
```

触发机制：关键词匹配（不区分大小写）+ LLM 语义匹配兜底。
管理命令：`/skill list`、`/skill show <name>`

### Hooks

在 `<workspace>/.yansh/hooks.json` 或 `~/.yansh/hooks.json` 里配置，格式兼容 Claude Code settings.json：

```json
{
  "hooks": {
    "PreToolUse": [
      {"matcher": "write_file",
       "hooks": [{"type": "command", "command": "node check.js", "timeout": 10}]}
    ]
  }
}
```

支持 4 个事件：`PreToolUse`、`PostToolUse`、`UserPromptSubmit`、`Stop`

Hook 子进程通过 stdin/stdout 通信，可返回 `allow`（默认）/ `block` / `modify` / `system_message`。

批处理 `--json` 模式默认关闭 hooks。查看当前配置：`/hooks list`

### MCP（Model Context Protocol）

在 `<workspace>/.yansh/mcp.json` 或 `~/.yansh/mcp.json` 中声明 stdio MCP server：

```json
{
  "mcpServers": {
    "my-server": {
      "command": "node",
      "args": ["server.js"],
      "env": {}
    }
  }
}
```

yansh 启动时自动连接，server 暴露的工具以 `mcp__<server>__<tool>` 前缀加入 TOOLS 列表。
查看连接状态：`/mcp list`，查看工具清单：`/mcp tools [name]`

### Memory（长期记忆）

跨 session 持久化记忆，每条记忆一个 `.md` 文件，frontmatter 存元数据：

```
类型：user / feedback / project / reference
存储路径：
  <workspace>/.yansh/memory/<slug>.md  （项目级）
  ~/.yansh/memory/<slug>.md            （全局）
索引：MEMORY.md，每次会话启动自动加载到 system prompt
```

LLM 通过 `save_memory` / `recall_memory` 工具读写。
管理命令：`/memory list`、`/memory show <name>`、`/memory delete <name>`

---

## slash 命令清单

交互模式输入 `/` 后自动弹出候选列表，Tab 补全，Esc 关闭：

| 命令 | 说明 |
|---|---|
| `/mode <name>` | 切换运行模式（auto / code / plan / audit） |
| `/model` | 交互式切换写代码模型或 Review 模型 |
| `/revert` | 回滚到上次任务前的文件状态 |
| `/context` | 显示当前上下文字符数和轮数 |
| `/compress` | 手动压缩对话历史 |
| `/clear` | 清空全部对话历史 |
| `/stats` | Token 消耗 + 费用估算 |
| `/config` | 显示当前生效配置 |
| `/rules` | 显示 `.agent_rules` 内容 |
| `/hil [on\|off]` | 开关 Human-in-Loop 模式 |
| `/log` | 查看最近任务日志 |
| `/replay list` | 列出可用 replay 包 |
| `/replay load <id>` | 加载指定 replay 包 |
| `/plan_on` | 进入 Plan Mode（多轮探索 + 草稿精炼） |
| `/plan_off` | 退出 Plan Mode（丢弃草稿） |
| `/plan` | 查看当前 plan 草稿 |
| `/approve` | 批准草稿并进入实施 |
| `/skill list` | 列出已发现的 skill |
| `/skill show <name>` | 查看指定 skill 详情 |
| `/memory list` | 列出所有记忆条目 |
| `/memory show <name>` | 查看记忆详情 |
| `/memory delete <name>` | 删除记忆 |
| `/hooks list` | 查看已配置的 hooks |
| `/mcp list` | 查看 MCP server 连接状态 |
| `/mcp tools [name]` | 查看 MCP server 工具清单 |
| `/subagent stats` | 查看本 session 子 agent 统计 |
| `/exit` / `/quit` | 退出 |

---

## 安全机制

**危险命令拦截**

`execute_command` 内置规则拦截 `rm -rf /`、`python -c <inline>`、未授权 `pip install` 等；可通过 `.yansh/config.json` 的 `safe_mode: false` 关闭（不建议）。

**路径越界保护**

`write_file`、`replace_in_file`、`move_file` 的路径必须在 workspace 根目录内，越界直接返回错误。

**任务前快照与回滚**

进入 code 阶段前对计划文件做快照（git stash 优先，无 git 时文件复制兜底）；`/revert` 把工作目录还原到任务前状态。

**workspace 信任机制**

第一次进入包含 `.yansh/mcp.json` 或 `.yansh/hooks.json` 的 workspace 时，交互模式提示用户确认信任（防止 clone 不可信 repo 后触发 RCE）。非交互模式默认拒绝，可通过环境变量 `YANSH_TRUST_PROJECT_CONFIG=always` 显式 opt-in。

**HIL（Human-In-Loop）**

开启后（`/hil on` 或配置 `human_in_loop: true`），每个文件改动显示 diff，用户逐一 accept / reject / edit 后才落盘。

**strict 模式**

批处理 `--strict` 参数：拒绝任何需要交互确认的命令，确保 CI 流程可重现。

**沙箱**

`--sandbox docker` 或 `--sandbox docker:<image>` 把 `execute_command` 的执行隔离在 Docker 容器内。

**audit 模式双重防护**

audit 系统 prompt 声明只读约束，分发层同时拦截写/执行调用——即使 LLM hallucinate 也无法写文件或执行命令。

---

## 任务日志和 replay

**任务日志**

每个任务自动记录：requirement、mode、model、plan、修改文件列表、所有工具调用（参数）、测试命令、测试结果、attempts、duration、token 消耗（按模型分类）、warnings。

日志存放在 `<workspace>/.yansh/logs/`，可用 `/log` 查看最近几条。

**replay 包**

任务失败或异常时自动打包到 `<workspace>/.yansh/replay/`，包含日志 + 工作区快照，便于事后复现和调试。

```bash
# 交互模式下
> /replay list          # 列出所有 replay 包（按时间倒序）
> /replay load <id>     # 加载指定包，恢复工作区到失败时的状态
```

---

## 跨平台支持

- Windows / Linux / macOS 均可运行
- shell 命令执行通过 `procutil` 模块抹平 Windows/Unix 差异，含子进程组管理（防孙进程泄漏）
- 路径处理全程使用 `pathlib.Path`，容忍正反斜杠
- Windows 交互输入用 `ctypes` 直接调 `ReadConsoleInputW`，支持 Shift+Enter 换行、Tab 补全、上下键历史

---

## 测试和 CI

**本地运行测试**

```bash
# 全部单元测试（不调用真实 LLM，有 mock）
python tests/run_unit.py

# 全部测试（含集成测试，会消耗 API）
python tests/run_all.py

# 单独跑某个测试文件
pytest tests/unit/test_security.py -v
```

单元测试覆盖：工具函数、安全检查、audit 模式、plan mode 状态机、skills/hooks/mcp/memory、子 agent、session 隔离、并发 parser、read cache、task_log 并发、workspace trust 等，共约 22 个测试模块。

集成测试分组：`test_1_9`、`test_10_12`、`test_13_16`、`test_17_19`、`test_20_23`、`test_24_25`、`test_26_27`、`test_28_31`、`test_32_35`、`test_36_42`。

**CI（GitHub Actions）**

`.github/workflows/unit-tests.yml` 在 push / PR 时自动触发，matrix：

- OS：`ubuntu-latest` + `windows-latest`
- Python：`3.11`

CI 只跑单元测试（mock 了 `call_llm`，不消耗 API）。

**代码质量**

项目使用 ruff 做 lint；yansh 自身在 code/fix 阶段也会自动对 workspace 代码跑 ruff。

---

## 架构简介

```
yansh-code/
├── main.py           # CLI 入口：argparse、交互 REPL、slash 命令分发
├── agent.py          # Agent 核心：plan/code/audit/fix 状态机、会话历史、MCP 初始化
├── subagent.py       # 子 agent 执行器：dispatch、并发、防递归、stats
├── llm_client.py     # LLM 客户端工厂：call_llm 主循环、流式处理、cascade、token 统计
├── tools.py          # 全部工具实现（25+）
├── tools_schema.py   # LLM 工具调用 schema（TOOLS 列表）
├── config.py         # 模型配置、价格表、项目级 config.json 加载
├── state.py          # 全局会话状态（Session snapshot/restore，plan mode 状态）
├── skills.py         # Skills 系统：discover、frontmatter 解析、关键词匹配
├── hooks.py          # Hooks 系统：4 事件、shell 子进程协议
├── mcp_client.py     # MCP 客户端：stdio JSON-RPC、工具注入
├── memory.py         # 长期记忆：MEMORY.md 索引、save/recall
├── workspace_trust.py# workspace trust 检查（防 RCE）
├── snapshot.py       # 任务快照 + 回滚（git stash / 文件复制）
├── hil.py            # Human-In-Loop：diff 展示 + 确认交互
├── task_log.py       # 任务日志：init/finish/show/replay 包管理
├── linter.py         # ruff 集成 + 测试命令自动检测（pytest/uv/poetry/npm 等）
├── sandbox.py        # execute_command 沙箱（docker 后端）
├── procutil.py       # 跨平台进程组工具
├── frontmatter.py    # YAML frontmatter 解析（skills/memory 共用）
├── interrupt.py      # ESC 中断：异步监听 + 中断标志
├── monitor.py        # replay 日志分析 + 错误模式监控
└── console_shared.py # Rich Console 单例 + JSON 模式切换
```

各模块职责说明见上表。模块间依赖方向：`main` → `agent` → `tools` / `llm_client`；`subagent` 通过 lazy import 避免与 `agent` 循环依赖。

---

## 开发状态

当前代码状态（截至 2026-05-23）：

- 22 个单元测试全绿，ubuntu + windows CI 通过
- 全 ROADMAP P0-P4 已完成
- 5 场 yansh vs Claude Code 子 agent AB 测试已完成

**已知 backlog（按优先级）**

P1（每项预计 < 2h）：
1. plan prompt 注入实际 `WORKSPACE_DIR`，解决 LLM 假设 `/workspace` docker-style 路径问题
2. coder "用尽轮次"假警告——已 `task_complete` 时不报 warning
3. Detector 扩展 `NameError` / `AttributeError` 模式识别

P3（体验优化，半天能清）：
4. read_cache 命中率度量（加一行 log）

P2（需 1-2 天）：
5. Coder 单文件循环历史压缩——当前每轮重发完整 messages，是 token 暴涨的主要原因；难点是 tool_use/tool_result 配对合法性

**性能参考**

与 Claude Code 子 agent 的 5 场对比结论（同等任务）：
- token 消耗约为 CC 的 4-25×（主因：无 prompt cache + 每轮重发完整历史）
- 写代码场景慢于 CC；纯探索任务和论证任务差距收窄
- 跨文件密集重构（56 处调用）在 v4 修复后首次 pass（baseline 误识别问题已解决）
