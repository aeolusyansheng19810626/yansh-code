# yansh-code 功能文档

## 1. 项目概述

### 目标与用途

yansh-code 是一个本地代码智能体（coding agent），通过 LLM 驱动，支持完整的 plan → code → test → fix 循环。设计目标是在本地开发环境中自主完成编码任务，同时提供充分的安全控制和人工介入机制。

### 核心能力

- **多模式任务执行**：auto/plan/code/audit 四种工作模式
- **工具调用系统**：25 个工具，覆盖文件 IO、命令执行、AST 操作、代码搜索、Web 检索
- **子 Agent 架构**：支持并发子 agent 分派（最多 4 个并发）
- **上下文压缩**：超阈值自动调用 Haiku 压缩历史
- **扩展系统**：Skills、Memory、MCP、Hooks 四大扩展机制
- **安全机制**：路径越界校验、危险命令拦截、workspace trust、沙箱

### 技术栈

| 组件 | 技术 |
|------|------|
| LLM 接入 | OpenAI 兼容协议（IBM ICA 网关 / OpenRouter / Vertex AI） |
| 结构化输出 | Pydantic v2（PlanResult / ReviewResult） |
| 终端 UI | Rich 彩色输出 + Windows Console API 原生输入 |
| AST 操作 | tree-sitter + tree-sitter-python |
| Web 工具 | requests + beautifulsoup4 + ddgs |
| 子进程管理 | psutil（跨平台进程树 kill） |
| 路径匹配 | pathspec（.gitignore 风格） |
| 包入口 | `yansh` → `main:main` |

---

## 2. 目录结构

```
yansh-code/
├── main.py               # CLI 入口 + 交互主循环
├── agent.py              # 核心 agent 逻辑（3484 行）
├── config.py             # 全局配置中心
├── llm_client.py         # LLM 客户端工厂 + 流式响应
├── tools.py              # 25 个工具实现
├── tools_schema.py       # 工具 JSON Schema 定义
├── subagent.py           # 子 agent 执行器
├── mcp_client.py         # MCP stdio 客户端
├── hooks.py              # 事件 Hook 系统
├── skills.py             # Skills 加载与匹配
├── memory.py             # 跨 Session 持久记忆
├── snapshot.py           # 文件快照与回滚
├── task_log.py           # 任务执行日志
├── state.py              # 会话级运行时状态封装
├── linter.py             # 项目类型检测 + Linter 执行
├── hil.py                # Human-in-Loop 人工确认
├── interrupt.py          # ESC 键中断检测
├── sandbox.py            # Docker 沙箱（opt-in）
├── monitor.py            # 日志分析与监控
├── procutil.py           # 跨平台子进程管理
├── workspace_trust.py    # Workspace 信任安全检查
├── frontmatter.py        # YAML frontmatter 解析器
├── console_shared.py     # Rich console 单例 + JSON 模式
├── pyproject.toml        # 包配置（版本 0.1.0）
├── requirements.txt      # 依赖声明
│
├── .claude/
│   ├── settings.json       # Claude Code 项目设置
│   └── settings.local.json # 本地设置（不入 git）
│
├── .github/workflows/
│   └── unit-tests.yml      # CI 单元测试工作流
│
├── tests/
│   ├── run_all.py          # 顶级测试入口
│   ├── run_unit.py         # 单元测试运行器
│   ├── run_integration.py  # 集成测试运行器
│   ├── unit/               # 22 个 pytest 单元测试文件
│   └── integration/        # 10 个场景式集成测试文件（场景 1–42）
│
├── workspace/              # 默认 agent 工作目录
│   ├── .agent_rules        # agent 行为规则（项目级）
│   ├── .yansh/             # yansh 运行时目录
│   │   ├── config.json     # 项目级配置
│   │   ├── mcp.json        # MCP server 配置
│   │   ├── hooks.json      # Hooks 配置
│   │   ├── memory/         # 项目级 memory .md 文件
│   │   ├── logs/           # 任务日志 .jsonl
│   │   ├── snapshots/      # 文件快照
│   │   └── replay/         # 失败回放包
│   └── tests/unit/
│
├── notes/
│   ├── SUMMARY.md
│   ├── yansh_features_spec.md
│   └── shadow/             # 开发日志（命名：YYYY-MM-DD_NN-slug.md）
│       └── ab/             # AB 测试原始数据与对比报告
│
└── scripts/
    ├── probe_ica_cache.py  # ICA 缓存探测
    └── probe_ica_models.py # ICA 模型列表探测
```

---

## 3. 核心模块

### 3.1 `config.py` — 全局配置中心

**重要常量**：

