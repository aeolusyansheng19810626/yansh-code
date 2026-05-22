# AB Test #1：探索任务 — `_dispatch_tool_calls` 并发条件

**提示词**（两边一致）：
> 在 yansh-code 项目里，找到 `_dispatch_tool_calls` 这个函数，告诉我它什么时候并发跑、什么时候串行跑——给出文件路径 + 行号 + 触发条件。

**模型**：Claude Sonnet 4.6（两侧）
**日期**：2026-05-23
**任务类型**：纯探索 / 只读

## 数据对比

| 维度 | yansh (audit mode) | Claude Code 子 agent (general-purpose) |
|---|---|---|
| 用时 | **24.54s** | **21.6s** |
| 工具调用数 | **2** | **4** |
| 工具序列 | `get_symbol_definition` → `task_complete` | `Grep` × 3 → `Read` × 1 |
| 总 tokens | 未记录（`task_log` 没存） | **20,365** |
| 准确性 | ✓ 对 | ✓ 对 |
| 完成度 | ✓ done | ✓ done |

两边结论一致：`_dispatch_tool_calls` 在 `agent.py:1115-1174`，并发条件是"同一轮 ≥2 个 dispatch_subagent"。

## 决策路径差异 ⭐ 最有意思的发现

**yansh 走了"符号级查找"路径**：
```
get_symbol_definition(symbol_name="_dispatch_tool_calls")
  → 一击命中：返回函数体 + 行号 + docstring
  → task_complete
```

**CC 子 agent 走了"text-based search"路径**：
```
Grep("_dispatch_tool_calls")          # 找出现位置
Grep("_dispatch_tool_calls", "agent.py")    # 缩范围
Grep("_dispatch_tool_calls", "subagent.py") # 也试了 subagent（误射）
Read("agent.py")                       # 读完整文件确认
```

**根因**：
- yansh 的工具集里有 **`get_symbol_definition`**——基于 tree-sitter，对"找函数/类定义"这种任务直接定位
- CC 子 agent 的 general-purpose 工具集只有 **`Grep` + `Read`**——必须先 text search 再读文件，对函数定义任务多走 2 步

这不是"哪个更聪明"的问题，是**领域工具 vs 通用工具**的取舍：
- yansh 是"代码 agent"，工具集里塞了 symbol-aware 的工具 → 探索代码任务路径短
- CC 子 agent 是 general-purpose（通用 → 知识工作各种类型），用更基础的原语，路径长但通用性强

## 输出风格差异

**yansh** 实际有 **两层输出**：
1. **stderr**（rich console）：完整 markdown 报告——分段、表格、源码引用
2. **stdout** JSON：精简 task_complete summary（一行）

**CC 子 agent** 单层：
- 紧凑：4 行结论 + 文件路径 + JSON 元数据块（按要求附的）

如果只看"用户屏幕上显示了什么"——yansh 显示的更详细（stderr console 完整 markdown），CC 显示的紧凑实用。

## 决策深度差异 ⭐

**yansh 引用了源代码 docstring 原文**（设计意图）：
```
本地工具 几毫秒，并发开销得不偿失 → 始终串行
写工具必须串行（HIL/confirm 顺序依赖）→ 始终串行
子 agent 是唯一长耗时操作 → ≥2 个时并发
```
这段是从 `agent.py:1118-1124` 读到的——`get_symbol_definition` 一次返回了**函数体 + docstring**，所以 yansh 不光知道"在哪"还知道"为什么这么设计"。

**CC 子 agent 没读 docstring**——它 grep 拿行号，再 read 文件，但 read 的是文件全部内容，给的回答只看代码逻辑没引设计意图。

这是符号级工具的另一个隐藏价值：**带 docstring 一起返回 → 答案能上升一层**（从"代码做什么"到"为什么这么做"）。

## 跑了几轮 LLM

- **yansh: 2 轮**（审计轮 1 = get_symbol_definition；审计轮 2 = task_complete）—— 第一轮就拿到答案，第二轮收尾
- **CC 子 agent: 1 轮 LLM 含 4 工具调用**（current Anthropic API 一次 LLM 响应可含多 tool_call）

yansh 的 24.54s 包含 **2 次** API round-trip + 1 次 tree-sitter parse；CC 的 21.6s 是 **1 次** round-trip + 4 工具计算。两边 wall clock 差不多，因为 yansh 工具计算便宜（tree-sitter 单次 < 50ms），CC API round-trip 单次贵但工具便宜。

## cwd 实证差异

- **yansh** 真的去读 `/tmp/ab_test/yansh-clone/agent.py`（被 `--cwd` 强制）
- **CC 子 agent** 实际去读 `C:/Users/ShengYan/Projects/yansh-code/agent.py`（**忽略了我提示词里的 cwd 提示，自己选了 main 项目**）

代码内容一样所以结论一致，但**隔离层级不同**——这是 task #2/#3 涉及修改时必须解决的问题。

## 数据收集 gap（管线问题）

1. **yansh `--json` 没存 token 数**——`task_log.py:_current_task_log` 没 token 字段。`task_log_signal` 也没。需要补一个 commit：从 `llm_client._session_tokens_by_model` 拉数字落盘。
2. **CC 子 agent 不能强制 cwd**——我提示了 `/tmp/ab_test/yansh-clone`，它实际读 `C:/Users/ShengYan/Projects/yansh-code/`（原 main）。本任务两份代码相同所以无影响，但 task #2 / #3 涉及修改时这是真的隔离破洞——后续要让 CC 子 agent 实际 cd 进 clone 目录。

## 管线校准结论

✅ 两边都能跑、能拿到结构化结果、能对比
⚠️ yansh 的 token 数据漏了，task #2 之前要修
⚠️ CC 子 agent cwd 实际生效需要明确

## 后续建议

继续 task #2（`tools.read_file` 加 `max_bytes` 参数）前要做的：
1. 修 `pyproject.toml` 缺的 10 个 py-modules（让 `yansh` CLI 能跑而不是 fallback `python main.py`）
2. 给 `task_log` 加 token 字段（从 `llm_client` 拉）
3. CC 子 agent prompt 显式包含 `cd /tmp/ab_test/yansh-clone &&` 命令风格的引导

是否继续？
