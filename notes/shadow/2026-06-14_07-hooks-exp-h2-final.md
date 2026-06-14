# H2 系列最终对比：bug 修复前后 + 对照组

参考：
- [H2-v4](./2026-06-14_05-hooks-exp-h2v4-results.md)
- [H2-v4-noHook](./2026-06-14_06-hooks-exp-h2-nohook-results.md)
- bug 修复：agent.py PostToolUse/PreToolUse system_messages 注入

## Bug 修复摘要

**根因**：`_dispatch_tool_call_with_hooks` 处理 PostToolUse/PreToolUse 结果时，
`hr["system_messages"]` 被静默丢弃，未传入 LLM。

**修复**（agent.py，4 处）：
1. 新增 `_hook_sys_msgs: list = []` 累加器
2. PreToolUse 非 block 分支末尾：`_hook_sys_msgs.extend(hr.get("system_messages", []))`
3. PostToolUse 分支末尾：同上
4. `_dispatch_tool_calls` 的所有 tool result append 完成后统一追加一条 `role:system` 消息

**Opus review**：LGTM，全量 unit tests 22/22 通过。

---

## 三组完整对比（同一 sliding_window 任务，9 failed 初始）

| 指标 | H2-v4（hook 损坏）| H2-v4-noHook | H2-v4-fixed（hook 修复）|
|---|---|---|---|
| 成功 | ✅ | ✅ | ✅ |
| 黑盒 | 全过 | 全过 | 全过 |
| 轮次 | **6** | 4 | 4 |
| 工具序列 | read×2, replace×3, exec, done | read×2, **write**, exec, done | read×2, **write**, exec, done |
| 每次写修 bug 数 | 1（逐一）| 4（批量）| 4（批量）|
| hook 触发 | 3（replace 命中）| 0 | **1**（write 命中）|
| hook 注入 LLM | **0（bug：丢弃）** | 0（无 hook）| **0（正确：全过静默）** |
| `[hook 注入]` 行 | 0 | 0 | 0 |
| cost | $0.22 | $0.16 | $0.16 |
| tokens_in | 66,215 | 43,273 | 43,273 |

---

## 关键发现

### 1. bug 修复验证通过

H2-v4-fixed 中，write_file 触发 hook → pytest 全过 → hook 正确返回 `{}` 静默，
没有产生 `[hook 注入]` 是**预期的正确行为**（不是 bug 依旧）。

### 2. 修复后行为与 noHook 完全一致

工具序列、轮次、cost、tokens 几乎相同。说明：
- bug 修复没有破坏现有行为
- 对于"第一次写入就全部正确"的任务，hook 静默不增加开销

### 3. H2-v4（损坏 hook）的"逐一 replace"行为仍未解释

H2-v4 时 hook 损坏（system_message 进不了 LLM），但 agent 用了 3 次 replace_in_file，
修复后 agent 却用了 1 次 write_file。这可能是：
- 单次实验的随机性（LLM 采样不确定）
- 损坏 hook 的某些副作用（如 hook stderr 打印到控制台影响了某些输出格式）

需要多次重跑才能区分偶然性 vs 系统性差异，不在本实验范围。

### 4. "hook 驱动自修复"假设：仍未在真实场景验证

三组实验都没有出现 `[hook 注入]` 驱动 agent 修改代码的情况：
- H2-v4：hook 注入路径损坏
- noHook：没有 hook
- fixed：第一次写入就全部正确，hook 无需注入

**结论**：工具链（hook 触发 → pytest → system_message 注入 → LLM 看到）现在完整可用。
但要真正测试"hook 驱动修复"假设，需要设计**第一次修复尝试大概率失败**的任务——
即 sonnet 无法通过静态分析一次性推断出所有正确改法的场景。

---

## 下一步建议

若要验证"hook 驱动自修复"：

**方向1（任务设计）**：给一个不完整/错误的实现思路（如 PROMPT 里提供错误算法方向），
让 agent 先按错误思路写，hook 注入失败，agent 改变策略。

**方向2（多轮任务）**：H1 实验（UserPromptSubmit TDD 注入）——hook 在任务开始就注入约束，
比 PostToolUse 更容易验证注入效果，且不依赖"第一次写错"。

**方向3**：直接进 H1/H3 实验，PostToolUse 留作"已验证可用的工具"。