```python
CLAUDE_OPUS    = "claude-opus-4-7"
CLAUDE_SONNET  = "claude-sonnet-4-6"
CLAUDE_HAIKU   = "claude-haiku-4-5"
ICA_GEMINI_3_PRO  # ICA 网关可达的 Gemini 模型
ICA_GPT_5_4       # ICA 网关可达的 GPT 模型
OPENROUTER_BASE_URL = "https://api.nextgen-beta.ica.ibm.com/ica/v1"
WORKSPACE_DIR  = "workspace"
QUALITY_CASCADE = [CLAUDE_SONNET, CLAUDE_HAIKU]  # 模型降级链
TOKEN_PRICE_TABLE  # 各模型 $/1M token 定价
MAX_ATTEMPTS   = 3
```

**`_DEFAULTS` 项目配置默认值**：

| 键 | 默认值 | 说明 |
|----|--------|------|
| `model` | `claude-sonnet-4-6` | 主模型 |
| `mode` | `auto` | 工作模式 |
| `safe_mode` | `true` | 安全模式 |
| `compress_threshold` | `6000` | 压缩触发 token 阈值 |
| `keep_recent_turns` | `3` | 压缩时保留最近轮数 |
| `coder_rounds_per_file` | `5` | 每文件最大 coder 轮次 |
| `fix_soft_limit` | `12` | fix 循环工具轮次软上限 |
| `max_attempts` | `3` | 最大重试次数 |
| `human_in_loop` | `false` | HIL 开关 |
| `test_command` | `""` | 覆盖自动检测的测试命令 |

**关键函数**：

- `set_workspace_dir(path)` — 切换工作目录
- `load_project_config()` — 从 `<workspace>/.yansh/config.json` 加载配置
- `get_config()` — 返回当前生效配置字典
- `override_config(**kwargs)` — 运行时覆盖配置项
- `get_model_price(model)` — 查询模型定价

---

### 3.2 `agent.py` — 核心 Agent 逻辑

**Pydantic Schema**：

```python
class PlanFile(BaseModel):
    filename: str
    intent: str
    description: str
    expected_edits: list[str]

class PlanResult(BaseModel):
    files: list[PlanFile | str]
    test_command: str

class ReviewResult(BaseModel):
    approved: bool
    issues: list[str]
    suggestions: list[str]
```

**模块级重要常量**：

```python
_FIX_SOFT_LIMIT    = 12       # fix loop 工具轮次上限
_AUDIT_SOFT_LIMIT  = 16       # audit loop 工具轮次上限
_FIX_TOKEN_BUDGET  = 60_000   # fix 预算警告阈值
_AUDIT_TOKEN_BUDGET = 120_000 # audit 预算警告阈值
MAX_HISTORY        = 20       # 最大历史轮数
CHAT_CONTEXT_ROUNDS = 5       # chat 模式保留轮数
COMPRESS_MODEL     = "claude-haiku-4-5"  # 压缩用模型
_TOOLS_LOCK        # TOOLS 列表并发读写保护锁
_MECH_ERROR_PATTERNS  # 机械错误检测正则（触发 fix 追加预算）
```

**角色 Prompt 常量**：

```python
_ARCHITECT_ROLE  # 生成 plan 的架构师
_CODER_ROLE      # 逐文件写代码
_REVIEWER_ROLE   # 代码审查
_TESTER_ROLE     # 测试失败修复
_AUDITOR_ROLE    # 只读审计
_PLANNER_ROLE    # Plan Mode 多轮对话
```

**主流程函数**：

| 函数 | 说明 |
|------|------|
| `run(requirement, mode="auto")` | 顶层任务入口；串联 plan → code → test → fix → review 循环 |
| `plan(requirement)` | 调 Architect LLM 生成结构化 plan（JSON 输出） |
| `code(plan_result, requirement)` | 逐文件循环调 Coder LLM，含 auto-compact + read_cache + HIL |
| `fix(test_result, requirement)` | 测试失败后调 Tester LLM 修 bug，有软上限 + token 预算保护 |
| `audit(requirement)` | 只读审计模式，输出 Markdown 报告 |
| `review(requirement, modified_files)` | 代码审查，返回 approved/issues/suggestions |
| `chat(user_input)` | 普通对话（非任务）分支 |
| `classify_input(user_input)` | 判断输入是 "task" 还是 "chat" |

**工具分发函数**：

| 函数 | 说明 |
|------|------|
| `_dispatch_tool_call(tool_call, ...)` | 单工具分发入口（触发 PreToolUse/PostToolUse hooks） |
| `_dispatch_tool_call_inner(...)` | 实际分发：写工具 HIL 确认、audit 拦截、read_cache 去重、MCP 路由 |
| `_dispatch_tool_calls(tool_calls, ...)` | 批量分发；`dispatch_subagent` ≥2 时用 ThreadPoolExecutor 并发 |

**历史管理函数**：

| 函数 | 说明 |
|------|------|
| `maybe_compress_history()` | 超阈值时触发压缩 |
| `compress_history()` | 调 Haiku 生成摘要替换旧轮次 |
| `_compact_messages(msgs, keep_recent_pairs)` | coder loop 内 auto-compact（保留最近 N pair 原文） |
| `save_history()` / `load_history()` / `clear_history()` | 持久化 / 加载 / 清空历史 |

