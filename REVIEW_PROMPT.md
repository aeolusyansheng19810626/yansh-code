# yansh-code 代码 review 请求

## 项目背景

yansh-code 是个**学习型** LLM coding agent，目标是复刻 Claude Code 的核心架构，理解 LLM agent 工程。
最近 4 周完成了 12 项 ROADMAP（P0/P1/P2），代码量约 8000 行 Python。

仓库：https://github.com/aeolusyansheng19810626/yansh-code

请直接读 GitHub 上的代码（`agent.py / mcp_client.py / hooks.py / memory.py / skills.py` 是新增和重写最多的）。
不要花时间扫整个 repo——重点文件清单见后面。

## 项目特点（不是普通 CRUD 项目，review 标准请按这个调）

- **学习项目**：架构选择 > 性能优化；可读性 > 高性能
- **作者一人维护**：不需要"团队协作友好"那一套（统一 lint 规则、PR 流程）
- **跟 Claude Code 对齐**：刻意复用了 Claude Code 的概念（mcp.json / hooks / memory / subagent role）
- **Windows 主开发环境**：跨平台 Python，Windows 特定的坑（npx.cmd / cmd.exe 转义 / 进程树 kill）已专门处理
- **测试覆盖严格**：16 个测试文件 / 约 270 单测，每个 P 项必须单测全过 + 端到端集成验证才算完

---

## 请重点 review 这 4 个方向（按优先级排）

### 1. 安全 / 越界 ⚠️ 最关键

新增了三个**对外的攻击面**，请帮我看是否有越界路径：

| 模块 | 攻击面 | 我做的防护 | 你帮我看的盲点 |
|---|---|---|---|
| `mcp_client.py` | LLM 通过 `mcp__server__tool` 名字调任意第三方 MCP server | 工具名前缀 + audit 模式拦截 mcp 工具 | server 返回的 content 直接给 LLM 看 → prompt injection？stderr 没流量限制？子进程 kill 不彻底？ |
| `hooks.py` | hook 是任意 shell 命令，stdin 给整个 tool_input/output 给外部进程 | 子 agent 内部跳过 + 模块禁用开关 + 失败默认 allow | shell=True 注入风险？hook 看到 user_input 后 leak 出去？timeout 杀进程树是否真彻底？ |
| `agent._run_subagent` (general role) | 子 agent 拿全工具集（除 dispatch_subagent + plan sentinels），能 write_file / execute_command | _IN_SUBAGENT 防递归 + 工具集物理过滤 | 父 agent 不知情下子 agent 改文件——这是设计选择还是漏洞？ |

具体 review 问题：
- `mcp_client.py:_run_one_hook` 那段进程树 kill 在 macOS / Linux 上真的能杀干净吗？（我只在 Windows 跑过）
- `hooks.py` 的 `_run_one_hook` 用 `shell=True`——如果用户在 hooks.json 里写 `"command": "rm -rf $(echo $USER_INPUT)"` 然后 user_input 含恶意内容，会发生什么？
- `tools.py:save_memory` 写文件路径基于 `_slugify(name)`——name 含路径遍历字符（`../../etc/passwd`）会被怎么处理？

### 2. 并发 / 线程安全

P2 #9b 引入了 ThreadPoolExecutor 并发跑 subagent。MCP 也用了后台 reader 线程。

- `agent.py:_dispatch_tool_calls` —— 多个 subagent 并发跑时共享什么？我用了 thread-local `_subagent_state` 和 `_SUBAGENT_STATS_LOCK`，**还有什么隐藏共享状态没保护**？比如 `_task_log_mod._task_tool_calls` 是个全局 list，并发 append 安全吗？
- `mcp_client.py:_reader_loop` —— reader 线程读 stdout 并 set Event。如果 server 进程死掉、stdout 关闭，reader 线程会怎么样？会泄漏吗？
- `hooks.py:_run_one_hook` —— 多个并发 hook 同时跑（虽然当前只串行，但未来可能放开）会不会有问题？

### 3. 架构 / 可维护性

`agent.py` 现在 **2859 行**——这是不是太大了？

