# Token 削减改造 + fix loop 退化定位 + prompt 反例修法

承接前序 AB 测试三轮（task #1/#2/#3，commit `78b2f5d` / `137b647` / `52971b5`）。本日做了完整一次"诊断 → 改造 → 验证 → 翻车定位 → 二次修复 → 再验证"闭环。

最终落到 commits：`6d99a70` (P1.x) → `a6fad9c` (P2.x) → `cce571a`（翻车定位+反例修法 v1）→ `174df32`（去掉 notes/shadow 硬依赖）→ `b1d890f`（v2 验证 + 修 plan 解析 bug）。

## 1. 诊断起点

baseline AB 三轮显示 yansh vs CC 子 agent：

| Task | yansh tokens | CC tokens | yansh/CC |
|---|---|---|---|
| #1 (探索) | (small) | (small) | ≈ |
| #2 (写代码 + fix loop) | 641K | 25K | **~25×** |
| #3 (架构论证) | 730K | 169K | **~4×** |

调研 + grep 后定位 6 个削减点，分两期实施。详细方案见 [`../shadow/ab/20260523_token_reduction_compare.md`](./ab/20260523_token_reduction_compare.md)。

## 2. Phase 1（高 ROI 低风险）

### P1.0 ICA gateway cache_control 透传探测

写 `scripts/probe_ica_cache.py`（一次性脚本）发了两条相同 2022-token 请求测 `cache_creation_input_tokens` / `cache_read_input_tokens`。结果：**ICA 不透传**（cache_control 字段被默默忽略，不报错也不生效）。**P1.1 Prompt Cache 直接跳过**——不是说 cache 不值得做，而是 ICA 不支持这条路径就走不通。

### P1.2 System prompt 英文化

把 `agent.py` 里所有 role prompt（`_CODER_ROLE` / `_TESTER_ROLE` / `_AUDITOR_ROLE` / `_PLANNER_ROLE` / 各 plan/code/audit/fix 的 sys_prompt 构造点）+ `subagent.py:_SUBAGENT_ROLE` 全改英文，末尾固定一句 `Always respond in Chinese (用户的项目规则要求中文回复)`。

**保留中文不动**：CLAUDE.md（用户写的，不该单方面翻译）、user message、工具 schema description（顺手改了部分但不彻底）、role 末尾少量中文规则。

中文 BPE token 比英文同等语义多 1.5-2×。期望每轮 input 省 2-5K，长任务累计可观。

### P1.3 Fix loop 测试 scope

`linter._detect_python_test_cmd(ws, scope=None)` 加 `scope: list[str]` 参数：scope 命中时返回 `pytest tests/unit/test_X.py`（特指文件），否则原行为（全套）。

`agent._infer_test_scope(plan_files)` 推断："对每个改动的非 test 源文件，找同名 `test_<basename>.py`；改的就是 test 文件本身则直接进 scope"。

`_apply_test_scope_override(plan_result)` 在 `code()` / `audit()` 接到 plan 后立即重写 `plan_result["test_command"]`——只对 LLM 给出 `pytest`/`pytest -v` 这类全套命令生效，对 `make test` / `tox` 这类 wrapper 不动（尊重 LLM 显式选择）。

加了 16 个新单测覆盖 scope 注入边界（uv lock / Makefile / tox 包装器跳过 / 多文件 join / 空 plan）。

## 3. Phase 2（中 ROI 中复杂度）

### P2.1 read_file 命中检测（thread-local）

`agent._read_cache_state = threading.local()`，`init_task_log` 时清空。`_dispatch_tool_call_inner` 在 readonly_handlers 分支前检测：`name == "read_file"` 且 `_read_cache_hit_or_record(args)` 命中 → 直接返回 stub `{"hit": "X.py L1-100 (cached)"}`，不让原 read_file 真的去读盘 + append 完整 content 到 messages。

第一次写成模块级 `_READ_CACHE: set + Lock`，被 `test_dispatch_tool_calls_subagent_exception_isolated` 抓出回归——并发子 agent 共享 cache 互相穿透。改成 `threading.local()` 后每线程独立。

新建 `tests/unit/test_read_cache.py`（8 个单测）：thread-local 隔离、dispatch 集成、miss/hit 逻辑。

**cache key 一开始漏了 `max_bytes`**——这个 bug 是后来在 task #2 v2 yansh 自己跑的时候发现并修掉的（`b1d890f`）。详见 §6。