**Plan Mode 函数**：

```python
enter_plan_mode()      # 进入 Plan Mode
cancel_plan_mode()     # 取消
approve_plan()         # 用户批准，切换到 code 阶段
plan_chat(user_input)  # Plan Mode 多轮对话
is_plan_mode()         # 查询当前状态
get_plan_draft()       # 获取草稿内容
```

**辅助函数**：

- `_append_active_prompts(sys_prompt)` — 把激活的 skill prompt + memory 索引追加到 system prompt
- `_call_with_json_retry(stage, messages, parser_fn, ...)` — LLM 调用 + JSON 失败自动 retry 1 次
- `_infer_test_scope(plan_files)` — 根据改动文件推断相关测试文件列表
- `create_replay_package(failure_reason)` — 失败现场打包到 `.yansh/replay/`
- `init_mcp(verbose)` / `shutdown_mcp()` — 启动/关闭 MCP server

---

### 3.3 `llm_client.py` — LLM 客户端

**重要常量**：

```python
LLM_TIMEOUT_SEC          = 120
LLM_MAX_RETRIES_PER_MODEL = 3
_RF_UNSUPPORTED          # 动态探测不支持 response_format 的模型集合
```

**关键类**：

- `_StreamToolCall` — 流式累积的 tool_call，提供 `model_dump()` 兼容接口

**关键函数**：

| 函数 | 说明 |
|------|------|
| `call_llm(messages, tools, tool_choice, response_format, stream, model_override)` | 主调用入口，走 QUALITY_CASCADE 降级，ESC 中断检测，429/5xx 指数退避重试 |
| `_get_ica_client()` | 懒创建 IBM ICA 专用 client |
| `_get_gemini_client()` | 每次刷新 OAuth token 的 Vertex AI client |
| `_client_for(model)` | 按模型路由到对应 client |
| `_handle_stream(stream_iter, model)` | 流式响应消费，实时打印，返回伪 response 对象 |
| `set_quality_cascade(cascade)` | 切换模型降级链 |
| `get_session_total_tokens()` | 返回 session 累计 token 数 |
| `get_session_token_breakdown()` | 返回按模型分类的 token 明细 |
| `show_stats()` | 打印 token 消耗和费用预估 |

---

### 3.4 `tools.py` — 工具实现层

**文件操作工具**：

| 工具函数 | 说明 |
|---------|------|
| `write_file(filename, content)` | 写文件（含路径安全校验） |
| `read_file(filename, offset, limit, max_bytes)` | 读文件（默认 limit=2000行/200KB） |
| `replace_in_file(filename, old_str, new_str)` | 精确字符串替换 |
| `apply_patch(patch_text, file_path)` | 应用 unified diff patch |

**代码分析工具**：

| 工具函数 | 说明 |
|---------|------|
| `get_symbol_definition(symbol_name, file_path)` | AST 定位函数/类定义 |
| `replace_symbol(symbol_name, new_code, file_path)` | AST 替换整个函数/类 |
| `list_symbols(file_path)` | 列出文件所有函数/类 |
| `search_in_files(pattern, regex, extensions)` | 全局内容搜索 |
| `workspace_symbols(extensions, path, recursive)` | 工作区符号清单（分层模式） |
| `directory_summary(path)` | 目录概要（文件数/扩展名分布） |

**执行工具**：

- `execute_command(command, _timeout_sec=30)` — 执行命令（三级策略：deny/safe/confirm），支持 sandbox 包装

**Agent 控制 Sentinel**：

| Sentinel 函数 | 说明 |
|--------------|------|
| `task_complete(success, summary)` | LLM 声明任务完成 |
| `dispatch_subagent(task, role, max_steps)` | 派发子 agent |
| `update_plan_draft` / `exit_plan_mode_signal` | Plan Mode 专用 |
| `save_memory` / `recall_memory` | 透传到 memory 模块 |

**重要常量**：

```python
READ_FILE_DEFAULT_LIMIT    = 2000
READ_FILE_DEFAULT_MAX_BYTES = 200_000
ERROR_KINDS  # 标准化错误分类集合

# 安全策略
_DANGEROUS_PATTERNS  # 危险命令正则黑名单（rm -rf/sudo/curl|sh/PowerShell -enc 等）
_SAFE_PATTERNS       # 免确认安全命令白名单（pytest/ruff/ls 等）
_CONFIRM_PATTERNS    # 需用户确认命令（pip install/git checkout 等）
```

**路径安全函数**：

- `_validate_path(filename)` — 禁止绝对路径、`..` 穿越、符号链接逃逸

---

### 3.5 `tools_schema.py` — 工具 Schema 定义

**重要常量**：

