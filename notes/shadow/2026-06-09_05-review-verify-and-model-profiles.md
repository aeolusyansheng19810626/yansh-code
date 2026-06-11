# Solo Mode 第三方 Review — 源码核实结论 + 各模型强项画像

> 第一轮四方 review（deepseek v4 / gemini 3.1 / gpt-5.5 / opus 4.8）结果在 `./2026-06-09_02-solo-code-review-result.md`。
> 本文是我对照源码做的**逐条裁决**（哪些真 bug、哪些误报、哪些设计权衡）+ 各模型擅长维度画像。
> 第二轮定向深挖 prompt：`./2026-06-09_03-review-deepdive-gpt.md`、`./2026-06-09_04-review-deepdive-opus.md`；补充源码 `./review_src_20260609/07-10`。

## 一、确认的真 bug（必修，按优先级）

| # | finding | 谁发现 | 核实依据 |
|---|---|---|---|
| 1 | gate `signal["success"]=True` 覆盖 agent 失败/放弃信号 | **gpt 独家** | 04:190/212。agent 沉默/熔断/`task_complete(success=false)` 后，只要旧测试碰巧绿就被覆盖成成功 → 误报。逻辑硬伤 |
| 2 | test gate 走 30s 默认超时（`test()→execute_command(_timeout_sec=30)`） | **opus 独家** | 06:3。大项目 pytest 必超时判红、烧满 8 轮，把能过的任务系统性误判。需跨 04↔06 追链才看得到 |
| 3 | compact 丢开场规划（与 `_SOLO_ROLE` "plan is your anchor" 冲突） | 4 家共识 | 01:21-30 head 只 pin `[system,user]`；`_SUMMARIZE_SYSTEM`(1331-1342) 强制保「改动文件+成功命令」但**不保**开场规划/接口契约 |
| 4 | `raw = stderr or stdout` + `raw[-4000:]` 丢 stdout traceback | **gpt 独家** | 04:197-198。pytest 失败详情在 stdout，stderr 有 warning 就丢掉真 traceback |

## 二、误报 / 已证伪（不必修）

| finding | 谁报的 | 证伪依据 |
|---|---|---|
| threading.Lock 多进程数据损坏「高危」 | deepseek/gemini/gpt 判高危 | **误报**：`parallel_orchestrator.py` 每子任务独立 git worktree + 独立进程 `main --cwd <wt>`，`agent_state.md` 落各自 worktree，**无跨进程共享**。同进程内 subagent 并发(ThreadPoolExecutor)写同一 state 时 threading.Lock 正确生效。**唯 opus 严谨标「待确认 workspace 是否共享」** |
| `pythonic_tool` "不会被误匹配" | deepseek 明确判不会 | **deepseek 判断错**：`.match` 只锚行首，`python[0-9.]*` 可匹配 0 个数字，`pythonic_tool` 确实命中。opus/gpt/gemini 判会误匹配正确（低危污染） |
| `_parse_pytest_failures` "实现不完整" | deepseek | **误判**：那是 sed 导出到 246 行的截断，非代码缺陷 |
| `out["result"]` 非 dict 崩溃 | deepseek | 基本可证伪：`_dispatch_tool_call` 各分支 result 恒为 dict(1588/1607)。低风险，第二轮让 opus 确认 inner |

## 三、真问题但需确认 / 属设计权衡

- **正则缺 `\b`**（05:30 `^\s*(py\b|python[0-9.]*|pytest)`）：python/pytest 分支无词边界，真误匹配，低危（仅污染状态文件）。确定真。
- **`raw[-4000:]` 丢头**：deepseek/gemini/gpt 说丢头部 vs **opus 说尾部够不算 finding**。裁决：对标准 pytest，FAILURES/summary 在尾部，opus 基本对；但 execute_command 已头尾各截 3000，再砍头不如统一 `_truncate`。算改进项，非严重。
- **no_progress 把任意 execute_command 当进展**（04:65，cat/ls 可绕过）：gemini/gpt 提，但 **opus 读懂了 R10 注释（连跑验证曾被误杀）→ 有意权衡**。gpt 的"仅写操作后的验证算进展"最精细。
- **gate 不尊重 `task_complete(success=false)`**：gemini 判"死循环烧光"高危过头，**opus 判低危更准**（有 gate_max=8 + soft_limit=120 + 熔断三重上限，不会无限）。
- **M1 compact 边界**：`_split_messages_into_pairs` 把 assistant+tool_result 绑同一 pair → 孤立 tool_result 基本不可能，M1 检查充分。opus 上轮标待确认，已钉死为安全（第二轮请 opus 复核退化路径）。
- **gate scope 落空机制**：`_infer_test_scope` 找不到 `test_<stem>.py` 返回 [] → agent 不留测试时 gate `break` 跳过复核。结合 #1 = 「agent 自称成功 + 无测试 break → 零复核报成功」，可能比单 bug 更系统性。第二轮让 gpt 穷举触发链。

## 四、各模型强项画像（用于后续选型）

| 模型 | 信噪比 | 最擅长 | 典型表现 |
|---|---|---|---|
| **gpt-5.5** | 高 | 控制流/数据流真实 bug、成功失败信号语义 | 独家 #1 #4，都是「会真跑错」的硬伤；正则修复给最完整版 |
| **opus 4.8** | **最高** | 跨文件调用链追踪 + 真伪裁决 + 读懂设计意图 | 独家挖出最隐蔽 #2；唯一对锁严谨标「待确认」；多条给「经核对正确，不算 finding」反向结论且基本成立 |
| **gemini 3.1** | 中 | 业务语义/概念污染、token 二阶效应、长链稳定 | 独到：「pytest 失败划黑名单会误导未来 agent 抛弃自测」（概念层）、compact「套娃式总结」。弱点：严重度爱夸张 |
| **deepseek v4** | 偏低 | 输入清洗/编码边界、防御性守卫 | 独到：Unicode NFKC、非 dict 守卫。弱点：量大噪音多 + 正确性翻车(pythonic_tool)+ 误判(导出截断) |

**选型一句话**：抓真能跑挂的逻辑 bug→gpt；要可信免复核的裁决+最隐蔽缺陷→opus（本次综合最强）；架构/语义长期行为→gemini；输入边界穷举→deepseek（结论需复核）。

## 五、待办

第二轮深挖结果回来后，合并两轮结论，按「确认真 bug」清单（#1-#4 + 正则 \b）开修。修复顺序建议：#1（信号覆盖，逻辑硬伤）→ #2（30s 超时，系统性误判）→ #3（compact 丢规划，pin 首个 assistant pair）→ #4（回灌通道）→ 正则 \b。锁加原子写为可选低优。