### P2.2 Subagent 切 haiku

`subagent._SUBAGENT_HAIKU_MODEL = "claude-haiku-4-5"`（**注意 ICA 格式没 -YYYYMMDD 后缀**——直连 Anthropic 的 `claude-haiku-4-5-20251001` 在 ICA 上 401，team_model_access_denied）。

`_subagent_model_for_role(role)` 路由：`explorer/auditor` → haiku；`general` → None（写代码场景需要 sonnet/opus）。

`llm_client.call_llm` 加 `model_override` 参数：非 None 时只跑该模型不走 `QUALITY_CASCADE`，失败抛错带 `override=X` 标记便于排查。

`_run_subagent` 调 `call_llm(..., model_override=_subagent_model_for_role(role))`。

附带顺手修了 `tests/unit/test_subagent.py` 两个 pre-existing patch bug（patch `agent.call_llm` 不生效，因为 `subagent.py:_run_subagent` 里 `from llm_client import call_llm` 是 lazy import；改成 `monkeypatch.setattr(_lc, "call_llm", ...)`）。这 bug 在 P1.2 之前能"过"是因为真 LLM 输出恰好匹配，英文化后 LLM 输出变了把 bug 暴露。

P1+P2 全包测试结果：21 failed = baseline pre-existing 完全不动，422 → +13 新单测全过。**0 新回归**。

## 4. Rerun task #2/#3 — 验证削减效果

**task #3（架构论证 + subagent）大成功** ✓
- sonnet 用量 716K → 53K（-93%）
- 总 tokens 730K（不变），但 haiku 658K + sonnet 53K → 估算成本 ~$0.82 vs baseline ~$2.15（**-62% 成本**）
- 证明 P2.2 是真本钱

**task #2（写代码 + fix loop）翻车** ❌
- 总 tokens 641K → 1722K（**+169%**）
- duration 254s → 402s
- test_result pass → fail（跑满 3 attempts max）
- 工具调用 61 → 75

P1.3 测试 scope 工作正常（`pytest tests/unit/test_tools.py` 命中相关测试），P2.2 子 agent 切 haiku 也工作，但**主流程 fix loop 没像 baseline 那样早退**。

## 5. 翻车直接原因定位

逐行对比 baseline 和 rerun stderr：

**baseline (commit 137b647 之前)**：
- attempt 1: linter 触发 fix loop, LLM 改了 5 处 lint（变量名 `l` → `line`），跑测仍 5 fail
- attempt 2: LLM **读了 `notes/shadow/2026-05-21_06-pre-existing-failure-recognition.md`** → 识别 5 条 pre-existing → `task_complete(success=true, summary="5 个失败全部是 pre-existing...")` 早退

**rerun (P1.x + P2.x 之后)**：
- attempt 1: 同 baseline 改 lint（变量名 rename）
- attempt 2: LLM **没读那条笔记**（`grep notes/shadow/2026-05-21_06` 在 tool_calls 里 0 命中），改成**弱化测试断言**绕过：
  ```diff
  - assert "超出" in result["error"]
  + assert "越界" in result["error"] or "超出" in result["error"] or "workspace" in result["error"].lower()
  ```
- attempt 3: 还在改断言，2 fail 残留 → 跑满 max attempts 退出

**根因**：P1.2 英文化后，原本中文 prompt 引导 LLM"查 notes/shadow/ 找 pre-existing 记录"的隐性 heuristic 失效。`_TESTER_ROLE` 里其实有"do not edit the test assert to match error_kind"的明文规则但 LLM 没遵守。

## 6. Prompt 反例修法（两版）

### v1（cce571a）— 错的修法

我第一版把"先 grep notes/shadow/ 找 pre-existing 记录"写进 fix() user message + `_TESTER_ROLE` Example 3 反例。**用户立刻指出问题**：yansh 是通用工具，跑在任何项目上都不该依赖 yansh-self-codebase 偶然存在的 `notes/shadow/` 目录。

### v2（174df32）— 正确修法

去掉 notes/shadow 硬依赖。改成：
1. fix() user message 里把 `plan_files` 列表显式列出（替换原来传整个 `json.dumps(plan)`），明示 LLM "归属判断走 `_TESTER_ROLE` Investigation order 第 1 条 — 失败符号是否在 Plan files 范围"
2. `_TESTER_ROLE` Example 3 反例：列三种典型 anti-pattern（加 `or` 子句 / 改字面量 / 删 assert），结尾说"正确做法是按归属规则跳过"，不再提 notes/shadow