- `TOOLS` — 完整工具列表（25 个工具的 OpenAI function calling JSON Schema）
- `READONLY_TOOL_NAMES` — 只读工具名集合，用于 audit 模式和 explorer/auditor 角色的工具过滤

---

### 3.6 `subagent.py` — 子 Agent 执行器

**重要常量**：

```python
_SUBAGENT_HARD_CAP       = 16    # max_steps 上限
_SUBAGENT_CONCURRENCY_CAP = 4   # 并发子 agent 上限
_SUBAGENT_HAIKU_MODEL    = "claude-haiku-4-5"  # explorer/auditor 默认模型
_WRITE_TOOLS             # 写工具集合，用于 general 子 agent 追踪修改文件
```

**关键函数**：

| 函数 | 说明 |
|------|------|
| `_run_subagent(task, role, max_steps)` | 子 agent 主循环（独立 messages，防递归，thread-local 隔离） |
| `_subagent_handler(task, role, max_steps)` | `dispatch_subagent` 工具的实际处理入口 |
| `_build_subagent_system_prompt(role)` | 构建 system prompt（含 workspace 符号索引 + memory 索引） |
| `_subagent_tools_for_role(role)` | 按角色过滤工具集（explorer/auditor 只有只读工具） |
| `_subagent_model_for_role(role)` | explorer/auditor 用 haiku，general 用父 cascade |
| `get_subagent_stats()` | 返回累计统计 |

---

### 3.7 `mcp_client.py` — MCP 客户端

**重要常量**：

```python
_PROTOCOL_VERSION  = "2024-11-05"
_INIT_TIMEOUT_SEC  = 15
_CALL_TIMEOUT_SEC  = 60
```

**关键类**：

- `MCPServer` — 单个 MCP server 的本地 stdio 客户端（JSON-RPC 2.0 协议，支持 tools/list + tools/call）

**模块级函数**：

| 函数 | 说明 |
|------|------|
| `start_all_servers(workspace_dir, verbose)` | 按 mcp.json 启动所有 server |
| `discover_tools_as_schemas()` | 将 server 工具转换为 yansh TOOLS 兼容 schema（命名：`mcp__<server>__<tool>`） |
| `call_tool(prefixed_name, arguments, timeout)` | 调用 MCP 工具 |
| `shutdown_all()` | 关闭所有 server（atexit 钩子） |
| `load_config(workspace_dir)` | 加载 mcp.json（含 workspace_trust 安全检查） |

---

### 3.8 `hooks.py` — 事件 Hook 系统

**重要常量**：

```python
_VALID_EVENTS      = ("PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop")
_HOOK_STDOUT_CAP   = 1MB     # 防 hook OOM
_HOOK_STDERR_CAP   = 256KB
```

**关键函数**：

| 函数 | 说明 |
|------|------|
| `run_hook_event(event, payload, match_target, workspace_dir)` | 触发事件，串行跑所有匹配 hook，聚合 block/modify/system_messages/errors |
| `_run_one_hook(hook, payload, cwd)` | 单 hook 子进程执行，含 stdout/stderr cap + 超时 kill |
| `load_config(workspace_dir)` | 加载 hooks.json（含 workspace_trust 检查） |
| `list_configured(workspace_dir)` | 列出当前配置（给 `/hooks` 命令） |

---

### 3.9 `skills.py` — Skills 系统

**关键类**：

```python
@dataclass
class Skill:
    name: str
    description: str
    triggers: list[str]
    modes: list[str]
    body: str
    source_path: str
```

**关键函数**：

| 函数 | 说明 |
|------|------|
| `discover_skills(workspace_dir)` | 扫描项目级 + 全局 skill 目录（`<workspace>/.yansh/skills/` + `~/.yansh/skills/`） |
| `match_skills(user_input, skills, mode, use_llm)` | 智能匹配（关键字 fast path + LLM fallback） |
| `format_skills_prompt(matched)` | 格式化 system prompt 片段 |
| `load_and_format(user_input, workspace_dir, mode, use_llm)` | 一站式入口 |

---

### 3.10 `memory.py` — 持久记忆系统

**重要常量**：

```python
VALID_TYPES = ("user", "feedback", "project", "reference")
```

**关键类**：

```python
@dataclass
class Memory:
    name: str
    description: str
    type: str
    body: str
    scope: str       # "project" | "global"
    source_path: str
```

**关键函数**：

| 函数 | 说明 |
|------|------|
| `save_memory(name, type, description, body, scope, workspace_dir)` | 写一条 memory + 更新 MEMORY.md 索引 |
| `find_memory(name, workspace_dir)` | 按 name 找 memory（含路径穿越防护） |
| `discover_memories(workspace_dir)` | 扫描所有 memory |
| `delete_memory(name, scope, workspace_dir)` | 删除 + 更新索引 |
| `load_memory_index(workspace_dir)` | 加载 MEMORY.md 索引文本用于 system prompt 注入 |

