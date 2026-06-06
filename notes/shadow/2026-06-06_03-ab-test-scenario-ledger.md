---
name: ab-test-scenario-ledger
description: yansh AB 测试 task1-60 全量场景台账 + 覆盖缺口分析（统一索引，避免翻代码）
metadata:
  type: project
---

# yansh AB 测试场景台账（task1-60）

**用途**：单一索引，记录每个 AB 测试任务测的是什么场景。新增任务前先查此表确认覆盖缺口，跑完后回填结果。
**维护**：新增 task 必须在此登记；场景定义权威源仍是各 `setup_*.py` 的 `TASKS` 字典，本表是导航。

## 任务来源映射

| task 范围 | 目标项目 | setup 脚本 / 笔记 |
|-----------|----------|-------------------|
| 1-10 | yansh-code（主场自测） | 早期 3way 笔记 + `2026-06-04_05-new-tasks-design.md` |
| 11-15 | strkit | `2026-06-04_08/09` |
| 16-20 | eventsys | `2026-06-04_10/11` |
| 21-25 | datakit | `2026-06-04_12/13` + `2026-06-05_01` |
| 26-30 | retrykit | `AB-test/yansh/setup_retrykit.py` |
| 31-35 | schemaval | `AB-test/yansh/setup_schemaval.py` |
| 36-40 | cachekit | `AB-test/yansh/setup_cachekit.py` |
| 41-45 | pipeline | `AB-test/yansh/setup_pipeline.py` |
| 46-50 | ratectl | `AB-test/yansh/setup_ratectl.py` |
| 51-60 | yscode | `AB-test/yscode/setup_yscode.py` |

## 全量台账

