# 三家 Reviewer（Gemini 3.1 Pro / Claude 4.7 Opus / Codex GPT-5.5）综合发现

承接 [_22](./2026-05-22_22-memory.md)：ROADMAP 12 项收官后，请三家顶级模型 review 全部代码（focus：安全/越界、并发、架构、测试有效性）。
源文件：`review_result.txt`。

## 三家定位

| Reviewer | 用时 | 特点 |
|---|---|---|
| Gemini 3.1 Pro | < 1 min | 覆盖广，混入若干假阳性 |
| Claude 4.7 Opus | 8m 34s | 最深，挖出 reader_loop EOF 死等等独家真问题 |
| Codex (GPT-5.5) | 跑完 | 独挖 2 个 P0（audit 绕过、项目配置 RCE）+ tree-sitter 并发 |

## 验证后发现清单（已读关键代码逐条验真）

### 🔴🔴 P0 真严重 / 必修（Codex 独家）

**1. audit 模式可被 `dispatch_subagent(role="general")` 绕过**

链条：
- `tools_schema.py:459` `dispatch_subagent` 在 `READONLY_TOOL_NAMES`
- `agent.py:916` audit 只按工具名拦截 → 放行
- `agent.py:1978-1979` general role 拿 `all_names - blocked`，含 `write_file` / `execute_command`
- `agent.py:2051` `dispatch_mode = "audit" if role in ("explorer","auditor") else "auto"` ← general 走 auto
- `agent.py:2077` HIL/confirm 全禁

PoC：audit 模式下 LLM 调 `dispatch_subagent(task="write pwned.txt", role="general")` → 子 agent 真写盘。audit 的"只读承诺"破了。

修复方向：
- 父 mode=audit 时强制子 role 降级为 `explorer/auditor`，或
- 把 `dispatch_subagent` 拆成 `dispatch_readonly_subagent` / `dispatch_general_subagent` 两个显式工具，只前者进 READONLY

**2. 项目级 `.yansh/{mcp,hooks}.json` 是无确认 RCE**

- `mcp_client.py:264` / `hooks.py:80` workspace 优先
- `hooks.py:145` `shell=True`，`mcp_client.py:77` 直接 `Popen`
- 没有 trust prompt

PoC：恶意 repo 提交 `.yansh/hooks.json`（UserPromptSubmit 事件），用户 clone + 启动 yansh + 第一次输入 → 任意命令执行。供应链攻击面。

修复方向：项目级配置首次发现时弹一次 trust 确认（写 `~/.yansh/trusted_workspaces.json`），未授权时只加载全局配置。

### 🔴 P1 真漏洞 / 这一波必修

**3. `mcp_client._reader_loop` EOF 不唤醒 pending**（Codex + Claude）
- `mcp_client.py:190-212` server 死掉/stdout 关闭 → for 循环退出 → `self._pending` 永不被 set
- `_request` 死等到 `_CALL_TIMEOUT_SEC=60s`
- 修复：`reader_loop` 加 `try/finally`，finally 里把 pending 全部设错误响应 + `ev.set()`

**4. `mcp_client.shutdown` 不杀孙进程**（Gemini + Claude）
- `mcp_client.py:71-88` Popen 没设 `start_new_session` / `creationflags`
- 对比 `hooks.py` 已正确处理
- 后果：npx → node → mcp-server 这条链上的孙进程变孤儿
- 修复：抄 hooks 那段进程组创建；shutdown 用 `taskkill /F /T /PID` (Win) / `os.killpg` (POSIX)

**5. `memory.find_memory` 路径穿越**（Codex + 上轮）
- `memory.py:141` `f = d / f"{name}.md"` 没 slugify
- `save_memory:165` / `delete_memory:207` 走了 slugify，**只 read 路径不安全**
- PoC：`recall_memory(name="../../README")` → 读 workspace/README.md
- 修复：`find_memory` 加 `_slugify` + `Path.resolve().is_relative_to(target.resolve())` 双校验

**6. `_TS_PARSER` 并发不安全**（Codex 独家）
- `tools.py:691` 模块级单例、`706` 懒加载无锁
- `tools.py:743` `parser.parse(src_bytes)` 跨线程共享
- tree-sitter Python binding 的 Parser 不是线程安全的
- 并发 subagent 启动 `agent.py:1995` 都会调 `workspace_symbols()` → 冷缓存撞 parser
- 修复：parser parse 加锁，或用 thread-local parser

### 🟠 P2 真问题 / 应修

**7. `hooks.py` stdout 无 size cap → OOM**（Claude 独）
- `hooks.py:167` `proc.communicate(input=stdin_text, timeout=timeout)` 不限大小
- 失控 hook 几百 MB 输出能把 yansh 拖崩
- 修复：手动循环读 + 大小 cap（默认 1 MB）

**8. `task_log` 全局 list 并发 append**（Claude + Gemini）
- `task_log.py:18-20` 模块级 list 无锁
- CPython GIL 下当前不崩，free-threaded / 3.13+ 后会 race
- 多 subagent 写盘顺序乱
- 修复：加 `threading.Lock`，append 和 snapshot 走锁（15 行）

**9. `init_mcp` `TOOLS[:]=...` 与子 agent 迭代竞态**（Claude 独）
- `agent.py:2164` 原地修改
- 子 agent `_subagent_tools_for_role:1974` 用 `{t["function"]["name"] for t in TOOLS}`
- 子 agent 跑时做 `/mcp restart` → `RuntimeError: list changed size during iteration`
- 修复：`init_mcp` 加锁，或文档说明不并发 hot-reload