---

### 3.11 其他模块

**`snapshot.py`**：

| 函数 | 说明 |
|------|------|
| `create_snapshot(file_list)` | 创建快照（备份 plan 中的文件） |
| `restore_snapshot(snap_info)` | 按 meta.json 还原文件 |
| `cleanup_snapshot(snap_info)` | 删除快照目录 |
| `_gc_old_snapshots(keep=10)` | 保留最近 N 个快照，清理旧的 |
| `get_latest_snapshot()` | 返回最新快照 |

常量：`_SNAPSHOT_IGNORE_DIRS = {".git", ".yansh", "__pycache__", "venv", "node_modules", ".pytest_cache"}`

**`task_log.py`**：

| 函数 | 说明 |
|------|------|
| `init_task_log(requirement, mode)` | 重置当前任务日志，记 token baseline |
| `finish_task_log(success, attempts, test_result, task_complete_signal)` | 落盘任务日志（含 token delta 计算） |
| `record_file_modified(filename)` / `record_tool_call(name, safe_args)` | 增量记录（线程安全，加锁） |
| `show_recent_logs()` | 打印最近 5 条日志摘要 |
| `get_last_task_log()` | 供批处理 `--json` 输出 |

**`state.py`**：

- `Session`（dataclass）— 镜像 agent.py/tools.py 所有模块级可变状态，提供 `pull()` / `push()` / `reset(workspace_dir)` 方法
- `scoped_session(workspace_dir)` — 上下文管理器，进入时拍快照，退出时恢复（单测隔离用）

**`linter.py`**：

- `detect_project_type()` — 扫描 workspace 识别项目类型（Python/Node.js/Go/Rust/Java），返回 `(type_str, test_cmd)`
- `run_linter_for(project_type)` — 执行对应语言 linter（ruff/mypy/go vet/cargo clippy）
- `_detect_python_test_cmd(ws, scope)` — 检测 Python 测试命令（支持 uv/poetry/pytest/tox/make）

**`hil.py`**：

- `hil_confirm(filename, old_content, new_content, is_new_file)` — 展示 diff，询问 y/n/e/a（e=打开编辑器，a=本轮全部接受）
- `show_diff(filename, old_str, new_str)` — 打印带颜色的 unified diff
- `reset_auto_accept()` — 每次新任务清掉"全部接受"状态

**`sandbox.py`**：

- `SandboxConfig`（dataclass：enabled, backend, image, extra_args）
- `parse_cli_arg(value)` — 解析 `--sandbox docker[:image]` CLI 参数
- `wrap_command(command, workspace_dir)` — 按配置包装命令（禁用时原样返回）
- 常量：`DEFAULT_IMAGE = "python:3.11-slim"`

**`workspace_trust.py`**：

- `check_or_prompt(workspace_dir, config_filename)` — 加载项目级配置前调用
- `is_trusted(workspace_dir)` — 查白名单文件（`~/.yansh/trusted_workspaces.json`）
- `mark_trusted(workspace_dir)` — 写入白名单
- 白名单路径：`~/.yansh/trusted_workspaces.json`

**`procutil.py`**：

- `spawn_with_pgroup(cmd, **popen_kwargs)` — 起子进程并放进独立进程组（Windows: CREATE_NEW_PROCESS_GROUP；Unix: start_new_session）
- `kill_tree(proc, timeout)` — 杀整棵进程树（psutil 优先；fallback 到 taskkill/killpg）

**`frontmatter.py`**：

- `parse(text)` — 返回 `(meta_dict, body)`，支持标量/列表/一级嵌套，不依赖 pyyaml

**`monitor.py`**：

- `analyze_logs(log_dir)` — 统计总任务数/失败率/平均尝试次数
- `watch_errors(log_dir)` — 检测同一任务连续失败，打印警告

---

## 4. CLI 接口

### 入口命令

```bash
yansh [OPTIONS] [REQUIREMENT]
```

### 命令行参数

| 参数 | 说明 |
|------|------|
| `--mode {plan,code,auto,audit}` | 工作模式（默认 auto） |
| `--model MODEL` | 覆盖默认模型 |
| `--json` | 批处理模式，输出 JSON 到 stdout，日志到 stderr |
| `--strict` | 严格模式（批处理失败时非零退出码） |
| `--cwd PATH` | 指定 workspace 目录 |
| `--sandbox [docker[:image]]` | 启用 Docker 沙箱 |
| `REQUIREMENT` | （位置参数）直接执行任务，不进入交互 |

`VALID_MODES = {"plan", "code", "auto", "audit"}`

### 斜杠命令（`_SLASH_COMMANDS`，共 24 个）

**模式与模型**：

| 命令 | 说明 |
|------|------|
| `/mode <mode>` | 切换工作模式 |
| `/model <model>` | 切换模型 |

**历史管理**：

