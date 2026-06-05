# task9 token budget 修复：测试+实现双修

日期：2026-06-04  
前置：./2026-06-04_06-task7-task9-misclassification.md（task9 分类修复）

## 背景

task9 要求给 yansh 加 token budget 上限功能（5 步：CLI 参数/超限异常/agent捕获/JSON字段/单测）。
分类误判修复后，yansh 能正确尝试任务，但 tests 仍 fail（success=false）。
opus-4.8 分析 agent.log 后确认是**测试写错 + 实现不完整**的双重问题。

---

## 根因（opus-4.8 分析 log 得出）

### 问题1：测试 mock 目标错

`call_llm` 内部取 client 路径：`cl = _client_for(model)`（line 248）
- Claude 模型 → `_get_ica_client()` 返回 `_ica_client`
- 测试 patch 了 `llm_client._client` → 这条路径根本不走 → mock 无效 → 真实请求打出去

### 问题2：测试假设 Agent 类不存在

`agent_module.Agent(task=..., task_log=...)._run()` → `AttributeError`  
yansh 实际入口是模块级函数 `run()` / `_run(requirement, mode)`

### 问题3：agent.py 实现不完整

`_run()` 没有 `except BudgetExceededError` 捕获块，需求第3、4点未实现。
Coder 在 agent.py 上轮次耗尽（6 文件每个 12 轮上限）后只改了 import 就停了。

### 问题4：BudgetExceededError 单位口径混乱

内部 `_budget_limit_k` 单位 K，但测试断言 `err.limit == 1000`（期望裸 token 数）→ 失败。

### 附：yansh pipeline 失效链路

- **Coder**：写 test_budget.py 前未 read agent.py 确认真实 API，凭 architect 计划臆造 `Agent` 类盲写
- **Fixer**：3 轮全程"迁就测试"（加 `_client` 别名、加 `reset_session_tokens` 别名），而非修测试
- **Fixer 两次被 token 上限截断**（fix delta > 60K / 12 轮上限），lint 错误（70 个 F401）耗尽前两轮预算

---

## 修法（opus plan → sonnet 实现 → opus review）

### llm_client.py

超限检查改用裸 token 数，异常 `used/limit` 口径统一：

```python
# before
total_k = get_session_total_tokens() // 1000
if total_k >= _budget_limit_k:
    raise BudgetExceededError(total_k, _budget_limit_k)

# after
used_tokens = get_session_total_tokens()
limit_tokens = _budget_limit_k * 1000
if used_tokens >= limit_tokens:
    raise BudgetExceededError(used_tokens, limit_tokens)
```

### task_log.py

补顶层 `success` 和 `summary` 字段（测试/JSON 直接读）：

```python
_current_task_log["success"] = bool(success)          # 顶层布尔
_current_task_log["summary"] = _summary_text           # task_complete_signal.summary 镜像
```

### agent.py

新增 `_BUDGET_EXCEEDED: bool = False`；  
`_run` 拆为 thin wrapper + `_run_impl`（原函数体完全不动）：

```python
def _run(requirement, mode):
    global _BUDGET_EXCEEDED
    _BUDGET_EXCEEDED = False
    try:
        return _run_impl(requirement, mode)
    except BudgetExceededError as e:
        _BUDGET_EXCEEDED = True
        summary = f"token 预算超限中断：已用 {e.used} / 上限 {e.limit} tokens"
        finish_task_log(False, 0, {"returncode": -1, "stdout": "", "stderr": summary},
                        task_complete_signal={"early_exit": True, "success": False, "summary": summary})
        return {"success": False, "budget_exceeded": True, ...}
```

### tests/unit/test_budget.py

| 问题 | 修法 |
|------|------|
| mock `_client` → 无效 | `patch("llm_client._client_for", return_value=mock_client)` |
| `call_llm` 走流式路径 MagicMock 不可迭代 | 显式传 `stream=False` |
| `resp.model` 是 MagicMock 变成 dict key → JSON 报错 | `_fake_response` 加 `resp.model = "test-model"` |
| `Agent` 类不存在 | 改为 `monkeypatch.setattr(agent_module, "call_llm", ...)` + `agent_module._run(req, "code")` |
| task_log 路径污染 | `monkeypatch.setattr(task_log_module, "_LOG_DIR", log_dir)` 隔离 |
| patch 绑定副本问题 | patch `agent.call_llm`（不是 `llm_client.call_llm`） |

---

## 测试结果

```
tests/unit/test_budget.py::test_no_budget_no_raise  PASSED
tests/unit/test_budget.py::test_budget_exceeded_raises  PASSED
tests/unit/test_budget.py::test_agent_task_log_on_budget_exceeded  PASSED
3 passed in 0.99s
```

全量：555 passed, 11 failed（11 个为预存失败，相对 HEAD git diff 为空）

---

## opus review 结论：Approved with minor suggestions

**无 Major 问题**。验证了三个风险点均不成立：
1. `_run_impl` 内部无 except 吞掉 BudgetExceededError（code() 是裸调用）
2. `init_task_log` 在任何 LLM 调用前就已执行，finish_task_log 空日志窗口不存在
3. test3 的 requirement "<20字+含文件名" 走 simple-fast，不触发 classify LLM

**Minor**：test3 的 requirement 有隐含约束（<20字且含单文件名），加注释说明。已应用。

---

## 经验教训

1. **Coder 写测试前应 read 被测模块**：确认真实符号名/API，不能凭 architect 计划里的假设盲写
2. **Fixer 策略：修代码还是修测试？** 当测试假设的 API 不存在时，Fixer 要"修测试"而非"让代码迁就测试"，但 yansh 当前 Fixer 默认倾向于改实现
3. **mock 目标的正确位置**：`from X import f` 后应 patch `module.f`（副本），不是 `X.f`；`_client_for` 是唯一可靠拦截点
4. **token 单位混用是真实 bug**：内部 K、外部裸 token 没有显式转换，测试就会失败
5. **Fixer 被 lint 错耗尽预算**：agent.py 改了 import 引入 70 个 F401，占满了 fix 的前两轮，导致 budget 功能本身没修完