**10. `stderr_buffer.pop(0)` 与读 buffer race**（Gemini 独）
- `mcp_client.py` `_stderr_loop` `pop(0)` 跟 `call_tool` 错误诊断读 `stderr_buffer[-3:]` 无锁
- 修复：`list` → `collections.deque(maxlen=50)`，一行修

**11. 抽 `procutil.py`**（Claude 推荐）
- 把 hooks 那段进程组 kill 抽通用 (`spawn_with_pgroup` + `kill_tree`)
- 同时给 mcp 用，一举解决 #4 + 复用
- 这是最划算的抽象

### 🟡 P3 测试质量 / 跨平台

**12. `test_run_one_hook_timeout` 假阳性**（Claude + Codex）
- 当前测的是"主线程 1s 后返回"，没验证 taskkill 真把进程树干掉
- 修复：用 `psutil.pid_exists(pid)` 验证 pid 真死

**13. `test_run_subagent_max_steps_clamped_to_hard_cap` 没真测到 hard cap**（Codex 独）
- fake LLM 第一轮就有 content → 循环直接退出
- 修复：fake LLM 必须每轮返 tool_call，让 step 真撞 cap

**14. GitHub Actions 加 ubuntu-latest matrix**（Claude 独）
- 当前进程组 kill 只 Windows 跑过；POSIX `os.killpg` 没 CI 验证
- 修复：matrix 加 ubuntu，跑 hooks/mcp 全套

### 🔵 P4 架构改进 / 后续打磨

**15. agent.py 拆 `subagent.py` / `dispatch.py`**（全员）
- 当前 2859 行
- 优先拆 `subagent.py`（最独立，~250 行）

**16. 抽 `frontmatter.py` 或装 pyyaml**（Gemini + Claude）
- skills.py / memory.py 各写一个半残 YAML 解析器，行为已不一致

**17. `_ACTIVE_*` 进 Session.pull/push**（Gemini + Claude）
- `_ACTIVE_SKILLS_PROMPT` / `_ACTIVE_MEMORY_INDEX` / `_SUBAGENT_STATS` 没镜像
- 单测互相污染（当前各 test 自己兜底）

**18. `build_system_prompt(role)` 抽函数**（Claude 独）
- 5 处重复拼 `_ACTIVE_SKILLS_PROMPT + _ACTIVE_MEMORY_INDEX`

**19. general subagent 改文件后告诉父 agent**（Gemini 独）
- 父 agent 不知子 agent 改了哪些文件 → Lost Update race
- summary 末尾追加修改文件清单

### ❌ 明确驳回（误报）

- **hooks `shell=True` 注入**：stdin JSON 不拼命令，cmd 静态来自 hooks.json
- **save_memory 路径遍历**：`_slugify` 已防（`../../etc/passwd → etc-passwd`）
- **抽 hooks/mcp 公共 base class**：Claude 反对，长跑异步 vs 短命同步差太多
- **Hooks daemon 模式降延迟**：性能优化，当前 yansh 不是高频场景

## 三家对比 / 教训

- **Gemini 3.1 Pro**：覆盖广但有假阳性（误报 shell 注入），架构建议（"洋葱模型"）有价值但偏理论
- **Claude 4.7 Opus**：最深，挖出 reader_loop EOF 这种"看代码看到细节"的独家发现
- **Codex (GPT-5.5)**：精准捕权限边界——audit 绕过和项目配置 RCE 是另两家完全没看到的两个 P0

**关键教训**：
- 信任边界一旦交叉（audit 模式信号 → subagent 工具集），LLM 会找到最薄弱的一处穿透
- 项目级配置文件是"借代码库走的可执行实体"——必须有 trust 模型，不能默认加载
- 三家结合比单家全面得多——任何一家单独都漏掉至少 1 个 P0/P1

## 修复顺序（建议）

1. **第一波（P0，必须先做）**：audit 上下文降级 subagent role + 项目级配置 trust prompt
2. **第二波（P1）**：mcp 三件套（reader_loop / shutdown / 抽 procutil）+ memory.find_memory + tree-sitter 锁
3. **第三波（P2）**：hooks stdout cap + task_log 锁 + init_mcp 锁 + stderr_buffer deque
4. **第四波（P3）**：测试 psutil + max_steps 真测 + GitHub Actions ubuntu
5. **第五波（P4）**：架构整理（subagent.py 拆出 + frontmatter.py + Session 镜像）

每一波都要补对应单测。

## 关键文件

| 文件 | 改动点 |
|---|---|
| `agent.py:2040-2051` | audit 上下文降级 subagent role |
| `mcp_client.py:264` / `hooks.py:80` | 项目级配置 trust prompt |
| `mcp_client.py:75-103` | Popen 加进程组；shutdown 杀进程树 |
| `mcp_client.py:190-212` | `_reader_loop` finally 唤醒 pending |
| `memory.py:141` | `find_memory` 加 slug + 路径校验 |
| `tools.py:691,743` | `_TS_PARSER` 加锁或 thread-local |
| `procutil.py`（新文件） | `spawn_with_pgroup` + `kill_tree` |
| `hooks.py:167` | stdout cap |
| `task_log.py:18-20` | 加 Lock |
| `agent.py:2155-2173` | `init_mcp` 加锁 |
| `mcp_client.py` stderr_buffer | `list` → `deque(maxlen=50)` |
| `tests/unit/test_hooks.py` | timeout psutil 断言 |
| `tests/unit/test_subagent.py` | max_steps 真测 hard cap |
| `.github/workflows/*.yml` | ubuntu-latest matrix |
