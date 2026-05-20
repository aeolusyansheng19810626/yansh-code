# yansh-code

基于 LLM（Claude / DeepSeek / Gemini）的自动化代码生成与测试 CLI 工具。

通过 ReAct 循环自动完成：需求分析 → 生成代码 → 代码审查 → 自动测试 → 错误修复。

## ✨ 功能特性

- 🎭 **多 Agent 角色**：架构师（制定计划）、码农（生成代码）、审查员（代码走查）、测试员（分析修复）、**审计员**（只读审计现有代码）。
- 🤖 **全流程自动化**：LLM 自动拆解需求、生成代码、运行测试，并在失败时自动定位修复。
- 📝 **增量修改**：优先使用 `replace_in_file` 进行局部代码替换，拒绝无脑重写整文件。
- 🔍 **AST 符号检索**：通过 tree-sitter 精确定位函数/类定义，支持精确代码替换。
- 🗺️ **全局符号索引**：`workspace_symbols` 工具一次性扫出整项目的函数/类清单，按文件 mtime 缓存，命中后零成本；让 LLM 不用反复 `read_file` 摸结构。
- 📑 **代码审计模式**：`--mode audit` 启动只读人格，注入符号索引摘要 + 只读工具白名单，输出分级 markdown 报告，**全程拒绝任何写/执行操作**。
- 👁️ **视觉支持**：支持 `@image <路径/URL>` 和 `@paste`（剪贴板），可分析 UI 设计稿或报错截图生成代码。
- 🧪 **闭环质量保障**：集成 `pytest` 自动运行测试，支持 `ruff` 静态检查，无测试文件时自动生成最小测试用例。
- 🛡️ **安全与回滚**：任务开始前自动 git stash 快照（无 git 时文件复制兜底），支持 `/revert` 回滚。内置安全沙箱拦截危险命令。
- 📡 **流式输出**：LLM 回复实时打印 token，长任务不再黑屏等待。
- 📂 **多项目支持**：`--cwd <目录>` 指定任意项目作为 workspace，无需修改配置。
- 🔍 **测试命令自动发现**：读取 pyproject.toml / package.json / tox.ini，自动选择 uv/poetry/tox/yarn/pnpm 等正确的测试命令。
- 🗂️ **上下文管理**：支持 `@add_file` 注入特定文件上下文，自动压缩长对话历史，支持项目级规则 `.agent_rules`。
- 💾 **任务回放**：任务失败或异常时自动打包 `replay` 包，方便后续复现与调试。
- 💰 **成本透明**：显示每轮任务的 Token 消耗及估算的 API 费用。

## 快速开始

```bash
# 1. 进入虚拟环境
.\venv\Scripts\activate

# 2. 安装（pyproject 已声明全部运行期依赖，单条命令即可）
pip install -e .

# 3. 配置密钥
copy .env.example .env
# Claude（IBM ICA 网关）：填入 CLAUDE_API_KEY + CLAUDE_BASE_URL
# DeepSeek（OpenRouter）：填入 OPENROUTER_API_KEY
# Gemini（Vertex AI）：填入 GEMINI_API_KEY，并执行一次 gcloud auth application-default login

# 4. 运行（默认 workspace/ 目录）
yansh

# 指定任意项目目录作为 workspace
yansh --cwd /path/to/your/project
```

## 测试

项目提供了完整的单元测试与集成测试，建议在重大修改后运行：

```bash
# 运行单元测试（工具函数、安全检查等）
python tests/run_unit.py

# 运行集成测试（多轮 Agent 交互流程）
python tests/run_integration.py

# 运行全部测试
python tests/run_all.py

# 运行特定的集成测试文件 (例如 1-9 号场景)
python tests/integration/test_1_9.py
```

## 工作流程

输入需求后，程序自动执行 4 个阶段：

```
📋 阶段1：制定计划  →  [Agent: Architect] 分析需求，生成文件列表和测试命令
✍️ 阶段2：生成代码  →  [Agent: Coder]     多轮工具调用，写入或增量修改代码文件
🧐 阶段3：代码审查  →  [Agent: Reviewer]  检查代码逻辑与规范，不通过则自动重修
🧪 阶段4：测试与修复 →  [Agent: Tester]    自动生成测试 → 运行测试 → 失败则精准修复
```

## 代码审计模式

`--mode audit`（或 `/mode audit`）启动一条**完全独立、只读**的工作流，专门用于审计现有项目代码。

### 用法

```bash
# 审计当前目录的项目，输出 markdown 报告
yansh --cwd /path/to/your/project --mode audit "审计 src/ 目录，找出潜在 bug 与设计问题"

# 在交互模式中切换
yansh
> /mode audit
> 审计 agent.py 与 tools.py 的安全和性能问题
```

### 工作机制

1. **预注入符号索引**：启动时调用 `workspace_symbols` 扫描全项目（默认 `.py`），把"每个文件包含哪些函数/类"作为 system prompt 一次性给到 LLM，**避免 LLM 上来就反复 `list_files` / `read_file` 摸结构**。
2. **只读工具白名单**：审计人格只能看到 `read_file / list_files / glob_files / search_in_files / list_symbols / get_symbol_definition / find_references / workspace_symbols / git_diff / git_log / fetch_webpage / search_docs`。
3. **双重防护**：即使 LLM hallucinate 调用 `write_file` / `replace_in_file` / `execute_command`，分发层也会**直接返回错误并拒绝执行**——审计模式下保证零写入、零命令执行。
4. **多轮深挖**：LLM 根据预注入索引按需调用只读工具深挖（最多 8 轮），最后输出 markdown 报告。

