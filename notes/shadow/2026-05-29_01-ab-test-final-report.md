# AB 测试最终报告（2026-05-28~29）

**范围**：yansh-code vs yscode，task1-5，共 7 轮迭代（v3~v7 + yscode Z-15~Z-29）

---

## 最终数据（最优版本）

| task | 类型 | yansh | yscode | cc（参考） |
|---|---|---|---|---|
| task1 探索 | 只读 | 394K/120s ✓ | 15K/17s ✓ | - |
| task2 trick | 已实现检测 | 372K/154s ⚠️重复实现 | 30K/21s ✓ | - |
| task3 文档生成 | 只读+写doc | **303K/184s ✓** | 188K/133s ✓ | - |
| task4 bug修复 | 写代码 | **196K/59s ✓** | 285K/136s ✓ | - |
| task5 大批量 | 65处改动 | **981K/769s ✓** | 1307K/320s ✓ | ~未知/180s ✓ |

yansh 版本：`0468033`（compact thrashing 修复）
yscode 版本：Z-28（streaming fallback）

---

## 成果

### 1. task5 从持续失败到稳定通过

yansh task5 经历 5 个版本才通过：

| 版本 | tokens | 时间 | 结果 | 根因 |
|---|---|---|---|---|
| v3 | 1402K | 574s | ❌ | test patch 绑定错位 + baseline 超时漏记 |
| v4 | 2519K | 1620s | ❌ | write_file 模式 22 轮重写副作用 |
| v5 | 2320K | 429s | ❌ | 同上（部分改善） |
| v6 | - | 351s | ❌ | compact thrashing（阈值 30K 太低） |
| **v7** | **981K** | **769s** | **✓** | thrashing 修复 |

最终 token 从峰值 2519K 降到 981K（**-61%**）。

### 2. yansh 新增 3 个关键机制

| commit | 内容 | 解决的问题 |
|---|---|---|
| `e608b1b` | baseline 超时 30s→120s + 超时保留已采集输出 | baseline 漏记预存失败 → coder 反复修无关测试 |
| `53b7b37` | test_subagent.py patch 绑定修复 | 15 个测试假失败（patch agent.call_llm 无效） |
| `0468033` | compact 阈值 60K + 压缩率判定 + _compact_disabled | thrashing 死循环 → 任务强制终止 |

### 3. yscode 关键优化路径（Z-15 → Z-28）

| 版本 | task5 tokens | 关键改动 |
|---|---|---|
| v3（Z-15） | 2663K ❌ | explorer cap=3 + 文档任务检测 |
| Z-23 | 2703K ✓ | attempts 用完但无失败 → 视为成功 |
| Z-24 | 2264K ✓ | baseline 非空提前 break |
| **Z-28** | **1307K ✓** | streaming tool_use parse error → fallback stream=False |

---

## 经验

### E1：任务类型决定胜者，没有万能架构

| 任务类型 | yansh 优势 | yscode 优势 |
|---|---|---|
| 只读探索 | — | 直接回答不触发 coder loop（15K vs 394K）|
| 已实现检测 | — | architect 识别已存在不重复实现（30K vs 372K）|
| 文档生成 | 直接写 doc 无多余探索 | — |
| 代码修复/批量改动 | token 更省（plan/coder 解耦精准）| 并发更快（subagent 并行）|

两个系统各有擅长领域，不存在全面压制。

### E2：AB 测试环境质量直接影响结论可信度

本轮多次结论被环境问题污染：
- **workspace 污染**：yscode task5 因上次运行残留，coder 检测到"已完成"32s 退出（假 pass）
- **bug inject SKIP**：workspace 未清理，注入步骤找不到 pattern 静默跳过
- **baseline 超时**：30s 超时截断 pytest 输出 → 漏记 15 个预存失败 → coder 反复修无关测试

**教训**：每次跑前必须 `git checkout -- . && git clean -fd`，不能依赖 runner 内部 reset。

### E3：compact 阈值不是越低越好

yansh 经历了：80K（从不触发）→ 30K（thrashing）→ 60K（稳定）。