| # | 项目 | 场景类型 | 需求一句话 | RO | 最近结果 |
|---|------|----------|-----------|----|----------|
| 1 | yansh-code | 只读分析 | 分析 `_dispatch_tool_calls` 并发/串行触发条件 | ✅ | pass |
| 2 | yansh-code | 已实现检测(trick) | 加 `read_file` 的 `max_bytes`——但功能已存在，应识别 no-op | ❌ | pass(费 915K) |
| 3 | yansh-code | 只读分析+文档 | 评估 task_complete 改自然语言信号可行性，出 md | ✅ | pass |
| 4 | yansh-code | bug修复 | 修 `memory.find_memory` 缺 `_slugify` 调用 | ❌ | pass |
| 5 | yansh-code | 多文件重构(加参) | `_err` 加 `tool` 参数，适配全 repo ~65 处 | ❌ | pass |
| 6 | yansh-code | 新增功能 | 新增 `--mode summary` 子命令 + task_log | ❌ | pass |
| 7 | yansh-code | bug修复(无test引导) | 修 `search_in_files` 正则特殊字符崩溃 | ❌ | **fail**(P0b假阳性) |
| 8 | yansh-code | 重构/抽取新文件 | auto-compact ~80 行抽到 `compact.py` | ❌ | pass |
| 9 | yansh-code | 新增功能(跨4模块) | token budget 上限，main/llm_client/agent/task_log 协同 | ❌ | pass(含手修) |
| 10 | yansh-code | 新增功能(spec留白) | 无测试任务自检机制，输出 self_review | ❌ | pass |
| 11 | strkit | bug修复 | 修 `truncate()` emoji/CJK Unicode 截断 | ❌ | pass |
| 12 | strkit | 新增功能 | 新增 `wrap_text(text,width,indent)` | ❌ | pass |
| 13 | strkit | 重构/抽取 | 4 函数重复正则抽成 `_match()` | ❌ | pass |
| 14 | strkit | 新增功能(新文件) | 新建 `pipeline.py` 实现链式 `Pipeline` 类 | ❌ | pass |
| 15 | strkit | 重构(模糊spec) | 改进 `readability_score()` 加可读等级 | ❌ | pass(R3) |
| 16 | eventsys | 新增功能 | `EventEmitter` 加 `once()` | ❌ | pass |
| 17 | eventsys | bug修复 | 修 `emit()` 迭代器失效（handler 内 `off()`） | ❌ | pass |
| 18 | eventsys | 新增功能(扩展) | 加通配符事件匹配（fnmatch） | ❌ | pass |
| 19 | eventsys | 新增功能(跨文件) | Emitter+Bus 同步加 handler 优先级 | ❌ | pass |
| 20 | eventsys | 新增功能(模糊spec) | 加可观测性接口（触发次数/handler 数） | ❌ | pass |
| 21 | datakit | 性能优化 | `find_duplicates()` O(n²)→O(n) + timeit 测 | ❌ | pass(R3) |
| 22 | datakit | 多bug修复(3个) | CSV 解析器 3 个 bug 一起修 | ❌ | pass |
| 23 | datakit | 加参数(向后兼容) | `format_record()` 加 `unit` + 批量 `format_records()` | ❌ | pass |
| 24 | datakit | 新增防御逻辑 | `validate_*` 加类型/越界校验抛异常 | ❌ | pass |
| 25 | datakit | bug修复(大文件定位) | 300+ 行 `indexer.py` 修 off-by-one | ❌ | pass |
| 26 | retrykit | bug修复 | retry `exceptions` 参数不生效 | ❌ | pass |
| 27 | retrykit | 加参数 | retry 加 `on_retry` 回调 | ❌ | pass |
| 28 | retrykit | 加参数 | `CircuitBreaker.call()` 加 `fallback` | ❌ | pass |
| 29 | retrykit | bug修复 | HALF_OPEN 成功后 state 未恢复 CLOSED | ❌ | pass |
| 30 | retrykit | 纯测试(只加测) | 加 2 个 backoff 时序测，不改 retry.py | ❌ | pass(*假通过) |
| 31 | schemaval | bug修复 | `type=int` 未排除 bool 子类 | ❌ | pass |
| 32 | schemaval | 新增功能 | str 字段加 min/max_length 约束 | ❌ | pass |
| 33 | schemaval | bug修复 | required 字段值为 '' 时误报 missing | ❌ | pass |
| 34 | schemaval | 新增功能 | 加 `allowed_values` 枚举约束 | ❌ | pass |
| 35 | schemaval | 新增功能(递归) | 加嵌套 dict 递归验证，点号路径 | ❌ | pass |
| 36 | cachekit | bug修复 | LRU capacity=2 提前淘汰 | ❌ | pass |
| 37 | cachekit | 新增功能 | 加 `cache_info()` 统计 | ❌ | pass |
| 38 | cachekit | bug修复 | `get()` 命中未移到最近使用位 | ❌ | pass |
| 39 | cachekit | 新增功能(回调) | 加 `on_evict` 淘汰回调 | ❌ | pass |
| 40 | cachekit | 纯测试(只加测) | 加 2 个 LRU 边界测，不改 cache.py | ❌ | pass(*假通过) |
| 41 | pipeline | bug修复 | map/filter 原地改 `_steps` 污染原实例 | ❌ | pass |
| 42 | pipeline | 新增功能 | 加 `flatten()` 步骤 | ❌ | pass |
| 43 | pipeline | bug修复 | `reduce()` 空序列无 initial 静默返 None | ❌ | pass |
| 44 | pipeline | 新增功能 | 加 `sort(key,reverse)` 步骤 | ❌ | pass |
| 45 | pipeline | 新增功能 | 加 `count()` + `first(default)` 终止方法 | ❌ | pass |
| 46 | ratectl | bug修复 | `_refill()` 未更新 `_last_refill` 无限流 | ❌ | pass |
| 47 | ratectl | 新增功能 | 加 `wait(tokens,timeout)` 阻塞等待 | ❌ | pass |
| 48 | ratectl | bug修复(改抛异常) | `acquire()` tokens>capacity 永 False → 抛 ValueError | ❌ | pass |
| 49 | ratectl | 新增功能(装饰器) | 加 `__call__` 作限流装饰器 | ❌ | pass |
| 50 | ratectl | 纯测试(只加测) | 加 2 个限流行为测，不改 limiter.py | ❌ | pass(*假通过) |
| 51 | yscode | 只读分析 | 分析 `_dispatch_tool_calls` 并发/串行 | ✅ | pass |
| 52 | yscode | 加参数 | `search_in_files` 加 `case_sensitive` | ❌ | pass |
| 53 | yscode | 只读分析+文档 | 评估 audit 模式改 read-only code 变体可行性 | ✅ | pass |
| 54 | yscode | bug修复(inject) | 修 frontmatter quote 剥离遗漏 | ❌ | pass |
| 55 | yscode | 重构/抽取私有函数 | `search_in_files` 匹配逻辑抽 `_match_line()` | ❌ | pass |
| 56 | yscode | 新增功能(多文件) | `--mode summary` 子命令 + task_log API | ❌ | pass |
| 57 | yscode | bug修复 | `search_in_files` 正则 `re.error` 崩溃 | ❌ | pass |
| 58 | yscode | 重构(改返回型) | `frontmatter.parse()` 改 NamedTuple + 更新调用点 | ❌ | pass |
| 59 | yscode | 新增功能(跨模块) | `--budget` CLI 参数 + BudgetExceededError | ❌ | pass |
| 60 | yscode | 新增功能(自检) | 无测试任务自检，输出 self_review | ❌ | pass |

