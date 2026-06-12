# yansh-code 主代码全量 review（2026-06-12）

范围：agent.py / tools.py / llm_client.py / config.py / main.py / subagent.py /
mcp_client.py / hooks.py / memory.py / parallel_orchestrator.py / snapshot.py /
sandbox.py / workspace_trust.py / linter.py / hil.py / task_log.py / interrupt.py /
procutil.py / frontmatter.py / skills.py / state.py / monitor.py / console_shared.py / tools_schema.py。
测试代码不在范围。

观点：正确性 / 鲁棒性 / 安全性 / 性能 / token 消耗。

---

## 一、Critical（功能性 bug，建议尽快修）

### C1. ESC 中断后整个交互 session 卡死 —— `interrupt.reset()` 从不调用
- 证据：[interrupt.py:15](../../interrupt.py#L15) 定义 `reset()`，但全仓库（main.py / agent.py）**无任何调用点**（grep 仅命中定义与文档）。
- 后果：用户在某次任务里按一次 ESC → `_interrupted[0]` 永久为 True。监听线程不会清它，下一轮 `_read_input` 拿到新任务后，`run()`→`_run()`→各 loop 第一次 `interrupt.is_interrupted()` 立即抛 `Interrupted()`，**之后每个任务都秒中断，必须重启进程**。
- 修法：在 [main.py](../../main.py) 主循环每次拿到 `user_input` 后（进入 `agent.run`/`chat`/`plan_chat` 前）调一次 `interrupt.reset()`；或在 `_run()` 入口 `interrupt.reset()`。注意别在 loop 内部 reset（会吞掉本次任务的中断）。

### C2. prompt 反复教 LLM 用 `replace_in_file(..., replace_all=True)`，但该参数不存在
- 证据：tool 实现 [tools.py:507](../../tools.py#L507) `replace_in_file(filename, old_str, new_str)` 三参，且 `count > 1` 直接报错要求唯一匹配；schema [tools_schema.py:61](../../tools_schema.py#L61) 也只声明三参。但 `_CODER_ROLE` 与 code() 的 edit_strategy_hint 多处明确指导 `replace_all=True`（[agent.py:2305](../../agent.py#L2305)、[agent.py:2970](../../agent.py#L2970)）。
- 后果：LLM 听话发 `replace_all=True` → `write_file(**args)` 式分发会把多余 kwarg 传进函数 → `TypeError`，被 [agent.py:2157](../../agent.py#L2157) 的 `except Exception` 兜成 internal error 回灌。重复 N 处时 LLM 还被告知"一次 replace_all 修完"，结果每次都失败，反而烧轮次。这与 prompt 的设计初衷（省轮次）正相反。
- 修法二选一：①给 `replace_in_file` 真加 `replace_all: bool=False` 参数（count>1 时按 flag 决定报错还是全替换）+ 补 schema；②删掉所有 prompt 里的 `replace_all` 指导，改用"write_file 整文件重写"。推荐 ①，与 prompt 已铺垫的工作流一致。

---

## 二、Major（鲁棒性 / 安全性）

### M1. 危险命令黑名单可被简单绕过（安全防线偏弱）
- 证据：[tools.py:236](../../tools.py#L236) `_DANGEROUS_PATTERNS` 是正则字符串匹配。
- 绕过示例：`rm -rf` 被拦，但 `rm  -rf`（多空格 ok）、`bash -c "rm -rf x"`（`sh -c` 拦了但 `bash -c` 没拦）、`python -m pip ...`（绕过 `pip install` 的 confirm 因为它走 `python -m` 但不在 `_CONFIRM_PATTERNS`）、`env rm -rf`、变量拼接等都能逃。`del /f /s /q` 要求 `(/x )+` 连续 flag，`del /q /f` 顺序换一下仍命中但 `del *.py`（无 flag）放行。
- 定位：这是"防手滑"而非"防对抗"的防线（README/sandbox.py 注释也这么说）。但既然默认 `--sandbox none` 在宿主机跑，且 batch 非 strict 模式会**自动确认所有未识别命令**（[tools.py:402](../../tools.py#L402)），实际安全边界主要靠 `_validate_path`（那个是 resolve+is_relative_to，扎实）。
- 建议：①把 `bash -c` 加入黑名单（已有 `sh -c`）；②文档明确"黑名单非安全边界，不可信任务务必 `--sandbox docker`"；③batch 非 strict 自动确认 pip/git reset 的行为应在 README 顶部显著警示。

### M2. sandbox docker 命令注入面
- 证据：[sandbox.py:65](../../sandbox.py#L65) `wrap_command` 用 `sh -c {shlex.quote(inner)}`，inner 已 quote，image/ws 也 quote——这块没问题。但 `extra_args` 直接 `" ".join` 拼进 docker 命令行未 quote（[sandbox.py:77](../../sandbox.py#L77)）。
- 后果：`extra_args` 来自 CLI 解析（当前 `parse_cli_arg` 不解析 extra，恒为 `()`），暂无外部输入路径，风险潜伏。若将来 CLI 暴露 extra_args 配置，未 quote 会成注入点。
- 建议：现在加 `shlex.quote` 或 `shlex.split` 校验，封住未来回归。

### M3. MCP / 第三方工具返回内容直接进 LLM context，无 prompt-injection 防护
- 证据：[mcp_client.py:14](../../mcp_client.py#L14) 注释已自认"不做 prompt injection 防护"，`call_tool` 把 server 返回的 text 原样拼给 LLM。
- 定位：设计取舍，且有 workspace_trust 把项目级 mcp.json 卡在 trust 之后（[mcp_client.py:315](../../mcp_client.py#L315)）——这点做得好。但全局 `~/.yansh/mcp.json` 的 server 仍无条件信任，且其返回值可操纵后续 agent 行为（agent 是 general role 时能写文件/执行命令）。
- 建议：文档层面提示"只接信任的 MCP server"；长期可给 MCP 返回内容加分隔标记 + 系统提示"以下是外部数据，不要当指令"。

### M4. `_extract_json` 顶层对象抽取用 `find('{')...rfind('}')`，混入散文易截断错
- 证据：[agent.py:858](../../agent.py#L858)。当 LLM 回复是"这是计划：{...}，另外注意 {不要}"这类含多个花括号的散文时，`find('{')` 到 `rfind('}')` 会把中间所有内容当 JSON，解析必失败。
- 缓解现状：有 `_call_with_json_retry` 兜底 retry 1 次 + `response_format=json_object`（Claude 走 ICA 时还被 `_should_skip_rf` 跳过，所以 Claude 实际不传 json mode → 更依赖这个脆弱抽取）。
- 建议：可接受（retry 兜底），但记录为已知脆点；真要稳可引入花括号配平扫描。

### M5. `move_file` 的 PreTool test-first guard 用 `dst` 判定，会误拦纯重命名
- 证据：[agent.py:1699](../../agent.py#L1699) `_PRETOOL_WRITE_TOOLS["move_file"]="dst"`。enforcement==pre 时，把已有实现文件 `a.py` 重命名为 `b.py`，dst=`b.py` 无测试骨架 → 被拦，要求先建 `test_b.py`。但这只是重命名，不是写新实现。
- 后果：pre 模式下重命名类任务会被反复拦截到 max_block 才放行，烧 3 轮。
- 影响面：仅 `solo_test_enforcement=pre` 实验模式，默认 off 不触发。优先级低但记一笔。

---

## 三、性能 / token 消耗

### P1. solo gate 仲裁路径存在重复跑全量测试
- 证据：targeted collected-0 时回退全量重测（[agent.py:4596](../../agent.py#L4596)），`_final_gate_verdict` 里又有一份等价的全量回退（[agent.py:1804](../../agent.py#L1804)）。最坏情况一个 gate 周期内 targeted + 全量各跑一次，外加最终裁定再跑全量。
- 定位：测试运行本身不耗 token（不进 LLM），但 `test_gate_timeout_sec` 默认 300s，全量重测在大项目里可能各等数分钟，累积体感卡。属于可接受的"确定性换稳健"取舍，但值得知道。

### P2. `search_in_files` / `find_references` 逐文件 `open` 全量扫描，无 mtime 缓存
- 证据：[tools.py:598](../../tools.py#L598)、[tools.py:1360](../../tools.py#L1360) 每次调用都 `rglob("*")` 全树读。对比 `_parse_symbols_cached` 有 AST mtime 缓存，这两个高频只读工具没有。
- 影响：大 workspace 下 agent 频繁 grep 时反复全量 IO。yansh 目标 workspace 通常不大，影响有限。
- 建议：低优先；若实测慢可加简单 mtime+pattern 缓存。

### P3. `_solo_drive` 的 `start_tokens` 参数已成死参数
- 证据：[agent.py:4278](../../agent.py#L4278) 签名收 `start_tokens`，函数体内**从未使用**（token 提醒早已改为按轮次 `_SOLO_BUDGET_ROUND_FRAC`，见 [agent.py:4300](../../agent.py#L4300)）。所有 6 处调用点都还在传。
- 同类：`_SOLO_TOKEN_BUDGET`（[agent.py:139](../../agent.py#L139)）定义后也无实际消费点。
- 建议：清理死参数/死常量，减少误导。无功能影响。

### P4. token 统计的 `used_model` 归因在 cascade 降级时可能错配
- 证据：[llm_client.py:301](../../llm_client.py#L301) `used_model = res.model or QUALITY_CASCADE[0]`。流式路径 `_handle_stream` 里 response.model 被设为传入的 model（准）；非流式靠后端回传的 `res.model`。若后端不回 model 字段且发生了 cascade 降级（主模型失败用了 haiku），会错记到 `QUALITY_CASCADE[0]`（主模型），导致费用按主模型价高估。
- 影响：仅统计/计费偏差，不影响功能。低优先。

---

## 四、正确性细节（minor）

- **m1**：[config.py:407](../../config.py#L407) `/model` 菜单项 1 是 `deepseek/deepseek-v4-flash`，但 config 顶部 DeepSeek/OpenRouter 整段已标"[已弃用]"且默认走 ICA。选它会路由到 `client`（OPENROUTER_BASE_URL=ICA 端点），ICA 不认 deepseek id → 必失败。菜单应移除或改注释。
- **m2**：[main.py:402](../../main.py#L402) `/model` 菜单标 "Claude Opus 4.7"，但 `CLAUDE_OPUS="claude-opus-4-8"`（[config.py:41](../../config.py#L41)）。label 与实际 id 不符（4.7 vs 4.8），纯显示误导。
- **m3**：[main.py:398](../../main.py#L398) `/mode` 用法提示只列 `[plan|code|auto|audit]`，漏了 `solo`（而 `VALID_MODES` 含 solo，且 solo 是默认 mode）。帮助文本与实际能力不符。
- **m4**：[agent.py:916](../../agent.py#L916) `load_history()` 定义但生产路径无调用（仅测试调）。交互 session 启动不加载历史 = 每次重启对话从空开始。若这是有意（避免跨 session 串味）应加注释说明，否则是遗漏的功能。
- **m5**：[main.py:283](../../main.py#L283) `--cwd` 分支调 `set_workspace_dir` + 两个 `_reinit_paths`，但 `linter`/`task_log` 之外，`detect_project_type()` 在其后才跑（[main.py:309](../../main.py#L309)）—— 顺序 OK。但 `_CONFIG_FILE` 在 `set_workspace_dir` 里更新了，`load_project_config()`（[main.py:297](../../main.py#L297)）在 `--cwd` 设定之后调，能读到新 workspace 的 config.json —— 顺序正确，确认无 bug。
- **m6**：[agent.py:3108](../../agent.py#L3108) pyproject 变更后 `pip install -e .` 用裸 `subprocess.run(["pip", ...])` cwd 为当前进程目录（非 workspace），且无 cwd 参数。若 workspace ≠ 进程 cwd（`--cwd` 场景），装的是错目录的包。建议加 `cwd=_get_workspace()`。

---

## 五、做得好的地方（值得保留）

- **路径安全**：`_validate_path`（resolve + is_relative_to + `..`/绝对路径双拦）、`find_memory` 的 slugify+resolve 双校验——这是真正的安全边界，做得扎实。
- **并发治理**：tree-sitter parser 锁、TOOLS 列表读写锁、read_cache thread-local + delta 合并、task_log/snapshot meta 原子写 —— 多 subagent 并发的竞态都覆盖到了。
- **进程树清理**：procutil 统一 spawn_with_pgroup + kill_tree（psutil 优先 + 平台兜底），解决了 npx/shell 包装的孤儿孙进程泄漏。
- **workspace_trust**：项目级 mcp/hooks 配置默认拒绝、trust 后才加载——堵住了 clone 恶意 repo 即 RCE 的洞，这是同类玩具 agent 常忽略的。
- **MCP reader 死锁防护**：server 崩溃时 finally 唤醒所有 pending（[mcp_client.py:238](../../mcp_client.py#L238)），避免 60s 死等。
- **gate 假阴性的层层补救**：collected-0 仲裁、final_verdict 对称化、_ever_completed 兜底——虽然复杂，但每条都对应真实翻车案例，逻辑自洽。

---

## 六、修复优先级建议

1. **C1**（ESC 卡死）——一行修复，体验阻断级，最高优先。
2. **C2**（replace_all 不存在）——加参数或删 prompt，直接影响 coder 成功率与 token。
3. **m6**（pip install cwd）、**m1/m2/m3**（菜单/帮助文本）——低成本，顺手清。
4. **P3**（死参数）、**M5**（pre 模式 move 误拦）——清理项，无紧迫性。
5. **M1/M2/M3**（安全边界）——文档化取舍 + 补 `bash -c`、`extra_args` quote。