| 命令 | 说明 |
|------|------|
| `/compress` | 立即压缩历史 |
| `/clear` | 清空历史 |
| `/log` | 显示最近任务日志 |

**任务控制**：

| 命令 | 说明 |
|------|------|
| `/revert` | 回滚到最近快照 |
| `/plan_on` | 进入 Plan Mode |
| `/approve` | 批准当前 plan 草稿 |

**扩展系统管理**：

| 命令 | 说明 |
|------|------|
| `/skill [list\|<name>]` | 列出/查看 skills |
| `/memory [list\|save\|delete]` | 记忆管理 |
| `/hooks [list]` | 查看 hooks 配置 |
| `/mcp [list\|restart]` | MCP server 管理 |
| `/subagent [stats]` | 子 agent 统计 |

**其他**：

| 命令 | 说明 |
|------|------|
| `/help` | 显示帮助 |
| `/exit` / `/quit` | 退出 |

### 输入特性

`_read_input(prompt_str)` 实现（Windows 原生 Console API）：

- **Shift+Enter** — 插入换行（多行输入）
- **Tab** — 斜杠命令补全（`_match_slash` 前缀匹配）
- **方向键** — 光标移动 + 历史翻阅
- **ESC** — 中断当前操作

---

## 5. Agent 系统

### 工作模式

| 模式 | 说明 |
|------|------|
| `auto` | 自动判断：classify_input 区分 task/chat，task 走完整 plan→code→test→fix |
| `plan` | 只生成 plan，不执行 |
| `code` | 跳过 plan 阶段直接写代码 |
| `audit` | 只读审计，不调用写工具 |

### 角色系统

每个 LLM 调用使用对应角色 prompt：

- `_ARCHITECT_ROLE` — 接收需求，生成结构化 plan（JSON 输出）
- `_CODER_ROLE` — 接收 plan 中的单个文件意图，调用工具写代码
- `_TESTER_ROLE` — 接收测试失败输出，调用工具修复 bug
- `_AUDITOR_ROLE` — 只读审计，仅用 `READONLY_TOOL_NAMES` 工具
- `_REVIEWER_ROLE` — 代码审查，返回 `ReviewResult`（JSON）
- `_PLANNER_ROLE` — Plan Mode 交互式草稿生成

### 工具分发机制

```
_dispatch_tool_calls(tool_calls)
    ├── 单个工具 → _dispatch_tool_call(tool_call)
    │       ├── PreToolUse hook 触发
    │       ├── _dispatch_tool_call_inner()
    │       │       ├── 写工具 HIL 确认（human_in_loop=true 时）
    │       │       ├── audit 模式拦截写工具
    │       │       ├── read_cache 去重
    │       │       └── MCP 路由（mcp__ 前缀）
    │       └── PostToolUse hook 触发
    └── dispatch_subagent ≥2 → ThreadPoolExecutor 并发
```

### 子 Agent 角色与权限

| 角色 | 模型 | 工具集 |
|------|------|--------|
| `explorer` | haiku | 只读工具 |
| `auditor` | haiku | 只读工具 |
| `general` | 父 cascade | 全部工具 |

并发上限：`_SUBAGENT_CONCURRENCY_CAP = 4`；递归深度保护：thread-local 防止子 agent 再派发子 agent。

### 提示词注入机制

`_append_active_prompts(sys_prompt)` 在每次 LLM 调用前：
1. 调 `skills.load_and_format()` 匹配当前输入，附加激活的 skill prompt
2. 调 `memory.load_memory_index()` 附加 MEMORY.md 索引
3. 附加 `.yansh/.agent_rules` 内容（项目级规则）

---

## 6. Compact/摘要机制

### 两级压缩策略

#### 级别 1：全局历史压缩（`compress_history` / `maybe_compress_history`）

触发条件：历史估算 token 数 > `compress_threshold`（默认 6000）

流程：
1. 调 Haiku 生成旧轮次的摘要文本
2. 将摘要替换旧轮次，保留最近 `keep_recent_turns`（默认 3）对原文
3. 摘要以 system 消息形式插入历史头部

#### 级别 2：Coder Loop 内 Auto-compact（`_compact_messages`）

触发条件：coder loop 内消息列表超过阈值

函数签名：`_compact_messages(msgs, keep_recent_pairs)`

逻辑：
- 保留最近 `keep_recent_pairs` 轮次的原始消息
- 对更旧的消息调 Haiku 生成摘要
- 防止单文件 coder loop 中历史无限增长（token 雪崩保护）

### 压缩参数

| 参数 | 来源 | 默认值 |
|------|------|--------|
| `compress_threshold` | `config.json` | `6000` |
| `keep_recent_turns` | `config.json` | `3` |
| `COMPRESS_MODEL` | 常量 | `claude-haiku-4-5` |

### 手动触发

- 交互命令 `/compress` — 强制立即执行 `compress_history()`

