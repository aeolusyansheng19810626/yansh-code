# Task #4 v2 验证：4 个 yansh 缺陷修法生效

承接 [`./20260523_task4_compare.md`](./20260523_task4_compare.md)。task #4 第一次跑下来发现 4 个 yansh 侧问题，逐个修后重跑同 prompt + 同 bug 状态验证。

## 4 个修法

### #1（真 bug）`--json` 模式 stdout 被污染

**原因**：`agent.set_batch_mode(json_output=True)` 只重绑了 `agent.py` 自己的 `console`，其他 7 个模块（`snapshot.py / hil.py / monitor.py / task_log.py / subagent.py / llm_client.py / main.py`）各自 `console = Console()` 没被重绑，仍然往 stdout 写 Rich 渲染。

**修法**：抽 `console_shared.py` 用 `_ConsoleProxy` 单例；所有模块 `from console_shared import console`；`set_json_mode(True)` 把 inner Console 切到 `sys.stderr`。`agent.set_batch_mode(json_output=True)` 调一行 `_set_json_mode(True)`，所有模块同时生效。

**改动**：9 文件 / 净 +/-32 行（机械替换为主，agent.py 实质改 `set_batch_mode`）+ 新文件 `console_shared.py` / `pyproject.toml` 的 `py-modules` 加一条。

### #2（命名混淆）`--mode` help 文本不解释

**修法**：`main.py:argparse` 加每个 mode 的解释——`auto=plan+人工确认+code+test+fix；code=同 auto 但跳过人工确认（仍走 plan）；plan=只输出计划不执行；audit=只读分析`。

### #3（低频）plan 阶段 LLM 偶发返回空内容触发 JSON retry

**修法**：`_call_with_json_retry` 检测空内容时不携带空 assistant 消息进 retry，直接重发原 prompt（省 tokens 也避免 ICA 拒绝空 assistant）。

### #4（行为）dispatch_subagent 派得过早

**原因**：`tools_schema.py` 的 dispatch_subagent description 已有"何时不要用：单文件读 / 一次 grep 这种简单任务直接调底层工具更便宜"，但 task #4 v1 LLM 仍派出"读 3 个函数 + 1 个测试"的 explorer subagent（23K haiku tokens）。schema description 权重不够。

**修法**：`_CODER_ROLE` 的 Tool-call efficiency 节加一条："Don't dispatch_subagent for small tasks"，附 ❌ anti-pattern + ✓ correct usage 各一例。系统 prompt 比 schema description 权重高得多。

## 重跑数据对比

| 维度 | v1 yansh | **v2 yansh (修后)** | 改善 |
|---|---|---|---|
| duration | 87.9s | 89.4s | ~ |
| 工具调用 | 24 | **21** | -12% |
| 总 tokens | 249K | **233K** | -7% |
| sonnet input | 226K | 230K | +2%（自己探查多读了点） |
| haiku input | 23K | **0** | -100% ✓ #4 修好 |
| dispatch_subagent | 1 | **0** | ✓ #4 修好 |
| --json stdout | 污染（混 `[快照]` / `--- diff:` 等） | **纯 JSON 单行** | ✓ #1 修好 |
| `json.loads(stdout)` | ❌ 直接挂 | **✓ 一次成功** | ✓ #1 修好 |
| JSON retry 次数 | 1（偶发） | 0 | （本次没触发，#3 偶发难复现） |
| test_result | pass | pass | = |
| 修法字面 | slugify | slugify | =（共同盲点：都漏 resolve 双校验） |

成本：sonnet $3/M, haiku $1/M（粗估）
- v1: 226K × 3 + 23K × 1 = $0.701
- v2: 230K × 3 + 0 = $0.690
- **净省 ~2%**——但**质的改变**在工具链友好度（stdout 可机器解析）

## v2 vs CC 子 agent

| 维度 | v2 yansh | CC 子 agent |
|---|---|---|
| 用时 | 89.4s | 31.3s |
| 工具 | 21 | 6 |
| Token | 233K | 63K |

CC 仍领先 ~3.7×，但 yansh 的差距从"4× tokens + 不可机器解析"收窄到"3.7× tokens + 工具链可用"。bug-fix 任务的 CC 优势仍在，因为 yansh 的 plan→code 流水线对 1 文件小改动是结构性 overhead。

## 副带发现：5 轮工具调用上限耗尽

v2 stderr 出现：
```
[警告] memory.py 已用尽 5 轮工具调用上限
[警告] tests/unit/test_memory.py 已用尽 5 轮工具调用上限
```

`agent.py:1800` 限制 Coder 阶段对每个 plan file 5 轮工具调用。本次 LLM 在 Coder 阶段反复试 pytest 命令——`cd /workspace && python -m pytest ...`——但 workspace 当前实际是 yansh-code 根目录，cd /workspace 落到不存在的 `/workspace` 目录，pytest 没东西可跑。LLM 反复换变体（`-v` / `--tb=short` / `2>/tmp/test_out.txt; cat` / `hexdump` / `strings` 等）都没拿到测试输出，5 轮耗尽。

最终 LLM 还是修对了（用 search_in_files 直接看 memory.py 找到 bug），但**这是个新发现的 LLM 行为问题**：LLM 对 yansh 的 workspace 路径认知有偏差，倾向于假设 `/workspace` 这种 docker 风格路径。

可能修法（**未做**，记着备查）：
- 在 plan 阶段 system prompt 显式告诉 LLM 当前 workspace 的绝对路径（`/get_workspace()` 已传入但只传了 tree_output）
- 或在 execute_command 工具的 description 里说明 yansh 不会 chroot 到 /workspace

## 状态

✓ #1 / #2 / #4 修法生效  
⚠ #3 偶发问题，本次未触发，看后续重跑遇到再说  
🆕 副带发现 LLM "/workspace 路径"假设问题，记入 backlog

## 数据文件

- `20260523_task4_v2_yansh.json` —— **现在是单行干净 JSON**，可直接 `json.loads`
- `20260523_task4_v2_yansh_stderr.log` —— stderr 含完整跑测过程（含 5 轮警告）
