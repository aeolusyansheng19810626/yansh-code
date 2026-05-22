# P2 #9b 子 Agent 并发执行

承接 [_18](./2026-05-22_18-subagent.md)：用户提了个好问题——"大项目主 agent 只派一个子 agent
还是按量分多个？"

## 现状（_18 完成时）

- LLM 可以一次 response 返回多个 dispatch_subagent tool_call（OpenAI 协议支持）
- 但主 agent loop 是 **for tc in msg.tool_calls 串行**——前一个跑完才跑下一个
- N 个子 agent 总耗时 = N × 单个耗时

Claude Code 的 Task 工具：能并发跑——其系统 prompt 里明确写
"send them in a single message with multiple tool uses so they run concurrently"。
yansh 这一波就是把这个能力补上。

## 改了什么

### 1) `_IN_SUBAGENT` 全局 flag → `threading.local`

并发跑多个子 agent 时每个线程独立——`_subagent_state.in_subagent`：
```python
_subagent_state = threading.local()
def _is_in_subagent(): return bool(getattr(_subagent_state, "in_subagent", False))
def _set_in_subagent(v): _subagent_state.in_subagent = bool(v)
```

实际防递归靠"工具集物理过滤 dispatch_subagent"——thread-local flag 只是同线程内的额外保险。

### 2) `_SUBAGENT_STATS` 加 `threading.Lock`

并发更新计数器要原子：
```python
with _SUBAGENT_STATS_LOCK:
    _SUBAGENT_STATS["calls"] += 1
    ...
```

### 3) 抽取 `_dispatch_tool_calls(tool_calls, *, ...)` helper

**核心策略——只对 dispatch_subagent 并发**：
- 本地工具（read/grep/list_files）几毫秒，并发开销得不偿失
- 写工具必须串行（HIL/confirm 顺序依赖、console 输出可读）
- 子 agent 是唯一长耗时（多轮 LLM call），并发收益最大

实现：
```python
sub_indices = [i for i, tc in enumerate(tool_calls)
               if tc.function.name == "dispatch_subagent"]

if len(sub_indices) >= 2:
    with ThreadPoolExecutor(max_workers=min(len(sub_indices), 4)) as ex:
        # 并发跑 subagents
        ...

# 剩余串行处理（含单个 subagent、所有非 subagent 工具）
for i, tc in enumerate(tool_calls):
    if outs[i] is None:
        outs[i] = _dispatch_tool_call(tc, ...)

# 按原顺序拼回 messages（OpenAI 协议要求 tool_call 与 tool result 顺序对应）
for out in outs:
    _record_dispatch(out, messages)
```

`_SUBAGENT_CONCURRENCY_CAP = 4`——thread pool size 上限。

### 4) 4 处 tool_calls 循环替换

`audit() / plan_chat() / _run_subagent() / fix()` / `code()` 内的循环都换成 helper。
sentinel 检测（task_complete / plan_draft_update / exit_plan_mode_signal）从"边跑边检测"
改成"全跑完后扫一遍 outs"——并发后所有结果都到了再统一处理。

`_auto_generate_tests` 不改——它有自己的特殊 tool 处理（不走 _dispatch_tool_call）。

### 5) dispatch_subagent schema description 加并发提示

> "**多分支并行调研**——一次 response 里发多个 dispatch_subagent tool_call，会**并发跑**
> （最多 4 个同时），总耗时≈max(单个) 而不是 sum。例：分析 A/B/C 三个模块怎么用，
> 一次发 3 个 dispatch_subagent 比串行查快 3×。"

## 验证

### 单测（tests/unit/test_subagent.py，新增 7 条，总 29 条全过）

新增覆盖：
- `test_dispatch_tool_calls_helper_serial_for_non_subagent` — 非 subagent 走串行 + outs 顺序
- `test_dispatch_tool_calls_concurrent_subagents` — 3 个 subagent，验证 max active ≥2 + 总耗时 < 0.8s（串行 0.9s）
- `test_dispatch_tool_calls_single_subagent_serial` — 1 个 subagent 不启 thread pool
- `test_dispatch_tool_calls_concurrency_capped` — 6 个 subagent + cap=4，验证 max active ≤ 4
- `test_dispatch_tool_calls_mixed_subagent_and_local_tools` — 混合工具时 outs 严格按原顺序
- `test_dispatch_tool_calls_subagent_exception_isolated` — 一个 subagent 抛错不影响其他
- `test_subagent_stats_lock_concurrent_increments` — 并发 5 个后 stats.calls=5（验证锁有效）