---

## 7. Baseline 测试

### 测试框架结构

```
tests/
├── run_all.py           # python tests/run_all.py
├── run_unit.py          # python tests/run_unit.py
├── run_integration.py   # python tests/run_integration.py
├── unit/                # pytest 风格（22 个文件）
└── integration/         # 自包含场景式（10 个文件，场景 1–42）
```

### 单元测试（`tests/unit/`）

运行方式：`python tests/run_unit.py` 或 `pytest tests/unit/`

核心测试文件：

| 文件 | 覆盖内容 |
|------|---------|
| `test_tools.py` | read/write/delete/execute_command/replace_in_file |
| `test_security.py` | 路径越界拦截、危险命令拦截 |
| `test_subagent.py` | dispatch_subagent、role→工具集映射、递归防护、并发、context 隔离 |
| `test_agent_loop.py` | fix()/code() task_complete 信号传递 |
| `test_plan_mode.py` | plan 阶段工具收紧 |
| `test_hooks.py` | hooks 系统事件触发 |
| `test_memory.py` | memory 持久化与路径穿越防护 |
| `test_parser_concurrency.py` | JSON 解析并发安全 |
| `test_task_log_concurrency.py` | task_log 线程安全 |
| `test_session_isolation.py` | `scoped_session` 状态隔离 |
| `test_workspace_trust.py` | workspace 信任白名单 |
| `test_mcp.py` | MCP JSON-RPC 协议 |
| `test_skills.py` | skills 加载与匹配 |

### 集成测试（`tests/integration/`）

运行方式：`python tests/run_integration.py`

输出格式：每个场景输出 `[PASS] 场景名` 或 `[FAIL: 原因] 场景名`，`run_integration.py` 聚合统计通过率。

场景分布：

| 文件 | 场景范围 | 主要内容 |
|------|---------|---------|
| `test_1_9.py` | 1–9 | auto/plan/code 模式、危险命令拦截、路径越界、replace_symbol、自动压缩、回滚 |
| `test_10_12.py` | 10–12 | 追加写/符号查找/list_files |
| `test_13_16.py` | 13–16 | snapshot 多文件/并发/大文件 |
| `test_17_19.py` | 17–19 | apply_patch/find_references |
| `test_20_23.py` | 20–23 | 批处理模式/JSON 输出 |
| `test_24_25.py` | 24–25 | 会话日志/task log |
| `test_26_27.py` | 26–27 | move_file/audit |
| `test_28_31.py` | 28–31 | HIL y/n/a/禁用 |
| `test_32_35.py` | 32–35 | MCP/skill/hook |
| `test_36_42.py` | 36–42 | review 非 JSON、fix 截断、瞬时错误识别、batch strict、replace_in_file 多匹配、call_llm timeout |

### CI

`.github/workflows/unit-tests.yml` — GitHub Actions 自动跑单元测试。

---

## 8. AB 测试框架

### 整体设计

`AB-test/yscode/` 是版本回归冒烟框架，验证各 patch 版本的行为改善效果。

**核心思路**：同一个 task prompt，不同 yscode 版本，对比完成度 / token 消耗 / 成本。

### Runner 结构

```python
# 每个 runner 的调用方式
sys.argv = ["yscode", "--workspace", "<path>", "--mode", "code", "--json", "<prompt>"]
from yscode.__main__ import main
main()
```

每个 runner 的 `docstring` 记录历史各版本结果，作为可追溯的 A/B 对比日志。

### 版本演进记录（task5 为例）

| 版本 | 结局 | Token | 成本 | 关键问题 |
|------|------|-------|------|---------|
| v0.2.1 baseline | PlanFailed | 789K | $2.58 | — |
| v0.3-α | PlanFailed | 307K | $0.95 | M-02: plan 卡 21 次 read_file |
| v0.3-β | CoderBudgetExceeded(16) | 496K opus | $7.94 | budget 太紧 |
| v0.3-γ | CoderBudgetExceeded(14) | 481K sonnet | $1.51 | 换型仍 budget |
| v0.3-δ | read-only cap(4w/42t) | 1.52M | $4.74 | 绕路写 patch 脚本 |
| v0.3-ε | CoderBudgetExceeded(14w) | 1.13M | $3.05 | 只完成 14/65 处(22%) |
| v0.3-ζ | CoderBudgetExceeded(55w) | 3.74M | $11.09 | history 不收敛（token 雪崩 +231%） |
| v0.3-η | FixExhausted | 4.38M | $13.08 | 偶然 4/4 |
| v0.3-θ | Budget exceeded(58w/109t) | 3.57M | $9.74 | 单文件卡死 |
| v0.3-ι | FixExhausted | 2.51M | $6.66 | 4/4 确定 + multi-block |

### 专项验证 Runner（`z06_verify_runner.py`）