### 报告结构

```markdown
## 总览
项目类型、规模、关注重点。

## 重要发现
- **严重**：xxx.py:123 — 现状 / 建议
- **中**：...
- **低**：...

## 总评
整体健康度，最值得优先处理的 1-3 项。
```

### 适用场景

- 接手老项目想先做体检
- PR review 前的预扫
- 想用一个 LLM 看另一个 LLM 写的代码（参见本仓库 `.claude/plans/` 下的多轮对照实验）

## 内置命令

在主循环中可直接输入以下命令（输入 `/` 后自动弹出候选列表，继续输入字母可过滤；Tab 补全，Esc 关闭）：

| 命令 | 说明 |
|------|------|
| `/mode plan` | 仅输出计划，不生成代码 |
| `/mode code` | 跳过确认，直接生成代码执行 |
| `/mode auto` | 默认交互模式（需用户确认计划） |
| `/mode audit` | 只读审计现有代码，输出 markdown 报告（详见上文"代码审计模式"） |
| `/revert` | 回滚到上一个任务执行前的状态 |
| `/context` | 查看当前上下文占用（轮数 / 字符数） |
| `/history` | 查看最近的对话历史记录 |
| `/stats` | 显示当前会话的 Token 消耗与费用统计 |
| `/config` | 查看当前生效的配置项 |
| `/rules` | 查看当前项目定义的 `.agent_rules` |
| `/hil [on/off]` | 开启/关闭 Human-In-Loop 模式（详细修改需逐一确认） |
| `/log` | 查看最近的任务执行日志 |
| `/model` | 交互式切换模型：写代码模型或 Review 模型（DeepSeek / Claude / Gemini 2.5，独立配置） |
| `/compress` | 手动压缩对话历史 |
| `/replay list/load` | 管理和加载任务回放数据 |
| `/clear` | 清空全部对话历史 |

## 特殊语法

- **注入文件**: 在输入中使用 `@filename.py` 临时注入单个文件，或使用 `@add_file path/to/file` 长期加载到上下文（`@clear_files` 清除）。
- **图片分析**: 输入 `@image path/to/img.png` 或粘贴图片后输入 `@paste`。
- **规则定义**: 在 `workspace/` 下创建 `.agent_rules` 文本文件，注入特定的开发规范。

## 项目结构

```
yansh-code/
├── main.py           # CLI 交互入口、命令分发与 / 自动补全
├── agent.py          # Agent 核心逻辑（状态机、质量级联、视觉处理）
├── llm_client.py     # LLM 客户端工厂、call_llm 主循环、流式处理、token 统计
├── tools.py          # 工具集（文件/AST操作、Web搜索、代码执行）
├── snapshot.py       # 任务快照与回滚（文件级备份，不污染 git 状态）
├── hil.py            # Human-In-Loop diff 展示与交互确认
├── task_log.py       # 任务日志记录与查询
├── tools_schema.py   # LLM 工具调用 schema 定义（TOOLS 列表）
├── linter.py         # Linter 与测试命令自动发现
├── config.py         # 模型配置、价格计算与项目级配置加载
├── monitor.py        # 任务执行监控与统计
├── interrupt.py      # ESC 异步中断处理
├── workspace/        # 生成代码的隔离输出目录
│   └── .yansh/       # 会话历史、日志与配置持久化目录
└── tests/
    ├── run_unit.py        # 单元测试入口
    ├── run_integration.py # 集成测试入口（真实调用 LLM，会产生 API 费用）
    ├── unit/
    └── integration/
```

## 支持的模型

| 显示名 | 模型 ID | 后端 |
|--------|---------|------|
| DeepSeek V4 Flash | `deepseek/deepseek-v4-flash` | OpenRouter |
| Claude Opus 4.7 | `claude-opus-4-7` | IBM ICA 网关 |
| Claude Sonnet 4.6 | `claude-sonnet-4-6` | IBM ICA 网关 |
| Claude Haiku 4.5 | `claude-haiku-4-5` | IBM ICA 网关 |
| Gemini 2.5 Flash | `google/gemini-2.5-flash` | Vertex AI |
| Gemini 2.5 Pro | `google/gemini-2.5-pro` | Vertex AI |

写代码模型与 Review 模型可**独立配置**，例如用 Claude Haiku 写代码、Gemini 2.5 Pro review。

### Gemini / Vertex AI 配置

1. 在 `.env` 中设置 `GEMINI_API_KEY`（Google Cloud API Key，需绑定 Agent Platform API）
2. 安装 [Google Cloud CLI](https://cloud.google.com/sdk/docs/install)，执行一次授权：
   ```bash
   gcloud auth application-default login
   gcloud auth application-default set-quota-project <your-project-id>
   ```
3. 默认使用 `us-central1` 区域，可通过 `GOOGLE_CLOUD_PROJECT` / `GOOGLE_CLOUD_REGION` 环境变量覆盖。

## 依赖

- `openai` — 调用 Claude / DeepSeek / Gemini API（OpenAI 兼容协议）
- `google-auth` — Vertex AI OAuth 2 token 自动刷新
- `tree-sitter` — AST 符号检索
- `rich` / `prompt_toolkit` — 高级终端交互
- `ruff` — 代码静态检查 (可选)
- `pytest` — 自动化测试驱动