- `_dispatch_tool_call_with_hooks` / `_dispatch_tool_call_inner` 拆 wrapper + inner——这个分层是对的吗？还是有更干净的做法（装饰器 / chain）？
- 5 个 system prompt 注入点都拼 `_ACTIVE_SKILLS_PROMPT + _ACTIVE_MEMORY_INDEX`——这种重复有没有更好的抽象（比如统一 `build_system_prompt(role)` 函数）？
- `state.Session` 镜像了一堆模块级变量（plan_mode / batch_mode / project_type 等），**新增的 `_ACTIVE_SKILLS_PROMPT / _ACTIVE_MEMORY_INDEX / _SUBAGENT_STATS / _IN_SUBAGENT` 没纳入 Session 镜像**——这会不会让测试隔离失效？
- 4 个新模块（`skills.py / mcp_client.py / hooks.py / memory.py`）是不是边界划得对？比如 hooks 跟 mcp 都是"子进程 + JSON-RPC over stdio"——可以抽 base class 吗？

### 4. 测试有效性

16 文件 / ~270 单测都过——但**测试本身可能有 hardcode 假设、mock 太狠**。请帮我挑出"看着覆盖了实际没测真问题"的：

- `tests/unit/test_subagent.py` 里的 `_mk_resp` mock 让 LLM 返回固定 tool_call——这种 mock 测出"代码能调到 LLM"，但测不出"LLM prompt 写得好"。这是 unit 测试该做的吗？还是需要单独的 prompt eval 测？
- `tests/unit/test_hooks.py:test_run_one_hook_timeout` 验证了 1 秒超时实际 1 秒返回——但它依赖 `taskkill` 真把进程树杀了。Windows / Linux / macOS 是不是都验证过？（其实我只在 Windows 上跑了 CI）
- `tests/unit/test_memory.py:test_agent_dispatch_save_memory` 验证 LLM 调 save_memory 后**真落盘**——这种"E2E in unit test"有没有更好的位置？

---

## 重点文件清单（按优先级）

按 review 价值排序（直接跳到这些文件最有性价比）：

1. **`mcp_client.py`** (411 行) ：JSON-RPC over stdio + 后台 reader 线程
2. **`hooks.py`** (309 行) ：4 事件 + 跨平台超时杀进程树
3. **`memory.py`** (296 行) ：4 type + 双路径 + 索引自动维护
4. **`skills.py`** (307 行) ：关键字 fast path + LLM 降级匹配
5. **`agent.py`** (2859 行) ：太大，建议只看以下函数：
   - `_dispatch_tool_call_with_hooks` / `_dispatch_tool_call_inner` (815-960)
   - `_dispatch_tool_calls` (998-1090) —— 并发 helper
   - `_run_subagent` (2018-2120) —— 子 agent loop
   - `init_mcp / shutdown_mcp` (2196-2240) —— TOOLS 原地修改
   - `run` 入口 (2467-2575) —— UserPromptSubmit / Stop hook 触发
6. **`tools_schema.py`** (464 行) ：所有工具 schema，看 description 是否引导得当
7. **`tests/unit/test_subagent.py`** ：并发测试是否真的测了并发（ThreadPoolExecutor 的 max active 验证）
8. **`tests/unit/test_hooks.py`** ：跨平台超时验证

不必看：tests/unit/test_tools.py / test_security.py 等老文件（pre-existing，本轮没动）。

---

## 期望产出

希望 review 给出（按优先级）：

1. **真实的安全漏洞**（能写出 PoC 的那种）
2. **会在生产挂掉的并发 bug**（race condition / deadlock 等）
3. **架构层面的"早改晚改"建议**（agent.py 2859 行该不该拆 / 4 个新模块边界）
4. **测试 hardcode** 揭露的"假覆盖"

不太需要：
- 命名风格 / 缺 docstring
- 单测覆盖率数字
- 标准 lint 类型问题

如有时间也欢迎：
- 跟 Claude Code 真实实现的对比（比如 Claude Code 的 Hooks 内部细节怎么做的）
- 我没意识到的 LLM agent 设计模式

谢谢！
