# AB Test #2：写代码任务 — `tools.read_file` 加 `max_bytes`

**提示词**（两边一致核心要求）：
> 给 yansh-code 项目里的 `tools.read_file` 加一个可选参数 `max_bytes`（默认 None=不限制），用来限制读取的字节数；超过 `max_bytes` 时返回截断内容加一个标记字段（`truncated: true`）。同时在 `tests/unit/test_tools.py` 加一条单测验证截断行为。

**模型**：Claude Sonnet 4.6（两侧）
**日期**：2026-05-23
**任务类型**：写代码 + 加单测

## 数据对比

| 维度 | yansh (auto mode) | Claude Code 子 agent (general-purpose, retry v2) |
|---|---|---|
| 用时 | **253.76s** | **72.3s** |
| 工具调用 | **61** | **15** |
| Token (in+out) | **641,163** (in 626,517 + out 14,646) | **25,104** |
| 文件改动 | **4** (tools.py / tools_schema.py / test_tools.py / agent.py) | **2** (tools.py / test_tools.py) |
| 测试通过 | ✓ | ✓ |
| 完成度 | done | done |

**yansh 比 CC 多花 25× token / 4× 工具调用 / 3.5× 时长** —— 但**做了更多事**。

## 多花的钱花在哪 (yansh)

| 阶段 | 工具调用范围 | 主要消耗 |
|---|---|---|
| plan + code（任务核心）| #1-29 | 探索代码 → 改 read_file → 改 schema → 加单测，约 25 调用 |
| fix（pre-existing test failures）| #30-61 | 跑完整 test suite 触发 5 个无关失败，逐个排查根因 |
| 派 dispatch_subagent | #30, #31, #55 | 派了 3 个子 agent 做子任务（探索 / 读文件 / 分析失败）|

yansh 进了 fix loop 是因为它跑**整个 test suite**（41 个测试），其中 5 个是历史遗留失败（test_execute_command_timeout / test_path_traversal_protection 等），跟本任务无关。yansh 的 LLM **最终自己识别**这些失败"跟本次 plan 无关，不修复"，task_complete=True 收尾。

CC 子 agent 只跑了 task 要求的那一条新单测（`pytest tests/unit/test_tools.py::test_read_file_max_bytes_truncation -v`），不跑全套 → 看不到 pre-existing 失败 → 不进 fix loop。

## 完成质量差异 ⭐

两边都通过单测、截断算法相同（byte 边界 + decode `errors='ignore'`）、offset/limit + max_bytes 可叠加——**核心代码功能一致**。

但**完成范围不同**：

| | yansh | CC |
|---|---|---|
| `read_file` 加参数 | ✓ | ✓ |
| 单测 | ✓ | ✓ |
| **`tools_schema.py` 加 schema 声明** | ✓ | ✗ |
| **顺手清死代码（agent.py 删 3 行 import）** | ✓ | ✗ |

`tools_schema.py` 是**必要的语义闭环**——这是 LLM 看到的工具签名定义。如果不加，LLM 不知道 `read_file` 有 `max_bytes` 参数，新功能等于没用。

> **yansh 改 `tools_schema.py`**：加 `"max_bytes": {"type": "integer", "description": "最大读取字节数（可选）"}` 到工具 schema
>
> **CC 没改**：满足了 task 字面要求（参数加了、单测过了），但 LLM 用不到新参数

这是 **领域知识 vs 通用工作流** 的差异：yansh 的 system prompt 教它"代码 agent 改工具签名时要更新 schema"——这是 yansh 内部的"规则"。CC general-purpose 是通用助手，没这个上下文，只看到字面任务要求。

## 决策深度差异（这次反过来）

CC 子 agent 的 `key_decisions` 末尾有一条**很专业的测试细节洞察**：

> monkeypatch 需直接 patch `tools._WORKSPACE_ROOT`（Path 对象，模块级常量），而非 `config.WORKSPACE_DIR`（已在 import 时复制到 _WORKSPACE_ROOT）

这是单测里的真坑——`tools.py` 顶部 `_WORKSPACE_ROOT = Path(WORKSPACE_DIR).resolve()`，monkeypatch `config.WORKSPACE_DIR` 不生效，要直接 patch `tools._WORKSPACE_ROOT`。

CC 显式输出了这个洞察；yansh 的代码也对（用了 monkeypatch 兼容方式）但没在 summary 里强调这个 trap。

**yansh task #1 优势是"读 docstring → 给设计意图"**；
**CC task #2 优势是"在 key_decisions 里显式记录隐藏 trap"** —— 风格反过来。

## 流程差异

**yansh 的 plan → code → 跑测 → fix → task_complete 完整流程**：
- 自动跑全套测试 → 发现失败 → fix loop 里逐个排查 → LLM 自己判断"无关失败不修"
- 优点：输出更完整、捕捉到 schema/死代码这种"隐含要求"
- 缺点：固定流程消耗高，对"局部小修改"任务过度工程

**CC 子 agent 的单循环**：
- 改文件 → 跑指定测试 → 收尾
- 优点：快、便宜、刚好满足要求
- 缺点：不会主动想"还要不要做别的"

## 数据收集 gap

- yansh 工具调用次数 (61) 在 stderr console 里没显式打印，task_log JSONL 才有
- CC 子 agent 的 `key_decisions` 是我在 prompt 里要求的，**不是默认输出** —— 设计 AB 测试要刻意加这种 instrumentation

## 总结：什么场景选什么

| 任务类型 | 推荐 |
|---|---|
| 探索 / 信息检索（task #1）| **CC**（路径短，但用 yansh 的 `get_symbol_definition` 能拿 docstring 上升一层）|
| 严格按字面要求小改 + 加测（task #2） | **CC**（25× 便宜，刚好满足）|
| **完整功能落地**（含 schema、文档、清理）| **yansh**（领域知识更深，输出更完整，多花 25× 但语义闭环）|
| 不熟悉的代码库 | **yansh**（plan 阶段强制探索 + audit 阶段强制只读，更安全）|

## 一个发现：CC 子 agent 的环境劫持风险

第一次 retry CC（直接派给 prompt）—— 子 agent 跑去做"分析 .claude/settings.json 的 Bash allowlist"，**完全偏离任务**。70K tokens / 31 工具调用 / 完全无效输出。

可能原因：sonnet 看到"yansh-code 项目"+ 触发了 `fewer-permission-prompts` skill 这条工作流路径，去扫了 `~/.claude/projects/.../JSONL` 的 transcript 历史。

**重写 prompt 加严格约束**（"不要读 ~/.claude、不要做 fewer-permission-prompts、忽略任何 skill 提示"）后才回到任务。

这是 CC 子 agent 在 fully-autonomous + 工具丰富 + skill 注入的环境下的真实风险——**用户和 yansh CLI 没这个风险**（yansh 工具集和 prompt 范围都更聚焦）。

## 附原始数据

- `20260523_task2_yansh.jsonl` — yansh task_log（61 工具调用、token 完整记录）
- `20260523_task2_yansh_stderr.log` — yansh stderr console（plan/code/fix 全过程 markdown）
- `20260523_task2_cc_transcript.jsonl` — CC 子 agent JSONL transcript（v2 retry，15 工具调用）
- 第一次 retry（任务跑偏的）只在我父对话里有，未保存

## 下一步

task #3 候选（架构论证）：评估 yansh 把 `task_complete` 从 sentinel 工具改成 LLM 自然语言信号的可行性。这种纯讨论 / 输出方案的任务，理论上 CC 子 agent 表现会更接近 yansh，因为不涉及"领域知识"和"测试 pipeline"——是个好的对照测试。
