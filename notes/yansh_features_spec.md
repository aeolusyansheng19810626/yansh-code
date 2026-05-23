# yansh 功能需求清单（文字版）

截至 commit `5c38fae`（task #5 v4 完成时）的当前实现功能全集。每条只描述需求，不涉及代码实现细节。用作多 agent 重建项目的 target spec。

---

## 1. 产品定位

一个命令行编程助手 CLI：用户输入自然语言需求，LLM 自动完成 **理解需求 → 制定计划 → 写代码 → 运行测试 → 修 bug** 的全流程，最终把代码写到指定工作目录。

支持交互模式（多轮对话）和批处理模式（命令行一次性传入需求 + JSON 输出）。

---

## 2. 主工作流（核心循环）

四个阶段串行执行：

1. **制定计划**
   - LLM 把需求拆成 N 个待修改/新建文件，每个文件配一个简短意图说明 + 预计改动量
   - 同时给出最终验收用的测试命令（如 `pytest tests/foo.py`）
   - 计划以结构化 JSON 输出，让主程序可解析后逐项执行

2. **执行计划生成代码**
   - 按计划文件列表逐个进入 coder 循环
   - 单文件循环里 LLM 多轮调用工具（读文件、改文件、查符号、跑命令等）直到声明该文件完成
   - 每文件有轮次上限保护，但允许根据计划中"预计改动量"动态调整上限

3. **代码审查**（可选阶段，已逐步并入 coder 自检）
   - 独立的 reviewer 角色 LLM 复查代码风格、逻辑、规范
   - 不通过则把意见喂回 fix 循环

4. **测试与修复循环**
   - 跑计划里的测试命令
   - 失败则进 fix 循环：LLM 看错误日志 + 当前代码 → 改代码 → 再跑测试，直到通过或耗尽 attempts
   - max_attempts 默认 3 次

主流程结束后输出成功/失败 + 摘要，并把"做过什么"加入会话历史。

---

## 3. 运行模式

四种 mode，互斥：

- **auto**（默认）：plan → 用户人工确认计划 → code → review → test → fix
- **code**：同 auto 但跳过人工确认（用于批处理 / CI）
- **plan**：只输出计划不执行，用于快速预览 LLM 的拆解思路
- **audit**：完全独立的只读审计路径——不写文件、不执行命令、不进 fix 循环，只输出 markdown 报告

模式可通过命令行 `--mode` 或交互内 `/mode <name>` 切换。

---

## 4. Agent 角色（system prompt 切换）

不同阶段使用不同的 LLM 人格 prompt：

- **架构师**（Architect / Planner）：负责 plan 阶段
- **码农**（Coder）：写代码 / 改代码
- **审查员**（Reviewer）：独立复查
- **测试员**（Tester）：分析测试失败、修 bug
- **审计员**（Auditor）：只读审计模式
- **子 agent**（Subagent）：以下三种 role
  - explorer（探索代码、查找信息，只读）
  - auditor（只读审计，类似主 audit 模式）
  - general（通用子任务）

---

## 5. 工具集（LLM 可调用的工具）

约 25 个工具，分类如下：

### 5.1 文件操作
- `read_file`：读文件，支持 offset / limit / max_bytes
- `write_file`：整文件写入
- `replace_in_file`：精确替换字符串（支持 replace_all）
- `append_to_file`：追加内容到文件末尾
- `move_file`：重命名/移动
- `delete_file`：删除
- `apply_patch`：应用 unified diff 补丁
- `list_files`：列当前目录文件树
- `glob_files`：按 glob 模式匹配文件

### 5.2 内容搜索
- `search_in_files`：跨文件 grep（支持正则 + 文件类型过滤）

### 5.3 AST 符号检索（基于 tree-sitter）
- `list_symbols`：列单文件内的函数/类
- `get_symbol_definition`：取符号定义
- `replace_symbol`：按符号名整段替换函数/类体
- `find_references`：查符号引用
- `workspace_symbols`：扫整项目的所有函数/类清单（按文件 mtime 缓存）

### 5.4 命令执行
- `execute_command`：跑 shell 命令（带超时 + 危险命令拦截）

### 5.5 项目导航
- `directory_summary`：按目录给文件数 / 类型分布摘要
- `git_diff`：当前工作树或 staged 的 diff
- `git_log`：最近 N 条提交

### 5.6 网络/查阅
- `fetch_webpage`：抓网页转 markdown
- `search_docs`：文档检索

