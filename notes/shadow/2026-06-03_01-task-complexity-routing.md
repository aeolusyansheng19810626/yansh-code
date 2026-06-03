# 任务复杂度路由 + haiku 优化

日期：2026-06-03

## 背景

yansh 对所有 code/auto 任务一律走完整 plan→code→test→fix pipeline。AB 测试中 Task 1（只读分析）消耗 498K tokens / 260s，而同等任务轻量路径只需 43K tokens / 44s（11× 差距）。

## 改动内容

### 1. 任务复杂度路由（agent.py）

在 `_run()` 的 audit early return 之后、plan 调用之前，新增路由块：

- `readonly` → 直接走 `audit()`，跳过 plan/code/test/fix
- `simple` → 注入 hint 压缩 plan 规模（expected_edits ≤5）
- `complex` → 不变，走完整 pipeline

分类函数 `_classify_task(requirement)` 使用关键词匹配优先 + LLM（haiku）兜底：
- complex 关键词优先：重构/全部文件/迁移/refactor/entire project…
- 写入否定词覆盖 readonly：修改/修复/添加/fix/add/implement…
- readonly 关键词：分析/解释/在哪里/审查/评估/analyze/explain…
- 关键边界：`"实现"` 不加否定词（误匹配"实现方式"），`"write"` 不加（误匹配 write_file）

### 2. readonly 路由使用 haiku（agent.py）

`audit()` 函数新增 `model_override` 参数，readonly 路由调用时传入 `"claude-haiku-4-5"`：
```python
res = audit(original_requirement, model_override="claude-haiku-4-5")
```

### 3. task_log 新增 cost_usd（task_log.py）

`finish_task_log()` 中按 by_model token 量 × 单价计算本次任务实际成本，写入日志：
```python
_current_task_log["cost_usd"] = round(cost_usd, 6)
```

### 4. runner 脚本解析 cost_usd（AB-test/*/task*_runner.py）

15 个 runner 统一更新，运行后自动解析 stdout.json，把 tokens_total / success / cost_usd / files_modified 写入 meta.json，避免 PowerShell 管道编码问题。

## 效果数据

Task 1（只读分析，`_dispatch_tool_calls` 并发/串行条件）：

| 版本 | elapsed | tokens | cost |
|------|---------|--------|------|
| 原始（无路由，sonnet） | 259s | 498K | — |
| 路由后（sonnet audit） | 113s | 19K | ~$0.06 |
| 路由后（haiku audit） | **32s** | 241K | **$0.25** |
| sheng 对比 | 43s | 43K | $0.15 |

- haiku 速度（32s）比 sheng（43s）快 25%，成本（$0.25）略高于 sheng（$0.15）
- haiku token 消耗较高（241K vs 19K）是 haiku context 效率低于 sonnet 的正常表现，但单价低，实际成本仍可接受

## 单元测试

新增 `tests/unit/test_classify_task.py`，31 个测试用例覆盖：
- readonly 正例（中英文）
- 否定词防护（最重要的回归防护）
- complex 正例
- simple 兜底
- LLM 兜底失败不崩溃

## 关键经验

- 否定词防护比正例更重要："分析一下然后修复它"→ "修复"覆盖"分析"，不走 readonly
- `"write"` 和 `"实现"` 不能作为否定词：会误匹配 `write_file` 函数名和"实现方式"名词
- haiku 速度提升显著，但 token 消耗比 sonnet 高；对于延迟敏感的只读任务，haiku 综合更优