规律：阈值必须高于"compact 后的最小消息体积"（system prompt + 摘要 + 最近 N 轮工具调用）。task5 这类多文件批量任务的底线体积约 42K，30K 阈值触发后立即超阈值，产生死循环。

**正确做法**：参考 cc 的 rapid-refill 思路——用"压缩率 <15% 才计 thrashing"而不是"compact 后还超阈值就计"。

### E4：bedrock streaming 有 bug，大 tool call 会被截断

yscode 在 task5 coder 阶段遇到 bedrock 截断 `replace_in_file` 的 arguments JSON：
```
arguments: '{"path": "tools.py"'  ← 第 19 字符截断
```

这不是 context 太大的问题（当时 history 才 25K），是 bedrock streaming 模式下大 tool call output 的 bug。**修法**：parse 失败时 fallback 到 stream=False 重试（Z-28）。

### E5：测试 patch 目标要和实现绑定匹配

yansh test_subagent.py 15 个测试长期假失败，根因：`subagent.py` 用 `from llm_client import call_llm` lazy import，测试 patch `agent.call_llm` 不影响 subagent 的局部绑定。

**规律**：patch 目标必须是函数实际执行时查找的名字空间，不是调用方的引用。用 `monkeypatch.setattr(module, "name", fake)` 而非直接赋值。

---

## 教训

### L1：优化方向正确，实现可能有副作用

yansh Fix C（允许 write_file 处理 20+ 处改动）方向正确，但实现后 task5 token 从 1402K 暴涨到 2519K。根因：write_file 模式触发后，LLM 仍在 22 轮内循环重写（写→pytest 失败→再写），每轮 1500 行文件 read+write，比 22 轮 replace_in_file 更贵。

**教训**：优化完要立即跑回归，不能假设"方向对 = 结果对"。

### L2：thrashing 保护不能只 raise，要让任务继续

原来的 thrashing 保护（连续 4 次超阈值 → raise RuntimeError）在 task5 上直接终止任务。参考 cc 的设计：遇到 thrashing 时停止自动 compact 但让任务继续执行，不能以"保护"为由终止用户任务。

### L3：数据说话，直觉分析可能出错

本轮两次诊断错误：
1. 以为 yscode token 高是因为 subagent 把完整输出塞回主 context → 实测 subagent 已有 1000 字符 hard cap，主因是 coder 39 轮 × 50K/轮
2. 以为 compact 阈值 60K→30K 能降低 bedrock 截断概率 → 实测 history 才 25K，compact 阈值改动是 no-op

**教训**：先看 log 数据，再下结论；不要用"直觉合理"代替实测。

### L4：ICA 的限制要写进 AB 测试基准

本轮多次结论受 ICA 环境影响：
- prompt cache 未透传（yansh/yscode 无缓存，cc 有 → 时间差异有 ICA 因素）
- bedrock 截断 tool call（yscode 特有问题，直连 Anthropic 不会出现）
- opus-4.8 ICA 未配置（模型版本不一致）

AB 测试结论应标注"基于 ICA 环境"，不能直接对比 cc 绝对数字。

---

## 最终结论

### token 效率

- **小任务（只读/已实现检测）**：yscode 胜出，10-20x 差距
- **中任务（文档生成/bug 修复）**：接近，yscode task3 已低于 yansh（188K vs 303K）
- **大任务（批量改动）**：yansh 更省 token（981K vs 1307K，-25%）

### 速度

- **所有任务**：yscode 更快（subagent 并发），task5 快 2.4x（320s vs 769s）
- cc 参考值（~180s）主要受益于 prompt cache，ICA 环境不可比

### 可靠性

- task3/4 两边都稳定
- task5 yansh v7 稳定通过；yscode Z-28 稳定通过
- 两边都有已知限制：yansh task1/2 plan pre-flight 缺失，yscode streaming 依赖 fallback

---

*数据文件：`C:\Users\ShengYan\Projects\AB-test\SUMMARY_v3.md` ~ `SUMMARY_v7.md`*