> `*假通过`：task30/40/50 yansh 在 ws 根目录新建同名文件，测试测的是副本而非包内实现——表面 pass，实际未触及目标。修法：prompt 明确目标文件为 `tests/unit/test_*.py`。

## 场景类型分布

| 场景类型 | 数量 | task |
|----------|------|------|
| bug修复 | 18 | 4,7,11,17,22,25,26,29,31,33,36,38,41,43,46,48,54,57 |
| 新增功能/加参数 | 26 | 6,9,10,12,14,16,18,19,20,23,27,28,32,34,35,37,39,42,44,45,47,49,52,56,59,60 |
| 重构(抽取/改型) | 6 | 5,8,13,15,55,58 |
| 只读分析(±文档) | 4 | 1,3,51,53 |
| 纯测试 | 3 | 30,40,50 |
| 性能优化 | 1 | 21 |
| 防御/校验 | 1 | 24 |
| 已实现检测(trick) | 1 | 2 |

## 覆盖缺口（尚未测的场景）

| 缺口 | 说明 | 优先级 |
|------|------|--------|
| **A. 失败路径鲁棒性** | 需求指向不存在的文件/函数、或需求自相矛盾不可行——应正确 `success=False` 放弃而非幻觉硬写。当前 success 判定只看"不引入新失败"，从未测"该失败时是否失败"。task2 的 no-op 检测最接近但不等价 | 高 |
| **B. 删除/清理** | 删死代码 + 其调用点。与全部现有"新增/修改"相反，agent 常不敢删 | 高 |
| **C. 正向全局重命名** | 跨多文件 rename 一个公开符号。task56 反而是*禁止*重命名，无正向 rename 测试 | 中 |
| **D. 大改动/深调用链** | 当前最深 task5(65处)/task9(4模块)。改被 5+ 处引用的核心函数签名，考验 explorer 找全引用 | 中(部分覆盖) |
| **E. 非 .py 文件** | 改 `pyproject.toml`/配置/README，纯 .py 之外的编辑 | 中 |
| **F. 测试本身有 bug** | 现有测试断言错误，应改测试而非改源码，考验"谁错了"判断 | 中 |
| **G. 并发安全修复** | task51 只*分析*并发，无*修*竞态/加锁的任务 | 低 |
