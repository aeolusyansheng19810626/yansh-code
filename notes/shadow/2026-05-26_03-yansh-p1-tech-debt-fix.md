# yansh P1 技术负债集中修复 plan

## Context

**起因**：2026-05-25 三方 AB-test（cc / yansh / yscode）跑完 5 个 task，yansh 暴露 7 条 P1 级机制问题（#1-#7）。其中：
- #2 烧 91 万 token 改 task 范围外（plan 不消费 coder "无需修改" 信号）
- #4 静默失败漏修 bug（baseline failure 识别误吞用户请求）
- #5 CLI crash（_err 签名不一致 — task #5 暴露的就是它本身）

**目标**：集中半天-1 天清掉 7 条中的 6 条（#1 已漂移，降级），AB 回归 task #2/#4/#5 yansh 这次过。

**硬约束**：
- Implementor 用 Sonnet 4.6（已 AB 验证 15 次 dispatch 稳定）
- Reviewer **必须独立 context** — 用 Agent tool 派 subagent，prompt 不预设结论
- Reviewer 用 Opus 4.7（reasoning 强，能 challenge implementor 的隐含假设）
- 测试不派 LLM agent，直接 bash 跑 pytest

## 修复策略

**串行 + 分级 review + 独立 commit**：

| 阶段 | 任务 | review | 单测 | 累计估时 |
|---|---|---|---|---|
| 暖身 | #7 → #2 → #3 | 不派（机械改） | 每条改完跑 | ~2h |
| 漂移处理 | #1 降级到 P3 监控 | n/a | n/a | 5min |
| 协议层 | #6 → #4 → #5 | 每条派 Opus reviewer | 每条改完跑 | ~1.5 天 |
| 总验证 | AB-test 回归 | 人工 git diff 总览 | pytest 完整套件 | ~30min |

**Commit 策略**：每条 P1 一个独立 commit，message 格式 `fix(P1 #N): <subject>`，方便回滚和 bisect。

## 详细方案

### P1 #7 — _err 签名不一致（30min）

**File**: `tools.py:25` + `agent.py:1236, 1244, 1248, 1310`

**改 `tools.py:25`** 加 `tool` 形参：
```python
def _err(kind: str, msg: str, tool: str = None, **extra) -> dict:
    result = {"error": kind, "message": msg, **extra}
    if tool:
        result["tool"] = tool
    return result
```

agent.py 4 个 callsite 不变（继续 `_err("internal", "msg", name)`），name 现在能正确落到 tool 形参。

**单测**：构造 LLM 给 `search_in_files` 传非法 regex（如 `missing )`）的场景，断言 CLI 不 crash、错误返回结构含 `tool` 字段。文件位置：`tests/unit/test_tools.py`（已存在）。

### P1 #2 — 用尽轮次假警告（< 1h）

**File**: `agent.py:1836-1840`

当前条件 `attempts_left <= 0 and response_message.tool_calls`，**未检查最后一轮是否调了 `task_complete`**。

**改法**：警告前加判断 — 上一轮 tool_calls 里若有 `task_complete` 则不警告。

**单测**：模拟"task_complete + 用尽轮次"路径，断言不进警告分支。

### P1 #3 — Detector 扩 NameError / AttributeError（< 30min）

**File**: `agent.py:2374-2392`

当前 regex 仅匹配 `TypeError missing argument`。加：
```python
r"NameError:\s+name\s+'.+?'\s+is\s+not\s+defined"
r"AttributeError:\s+'.+?'\s+object\s+has\s+no\s+attribute"
```

**单测**：构造 NameError / AttributeError 输出，断言 detector 命中并追加 fix 预算。

### P1 #1 — 降级处理（5min）

Explore 报告确认当前代码**无 `/workspace` 字面路径假设**（`tools_schema.py:39` 用泛化文案，`agent.py:1599` 注入运行时 `_get_workspace()`）。memory 里的描述基于 task #4/#5 v3 的旧 commit 状态。

**动作**：在 memory `project_yansh_tech_debt.md` 里把 #1 标 `[已漂移 / 解决]`，移出 P1，转 P3 观测项 — 下次跑 AB 时关注 fix loop 是否还有路径假设错误。

### P1 #6 — Baseline 识别误吞用户请求（半天）

**File**: `agent.py:2705-2709`（capture）+ `agent.py:2792-2821`（subset 比较）

**问题**：current vs baseline subset → 视为通过，没区分"用户明确要求修的失败" vs "无关 pre-existing"。

**改法（推荐 a 方案）**：在 subset 判定前加 prompt 关键词过滤：
- 若用户 prompt 含 "修" / "测试失败" / "fix" / "failing test" / "bug" 等关键词，**禁用 baseline subset 比对**（强制走完整 fix loop）
- 关键词列表硬编码 + 大小写不敏感