使用独立的 `z06_verify_workspace/`（含故意损坏的测试文件，触发 exit_code=2 unparseable 场景），验证 Z-06.1/Z-06.3 具体修复点，减少测试成本和噪声。

### 验证方式

通过 grep 特定日志关键词验证内部行为：

```python
# 典型验证模式
assert "[fix]" in stderr
assert "[edit]" in stderr
assert "unparseable" not in stderr
```

---

## 9. 配置系统

### 环境变量（`.env`）

| 变量 | 说明 |
|------|------|
| `CLAUDE_API_KEY` | IBM ICA 网关密钥（主要） |
| `CLAUDE_BASE_URL` | ICA 端点（默认 `https://api.nextgen-beta.ica.ibm.com/ica/v1`） |
| `OPENROUTER_API_KEY` | OpenRouter 密钥（已弃用但仍支持） |
| `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` | 备用直连 |
| `GEMINI_API_KEY` / `GOOGLE_CLOUD_PROJECT` / `GOOGLE_CLOUD_REGION` | Gemini/Vertex AI |
| `HUMAN_IN_LOOP` | 全局 HIL 开关（默认 false） |
| `YANSH_TRUST_PROJECT_CONFIG` | workspace trust 策略（always/never/auto） |

### 项目级配置（`<workspace>/.yansh/config.json`）

| 键 | 类型 | 默认值 | 说明 |
|----|------|--------|------|
| `model` | string | `claude-sonnet-4-6` | 主模型 ID |
| `mode` | string | `auto` | 工作模式 |
| `max_attempts` | int | `3` | 最大重试次数 |
| `test_command` | string | `""` | 覆盖自动检测的测试命令 |
| `safe_mode` | bool | `true` | 安全模式开关 |
| `compress_threshold` | int | `6000` | 压缩触发 token 阈值 |
| `keep_recent_turns` | int | `3` | 压缩保留轮数 |
| `human_in_loop` | bool | `false` | HIL 开关 |
| `coder_rounds_per_file` | int | `5` | 每文件最大 coder 轮次 |
| `fix_soft_limit` | int | `12` | fix loop 工具轮次软上限 |

### 运行时配置函数

```python
get_config()              # 返回当前生效配置
override_config(**kwargs) # 运行时覆盖（CLI 参数用）
load_project_config()     # 从 config.json 重新加载
```

### 扩展系统配置

| 文件路径 | 说明 |
|---------|------|
| `<workspace>/.yansh/mcp.json` | MCP server 定义（command/args/env） |
| `<workspace>/.yansh/hooks.json` | Hooks 定义（event/match/command） |
| `<workspace>/.yansh/skills/*.md` | 项目级 skills（frontmatter + body） |
| `<workspace>/.yansh/memory/*.md` | 项目级 memory（frontmatter + body） |
| `~/.yansh/skills/*.md` | 全局 skills |
| `~/.yansh/memory/*.md` | 全局 memory |
| `~/.yansh/trusted_workspaces.json` | workspace trust 白名单 |

### Skill/Memory 文件格式

```markdown
---
name: skill-name
description: 单行描述
triggers:
  - 关键词1
  - 关键词2
modes:
  - auto
  - code
---

# Skill 正文

具体指令内容...
```

---

## 10. 依赖和安装

### 核心依赖

| 包 | 版本要求 | 用途 |
|----|---------|------|
| `openai` | >=1.0.0 | LLM 调用（OpenAI 兼容协议，接 Claude/DeepSeek/Gemini） |
| `pydantic` | >=2.0.0 | 结构化输出 schema（PlanResult/ReviewResult） |
| `python-dotenv` | >=1.0.0 | 加载 `.env` 环境变量 |
| `rich` | >=13.0.0 | 彩色终端输出 |
| `tree-sitter` | >=0.25.0 | AST 解析 |
| `tree-sitter-python` | >=0.25.0 | Python AST 符号操作 |
| `requests` | >=2.31.0 | Web 抓取 |
| `beautifulsoup4` | >=4.12.0 | HTML 解析 |
| `ddgs` | >=7.0.0 | 文档搜索 |
| `prompt_toolkit` | >=3.0.0 | 斜杠命令补全 |
| `pathspec` | >=0.11.0 | .gitignore 风格路径匹配 |
| `Pillow` | >=10.0.0 | 图片注入 |
| `psutil` | >=5.9.0 | 跨平台子进程树管理 |

### Python 版本要求

`requires-python >= 3.9`

### 安装方式

```bash
# 从源码安装（开发模式）
pip install -e .

# 安装依赖
pip install -r requirements.txt

# 安装后可用命令
yansh [OPTIONS] [REQUIREMENT]
```

### 配置初始化

```bash
# 复制环境变量模板
cp .env.example .env
# 填写 CLAUDE_API_KEY 和 CLAUDE_BASE_URL

# 首次运行时自动创建 workspace/.yansh/config.json
yansh
```