### 5.7 控制信号 / 元工具
- `task_complete`：LLM 主动声明本阶段完成（含 success 标志 + summary）
- `update_plan_draft`：plan mode 内更新计划草稿
- `exit_plan_mode_signal`：退出 plan mode
- `dispatch_subagent`：派发子 agent 执行子任务，返回结果
- `save_memory` / `recall_memory`：长期记忆读写（按命名空间）

工具按运行模式裁剪：audit 模式下只暴露只读工具子集。

---

## 6. 子 agent 派发（dispatch_subagent）

主 agent 可以把"需要单独 context 的子任务"派给子 agent：
- 输入：自然语言任务描述 + role + max_steps
- 子 agent 独立 messages 历史，跑工具循环直到 task_complete 或耗尽轮次
- 返回：子 agent 的最终摘要（不暴露内部 messages）
- 子 agent 不能再嵌套派发（防递归）
- explorer/auditor 自动用更便宜的模型（haiku），general 用主模型

---

## 7. 模型支持

支持多家 LLM：
- Claude 系列（Opus / Sonnet / Haiku，走 IBM ICA 网关）
- DeepSeek（走 OpenRouter）
- Gemini（Google Vertex AI）

特性：
- 主模型 + 自动降级 cascade（主模型失败/超时自动切下一个）
- 写代码模型 / Review 模型可独立配置
- 流式输出（实时打 token，不黑屏等待）
- 模型按调用次数累计 token，最终给出按价格表估算的费用

---

## 8. 输入扩展语法

用户输入支持以下特殊语法（非 slash 命令）：

- `@filename.py`：临时把单个文件内容注入本轮 prompt
- `@add_file <path>`：长期加载到上下文（每轮都注入），直到 `@clear_files`
- `@image <path/URL>`：注入图片（支持本地路径或 URL，多模态视觉）
- `@paste`：从剪贴板读图片注入

---

## 9. 上下文管理

- 会话历史保存在 `<workspace>/.yansh/` 下，下次启动自动加载
- 历史超过阈值时自动压缩（保留最近 N 轮原文 + 早期摘要）
- 用户可手动 `/compress` 或 `/clear`
- 项目级规则文件 `.agent_rules`：写在 workspace 根目录的文本文件，每轮注入到 system prompt
- read_file 命中检测：单任务内已读过的同 ranges 不重复塞 messages

---

## 10. 安全机制

- **危险命令拦截**：`rm -rf /`、`python -c <inline>`、未授权 `pip install` 等触发拦截或人工确认
- **路径越界保护**：write/replace/move 的 filename 不能越出 workspace 根目录
- **任务前快照**：进 code 阶段前对计划列表里的文件做备份（git stash 优先，无 git 时文件复制兜底）
- **快照回滚**：`/revert` 把工作目录回到任务前状态
- **沙箱**：可选 `--sandbox docker` 把 execute_command 跑进容器
- **workspace 信任机制**：第一次进入新 workspace 提示用户 trust（防止恶意 `.yansh/config.json` 注入）
- **HIL（Human-In-Loop）**：开启后每个文件改动展示 diff 让用户逐一确认（accept / reject / edit）
- **strict 模式**：批处理下拒绝任何需确认的命令（确保非交互可重现）

---

## 11. 内置 slash 命令（交互模式）

| 命令 | 功能 |
|---|---|
| `/mode <name>` | 切换运行模式（auto/code/plan/audit） |
| `/model` | 交互式切换写代码/Review 模型 |
| `/revert` | 回滚到上次任务前 |
| `/context` | 显示上下文占用 |
| `/history` | 查看对话历史 |
| `/stats` | Token 消耗 + 费用估算 |
| `/config` | 当前生效配置 |
| `/rules` | 当前 `.agent_rules` 内容 |
| `/hil [on/off]` | 开关 HIL 模式 |
| `/log` | 最近任务日志 |
| `/compress` | 手动压缩历史 |
| `/clear` | 清空历史 |
| `/replay list/load` | 任务回放管理 |
| `/skill` | 列出/启用 skill |
| `/memory` | 列出/查看/删除长期记忆 |
| `/hooks` | 查看已注册 hooks |
| `/mcp` | 查看已连接 MCP server |
| `/subagent` | 子 agent 调用统计 |
| `/plan_on` / `/plan_off` / `/plan` / `/approve` | plan mode 进入/退出/查看草稿/批准 |
| `/exit` `/quit` | 退出 |