旧测试改了 2 条：`agent._IN_SUBAGENT = True` → `agent._set_in_subagent(True)`；
`agent._IN_SUBAGENT is False` → `agent._is_in_subagent() is False`。

13/13 文件全过。

### 集成基准（ICA Sonnet 4.6，3 个完全独立任务避免 prompt cache）

```
串行 3 个 subagent: 33.3s
并发 3 个 subagent: 13.9s
加速比: 2.40×
```

理论上限 3×（3 个独立 LLM call 同时跑），实际 2.4× 是因为：
- prompt cache miss penalty（首次串行的 cache 未命中分摊到第一次串行）
- ICA 网关有 token 速率限制——并发 3 个的 token throughput 受限
- LLM 响应时间本身有方差（fastest 拖慢 slowest）

**集成 audit 跑下来**（让主 agent 用 dispatch_subagent 派 3 个看 3 个文件）：
- 主 agent 一次 response 返回 3 个 dispatch_subagent tool_call ✅
- 控制台 `[审计轮 N] [subagent 并发] 3 个子 agent 同时启动` ✅
- stats.calls=3, total_steps=6（每个 2 步）✅
- 主 agent 基于 3 份 summary 给出汇总表 ✅

## 评估

### 跟 Claude Code 收窄到了多近

| 维度 | Claude Code Task | yansh dispatch_subagent |
|---|---|---|
| context 隔离 | ✅ | ✅（_18） |
| role 切工具集 | ✅ | ✅（_18） |
| 防嵌套递归 | ✅ | ✅（_18） |
| **一次发多个并发跑** | ✅ | ✅ **本波** |
| 后台跑 (run_in_background) | ✅ | ❌ |
| 完整子 agent 转录可查 (TaskOutput) | ✅ | ❌（只有 last_summary 截断） |
| 主动取消子 agent (TaskStop) | ✅ | ❌ |

差距收窄到"observability + 后台执行"，不再是核心架构差异。

### 工程意义

并发的真实价值不是"快 3 倍"——是**让 LLM 学会拆任务**。
schema 里加了"分析 A/B/C 三个模块怎么用一次发 3 个 dispatch_subagent"的提示后，
Sonnet 4.6 在自由 audit 任务里就**会主动并发**——不用人指示。
这才是把"并行思维"内化到 agent 行为模式里。

### 边界情况（已验证 OK）

- 一个并发 subagent 抛错 → 其他不受影响；该 subagent 的 result 是 internal error
- 单个 dispatch_subagent → 不启 thread pool（避免无谓的 thread 创建开销）
- 混合 dispatch_subagent + read_file → outs/messages 严格按原顺序
- stats 在 5 个并发同时更新下不丢计数（锁有效）
- 同步线程内 `_set_in_subagent(True)` 后再调 `_run_subagent` → 仍然递归拦截

## 不做（留给后续）

- 子 agent 后台执行：派完不阻塞父 agent，事件回调（参考 Claude Code `run_in_background`）
- 子 agent 中途取消：父 agent 决定不要 B 了，主动 stop 该 thread
- 子 agent 完整转录回查：当前 last_summary 只存 500 字截断；要完整 messages 需要
  写入磁盘（或加 in-memory ring buffer）
- thread pool 复用：当前每次 helper 调用新建 ThreadPoolExecutor，N 次并发就建 N 个
  pool。pool 复用要小心 thread-local 状态泄漏

## 关键文件

| 文件 | 改动 |
|---|---|
| `agent.py` | `_IN_SUBAGENT` → `threading.local`；`_SUBAGENT_STATS_LOCK`；`_SUBAGENT_CONCURRENCY_CAP=4`；`_is_in_subagent` / `_set_in_subagent`；`_dispatch_tool_calls` helper；4 处循环替换 |
| `tools_schema.py` | dispatch_subagent description 加并发提示 |
| `tests/unit/test_subagent.py` | +7 条并发测试，旧 2 条改用新 API |
