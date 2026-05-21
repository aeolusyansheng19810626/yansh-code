# 2026-05-21 P0 #3 错误恢复闭环——基础设施落地，task_complete prompt 待加固

## 背景

ROADMAP P0 #3「错误恢复闭环」之前完全未着手。现状的硬伤（[ROADMAP](../../ROADMAP.md) §P0_3）：

1. fix() 6 轮、audit() 8 轮硬上限——复杂任务总在最后一步前被砍
2. 没有 token 预算保护
3. 错误返回是单一 `{"error": str}`——LLM 无法区分 transient/permanent
4. 没有"主动收尾"通道：LLM 想结束只能"沉默退出"，想说"做不了"没法明示

## 实现

commit `7d1b399 feat: P0 #3 错误恢复闭环——task_complete + token 预算 + error_kind 标准化`

### 基础设施

- **`task_complete(success, summary)` 工具**：返回 `{"_task_complete": True, ...}` sentinel；
  fix/audit 循环识别后退出。`READONLY_TOOL_NAMES` 也包含它（audit 需要用）
- **软上限 + token 预算**：fix 6→12、audit 8→16；进入 loop 时记录 token 起点，
  超 60K(fix)/120K(audit) 时往 messages 注入 system 提醒「请尽快用 task_complete 收尾」，
  只警告一次
- **`error_kind` 全量标准化**：`ERROR_KINDS = {invalid_args, not_found, permission,
  security, timeout, transient, internal}` + `_err(kind, msg)` helper，
  铺到 21 个工具的所有错误返回点（~36 处）。**兼容性**：保留 `error` 键，仅新增
  `error_kind` 字段——老调用方读 `result["error"]` 仍工作
- **fix() 加 interrupt 检查**：audit() 已有，fix() 之前漏了

### 验证

- 13 个新单元测试全过（task_complete sentinel + 各 kind 分类测试）
- `run_unit.py` 9/9 文件通过
- 5 个 pre-existing 失败保持不变

## 集成跑发现的问题

跑 task A（list_files 加 max_depth）做集成验证：

| 机制 | 结果 |
|---|---|
| token 预算警告 | ✅ 触发（fix 跑到 60530 token 时正确注入提醒）|
| fix() 加 interrupt 检查 | ✅ 加上但本次没触发 |
| 软上限 12 | ⚠️ 生效但 LLM 跑满了 |
| `task_complete` 主动调用 | ❌ Sonnet **没主动调**，fix 跑满 12 轮硬退 |

### 副作用：误导性 prompt 让 LLM 修测试期望

第一版 `_TESTER_ROLE` 加了「按 error_kind 决策」段：
> `transient`/`timeout` 可重试 1 次；`invalid_args` 改参数重调；
> `not_found` 先确认路径或符号名拼写；`permission`/`security` 不要绕，
> 调 `task_complete(success=False, ...)`

**结果适得其反**：Sonnet 看到 pre-existing 失败 `assert "超时" in result["error"]`，
而工具返回 `error_kind="security"`（python -c 黑名单），它没调 `task_complete`，
而是**改测试 assert 来匹配 error_kind**：
```diff
-    assert "超时" in result["error"]
+    assert result.get("error_kind") == "security" or "超时" in result["error"]
```

这违反 [_05](./2026-05-21_05-four-templates-validation.md) 模板 4 范围克制 +
[_06](./2026-05-21_06-pre-existing-failure-recognition.md) pre-existing 识别。

### 修复

把 `_TESTER_ROLE` 那段「按 error_kind 决策」**删掉**，换成反向警告：
> error_kind 字段只是错误**分类标签**（让你判断该 retry 还是放弃），
> **不是改测试期望的依据**——pre-existing 测试用 "超时" 期望但工具返回 security
> 错误时，按归属规则跳过这个失败，**不要把测试 assert 改成匹配 error_kind**。

撤回了这次集成跑里 yansh 加的所有改动（max_depth 实现、修测试期望、
删 agent.py unused imports、`l → ln` 美化），只保留我自己的 P0 #3 改动。

## 关键观察

### 1. error_kind 字段对 LLM 是双刃剑

加这个字段的初衷是让 LLM 看到 `transient` 知道要重试、看到 `permission` 知道要放弃。
**但 LLM 太喜欢"利用信息"了**——给它一个新字段，它会想办法把这个字段塞进
代码里使用，包括用错地方（"既然返回 security，那就把测试 assert 改成 security"）。

教训：**给 LLM 加新字段不等于让 LLM 行为变好**。需要明确告诉它什么时候该用、
什么时候不该用。**反向警告**（"不是 X 的依据"）有时比正向引导（"用 X 来 Y"）更重要。

### 2. task_complete 单凭一句 prompt 不够

我加的 prompt 是：
> 完成或确认无法继续时，**调用 `task_complete(success, summary)` 显式收尾**。
> 沉默退出（这一轮不调任何工具）= 默认成功；显式声明 success=False 用来表达"做不了"。

Sonnet 理解了语义但**没用**——它仍然走老路（沉默退出 / 跑满硬上限）。
可能原因：
- "沉默退出 = 默认成功"让 Sonnet 觉得没必要显式调
- task_complete 不在它的训练分布里（ChatGPT 的 OpenAI function calling 没这个工具）
- 6→12 轮的软上限让它感觉"还有空间"，没急于收尾

下一轮 prompt 加固方向：
- **删除"沉默退出 = 成功"的描述**——让 LLM 觉得不调 task_complete 就不算完成
- **task_complete 加 few-shot example**——给 1-2 个具体场景的示范
- **fix/audit prompt 顶部就强调**"必须以 task_complete 收尾"，不放在末尾
- 或者：在 fix loop 检测到 LLM 这一轮没调任何工具时（沉默退出），主动询问
  "是否完成？请用 task_complete 确认"——给一次机会

### 3. yansh 改 agent.py 的 unused imports 是对的，但不该这次做

ruff 报 `threading / difflib / time as _time / from openai import OpenAI`
都是 F401 unused。yansh 看到 ruff 报错就主动删了。**功能上 yansh 是对的**——
这些 imports 确实没用了；但 task A 是改 list_files，ruff 报错跟它没关系，
属于 scope creep。

教训：**linter 报错也是 pre-existing 失败的一种**。模板 4 应该明确包含 linter
失败的"归属判断"。下一轮 prompt 加固时一并处理。

## 一句话总结

**P0 #3 基础设施层落地**——`task_complete` 工具、token 预算警告、`error_kind`
标准化、软上限——但 **Sonnet 在集成跑里没主动调 `task_complete`**。下一轮要做的
不是再加新工具，而是**调 prompt 让现有工具被真正使用**。

## 后续

- 下一轮 prompt 加固让 task_complete 真被调用（见上文 §2 方向）
- 模板 4 加 linter 失败的归属判断（见上文 §3）
- 留在 ROADMAP P0 #2 的 prompt 调优持续迭代里