---

## 12. 任务日志 / 回放 / 监控

- 每个任务有完整日志：requirement、mode、model、plan、修改文件、所有工具调用（参数）、测试命令、测试结果、attempts、duration、token 消耗（按模型分类）、警告
- 失败/异常自动打包 replay 包到 `.yansh/replay/`，含日志 + 工作区快照，支持 `/replay load` 复盘
- monitor 模块可分析 replay 日志、watch 错误模式

---

## 13. 项目级配置

`<workspace>/.yansh/config.json` 持久化以下键：
- `model`：默认模型
- `mode`：默认运行模式
- `max_attempts`：fix 循环最多重试次数
- `test_command`：覆盖自动检测的测试命令
- `safe_mode`：危险命令拦截开关
- `compress_threshold` / `keep_recent_turns`：上下文压缩参数
- `human_in_loop`：HIL 默认状态
- `coder_rounds_per_file` / `coder_edits_per_round`：单文件轮次预算
- `fix_soft_limit` / `fix_mechanical_error_bonus`：fix 阶段轮次预算

CLI 参数（如 `--model`）覆盖 config.json，config.json 覆盖默认值。

---

## 14. 测试集成

- 自动检测项目类型（python / node）+ 选对应测试命令（pytest / npm test / pnpm test / poetry / uv 等）
- 自动检测 test_command 的 scope（plan files → 推断相关 test_*.py）
- 自动跑 ruff lint，错误进 fix 循环
- workspace 无测试文件时自动生成最小测试用例（normal / boundary / invalid 三场景）
- 进入 fix 循环前自动捕获 baseline 失败列表，循环里只对增量失败修

---

## 15. 扩展点

### 15.1 Skills
- 用户在 `~/.claude/skills/` 或 workspace `.claude/skills/` 下放 markdown 文件定义自定义指令
- 主程序根据用户输入做关键字匹配 + LLM 选择，命中后把 skill 内容注入 system prompt

### 15.2 Hooks
- 在 7 个事件点注入用户自定义 shell hook：`UserPromptSubmit` / `PreToolUse` / `PostToolUse` / `Stop` / `SubagentStop` / `Notification` / `SessionStart`
- hook 可拒绝、修改或补充信息进入主流程
- 配置文件：workspace `.claude/settings.json` 或全局 `~/.claude/settings.json`

### 15.3 MCP（Model Context Protocol）
- 启动时连接 `mcp.json` 里声明的 MCP server，把它们暴露的工具自动加入 TOOLS 列表
- 工具名加 `mcp__<server>__` 前缀避免冲突
- 支持 stdio / sse 两种 transport

### 15.4 Memory（长期记忆）
- 跨任务持久化记忆，支持 user / project / feedback / reference 四种 type
- LLM 通过 `save_memory` / `recall_memory` 工具读写
- 索引文件 `MEMORY.md` 在每次会话启动时自动加载

---

## 16. 中断与控制

- ESC 键随时中断当前 LLM 调用 / 工具循环
- 中断后保留状态，下一轮继续
- 长任务自动捕获 KeyboardInterrupt / SIGTERM 优雅收尾

---

## 17. 输出与可观测

- Rich Console 彩色输出 + 阶段标记
- 流式 token 实时打印
- 每任务结束打印 token 用量 + 费用估算（按模型分别计费）
- `--json` 模式 stdout 只输出最终 JSON 结果，stderr 保留过程日志（适合 batch / pipe）

---

## 18. 跨平台

- Windows / Linux / macOS 都跑
- shell 命令执行用统一 procutil 抹平差异
- 路径处理用 pathlib，全程容忍正反斜杠

---

## 不在功能范围（明确不实现）

下面这些当前 yansh 也没有，重建 project 同样不做（避免范围爆炸）：

- 没有 GUI / Web UI
- 没有多用户协作
- 没有版本管理（不替代 git）
- 没有自己的语言服务器（依赖 tree-sitter 做轻量 AST）
- 不支持二进制文件编辑（图片只能"看"，不能改）
- 不持久化任务间的 LLM messages（只存 history 摘要）

---

## 重建优先级建议（如果要分期）

**P0 核心（必须）**：第 2 / 3 / 4 / 5.1-5.4 / 6 / 7 / 12 / 13 / 14
**P1 增强**：8 / 9 / 10 / 11 / 16
**P2 扩展生态**：15.1-15.4
**P3 体验**：17 / 18