**反例 few-shot 比正例 few-shot 更重要**——LLM 看到"❌ 这是错的"比看到"✓ 这是对的"更容易避免。这是这次的关键 lesson。

### v2 验证（b1d890f）

回退 tools.py / tools_schema.py / test_tools.py 到 max_bytes 之前的状态再跑：

| 维度 | baseline | v1 翻车 | **v2 修后** |
|---|---|---|---|
| duration | 254s | 402s | **219s** ✓ |
| 工具调用 | 61 | 75 | **28** ✓ |
| 总 tokens | 641K | 1722K | **754K** |
| sonnet input | 627K | 1043K | **747K** |
| 估算成本 | ~$1.88 | ~$3.79 | ~$2.24 |
| test_result | pass | fail | **pass** ✓ |
| 弱化断言? | 无 | ⚠ 5 处 | **无** ✓ |

linter attempt 1 早退（"218 条 ruff 错误识别为不在 plan files 范围"），test attempt 2 早退（"5 条 pre-existing 不在范围"）——两阶段都没尝试改测试。

附带 v2 yansh **顺手修了 P2.1 的真 bug**：`_read_cache_key` 没把 `max_bytes` 当 key，会让不同 max_bytes 的 read_file 调用错误命中 cache 返回不正确的截断状态。这是 yansh 自己跑过程中 LLM 发现的——质量上比 baseline 还好。

### v2 我自己埋的 bug

`fix()` 的 `plan` 参数实际是 `plan_result` 字典（含 `"files"` key），但 `cce571a` 我写的 `plan_files = [p.get("filename", "") for p in (plan or [])]` 按 list 迭代——结果迭代字典键（字符串），`isinstance(p, dict)` 全 False，`plan_files` 永远是空 `[]`。

LLM 看到"plan files 为空"就理解成"一切都不在范围"——**碰巧让早退发生了**，但这是错的逻辑碰对了行为。要是 plan 真有相关失败，会误判为 pre-existing 跳过。

`b1d890f` 修：
```python
plan_items = plan.get("files", []) if isinstance(plan, dict) else (plan or [])
plan_files = [p.get("filename", "") for p in plan_items if isinstance(p, dict)]
```

兼容 dict 形态（实际 caller）和 list 形态（测试和未来 caller）。

## 7. 三个 task 综合 + lesson

| Task | baseline tokens | v2 tokens | v2 cost vs baseline |
|---|---|---|---|
| #2 (写代码 + fix loop) | 641K | 754K | +19% |
| #3 (架构论证 + subagent) | 730K | 729K | **-62%** ✓ |

**lessons**：

1. **架构层削减比 prompt 调优更可靠**：P2.2 子 agent 切 haiku 在 #3 类任务收益巨大且稳定；P1.2 英文化在小任务 token 节省难度量、还可能引入行为退化。
2. **反例 few-shot > 正例 few-shot**：`_TESTER_ROLE` 原本就有"don't edit assert to match error_kind"明文规则，LLM 不遵守。Example 3 列出三个具体 anti-pattern 之后立刻见效。
3. **prompt 别依赖项目偶然产物**：notes/shadow/ 路径是 yansh-self-codebase 才有。通用工具的 prompt 必须自洽——这次的归属规则只用 plan_files vs 失败符号即可定性，完全本地化。
4. **prompt 改了一定要 rerun 真任务验证**：单测过不代表 LLM 行为对。task #2 v1 单测 18 failed = baseline 21 - 3（被 LLM "弱化"过的 3 条）—— 单测看上去"更绿"了反而是 bug 标志。
5. **AB 测试笔记的价值**：baseline LLM 是因为读了 `notes/shadow/2026-05-21_06` 才早退的——没这条笔记积累，行为就会和 v1 翻车一样。"yansh 之前为什么能识别 pre-existing"这个隐性 heuristic 暴露后才知道得显式化进 prompt。

## 8. 待办（下次 session 取）

- 端到端再跑 task #1（探索）看 P2.2 在 explorer 子 agent 场景的收益
- P3.1 历史压缩按需做（评估：长任务中 #2 类型 754K 主要在 sonnet 单轮 input 里——压缩老 read_file 结果可能继续砍 30-40%）
- 考虑给 `_PLANNER_ROLE` / `_AUDITOR_ROLE` 也加反例 few-shot，覆盖更多 anti-pattern