**单测**：mock 用户 prompt 含/不含关键词两种场景，断言 baseline 比较是否生效。

**Reviewer (Opus, 独立 context)**：派 subagent 看 git diff，prompt 里给：
- 原始问题描述（从 memory `project_yansh_tech_debt.md` P1 #6 抄）
- 当前 diff
- 不预设结论；让 reviewer 独立判断"是否解决 + 是否引入新问题（如：用户没说修测试但实际测试有 pre-existing failure 时被错误激活 fix loop）"

**回归**：跑 AB-test task #4 yansh 这次应能修对。

### P1 #4 — Plan 不接受 coder "无需修改" 信号（半天）

**File**: `agent.py:1828`（coder task_complete 处理）+ `agent.py:2727-2737`（run() 流程）

**改法**：coder `success=True` 且 summary 命中"无需修改" / "已实现" / "no changes needed" 关键词时，plan 主动跳过剩余 expected_edits 子任务，**直接进入 fix/test 阶段**。

**单测**：mock coder summary 含/不含关键词，断言 plan 是否短路。

**Reviewer (Opus)**：独立 context 看 diff，重点 challenge："关键词过滤会不会误吞 coder 的真实工作？比如 coder 改了 1 个文件后说 '其余 3 个无需修改'，会被识别成全无需修改吗？"

**回归**：跑 AB-test task #2 yansh，预期 token 从 915K 大幅下降。

### P1 #5 — Plan 写文档前必须 explorer（半天）

**File**: `agent.py:1599-1628`（plan system prompt）

**改法**：plan agent 检测到任务 prompt 含"具体行号" / "改动范围" / "兼容分析" / "代码细节"等关键词时，**先派 explorer subagent**（general-purpose, sonnet）扫描相关文件，把扫描结果作为 plan 上下文，再生成文档。

或更简单方案：plan system prompt 加规则 — "涉及具体代码描述前必须调 read_file 至少 1 次相关文件"，并在 plan 阶段开放 read_file 工具（当前 plan 阶段无工具）。

**待 reviewer 决定哪个方案更优**。

**Reviewer (跟 #4 合并一次 review)**：独立 context，问"两条改动是否在 plan 流程中互相冲突"。

**回归**：跑 AB-test task #3 yansh，预期文档准确度从 5/8 提升到 ≥ 7/8。

## 验证

**每条改完**：
```cmd
pytest tests/unit/test_<相关>.py -v
```

**全部改完**：
```cmd
pytest                                  # 完整套件
git log --oneline main..HEAD            # 确认 6 个独立 commit
git diff main..HEAD --stat              # 总览扫
```

**AB 回归**：
```cmd
cd C:\Users\ShengYan\Projects\AB-test\yansh\yansh-code
yansh code <task-2 prompt>
yansh code <task-4 prompt>
yansh code <task-3 prompt>     # 验证 #5
```

预期：3 个 task yansh 这次都过（之前是 ❌/❌/⚠️）。

## Reviewer agent 调用模板

```python
Agent({
  subagent_type: "general-purpose",
  model: "opus",
  description: "Review P1 #N implementation",
  prompt: """
你的任务：独立 review yansh-code 里 P1 #N 的修复实现。

**原始问题**：
<从 memory project_yansh_tech_debt.md 复制 P1 #N 描述>

**当前修复**：
<git diff 输出>

**审查重点**：
1. 修复是否解决了原始问题？
2. 是否引入新问题（edge case / 误吞 / 性能退化）？
3. 关键词过滤策略是否过于宽松或过于严格？
4. 是否有未覆盖的触发场景？

**不要**预设结论。如果实现已正确，明确说 "approve + 理由"；如果有问题，列出具体行号 + 修改建议。
"""
})
```

## 已知风险

1. **agent.py 4 条 P1 都改它**（#7 / #2 / #6 / #4），串行做避免冲突。
2. Reviewer 是 Opus 独立 context，单次成本 ~80K token，3 次约 240K — 可接受。
3. AB 回归在 `AB-test/yansh/yansh-code` workspace 跑，不影响主仓 main 分支状态。
4. **#5 方案二选一**（关键词触发 explorer 子 agent vs plan 阶段开放 read_file 工具）由 reviewer 评议后决定，可能要 1 轮迭代。

## 数据来源

- yansh 技术负债清单：`memory/project_yansh_tech_debt.md`
- 代码事实 verify：本次 explore agent 报告（agentId: a327058e63f133fcb）
- ICA 配置陷阱（reviewer 用 sonnet/opus 不用 haiku 的原因）：`memory/reference_ica_claude_code_config.md`
