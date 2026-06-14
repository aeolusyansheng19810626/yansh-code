# Exp5 Skill 实验结果记录

## 实验信息

- 目录：`C:\Users\ShengYan\Projects\AB-test\skill-exp5-typing`
- 场景：实现 strutil.py（5 个函数），对比有/无 skill 的 agent 输出差异
- 模型：sonnet-4-6
- 实验日期：2026-06-13

---

## A1（首轮，类型标注约束）— 无效实验

**Skill 约束**：所有公开函数必须有完整类型标注（参数 + 返回值）

| 组 | 类型标注覆盖率 |
|----|--------------|
| 无 skill（baseline） | 5/5 (100%) |
| 有 skill | 5/5 (100%) |

**结论**：无可测量差异。sonnet-4.6 对简单类型函数（str/int/list）默认写完整类型标注，skill 约束的行为已在 baseline 里。

**教训**：skill 实验的约束必须选模型默认不做的行为，否则无法区分"skill 生效"和"模型默认行为"。

---

## A2（重设计，Google-style docstring 约束）— 有效实验

**Skill 约束**：所有公开函数必须有 Google-style docstring（含 `Args:` 和 `Returns:` 节）

| 组 | Google-style docstring 覆盖率 | pytest |
|----|------------------------------|--------|
| 无 skill（baseline） | **0/5 (0%)** | 30 passed |
| 有 skill | **5/5 (100%)** | 30 passed |

**结论：skill 注入有效，行为差异显著。**
- baseline：写了 docstring，但只有单行描述，无 `Args:`/`Returns:` 节
- 有 skill：5/5 全部写出完整 Google-style，含详细参数说明和返回值描述
- skill 触发日志：`命中 1 个：strict-typing`（触发词"实现"命中）

**机制确认**：
- skill 文件放在 `ws-with-skill/skills/strict-typing.md`
- yansh 以 `--cwd ws-with-skill` 启动，自动扫描 `<workspace>/skills/`
- 关键字匹配（"实现" in triggers）→ 注入 system prompt 尾部

---

## 关键发现

1. **skill 机制本身有效**：skill 内容确实被注入并改变了 agent 行为
2. **约束选择是实验设计的核心**：必须选模型默认不满足的约束，否则实验无区分度
3. **sonnet-4.6 默认行为边界**：
   - 默认写：类型标注（简单类型）、单行 docstring
   - 默认不写：Google-style `Args:`/`Returns:` 节

---

---

## Exp5-B：关键字 vs LLM 匹配准确率（2026-06-13）

**Skill**：strict-typing，触发词 `["实现", "函数", "模块", "implement", "strutil"]`

**关键机制发现（读代码得出）**：
`match_skills` 的决策顺序：关键字命中 → 立即短路返回，LLM 不调用；关键字未命中 → 才走 LLM。
因此 **LLM 只能补救漏报，无法拦截误报**——这是设计约束。

**13 条测试用例结果**：

| 分类 | 含义 | 关键字 | LLM |
|------|------|--------|-----|
| TP（2条） | 有触发词+应命中 | 2/2 ✅ | 2/2 ✅ |
| FP（4条） | 有触发词+不应命中 | 0/4 ❌ | 0/4 ❌（短路，LLM未调用）|
| FN（4条） | 无触发词+应命中 | 0/4 ❌ | **4/4 ✅**（LLM全部补救）|
| TN（3条） | 无触发词+不应命中 | 3/3 ✅ | 2/3 ✅（LLM引入1个新误报）|

**总准确率：关键字 38%，LLM 62%**

**三条结论**：
1. LLM 漏报补救率 4/4（100%）——「帮我写字符串库」这类无触发词输入全部命中
2. 关键字误报是当前设计盲区——触发词写宽（如「函数」「模块」）产生的误报 LLM 无法纠正（短路）
3. LLM 引入 1 个新误报（「写数学计算库」被误判相关），宁缺勿滥执行不彻底

**实验脚本**：`C:\Users\ShengYan\Projects\AB-test\skill-exp5-typing\run_exp5b.py`

---

## Exp5-C：多 skill 冲突实验（2026-06-13）

**两个互斥 skill**：
- `minimal-style`（先注入）：禁止写 docstring
- `strict-typing`（后注入）：必须写 Google-style docstring（Args/Returns）

**结果**：两个 skill 均触发（命中 2 个），agent 完全遵循 strict-typing，完全无视 minimal-style。
- Google-style docstring：5/5 (100%)
- docstring 存在率：5/5 (100%)

**结论**：
- Agent 忽略了注入顺序靠前的 minimal-style，选择了靠后的 strict-typing
- 根本原因：minimal-style 是**负向约束**（禁止），strict-typing 是**正向约束**（必须）——agent 对否定约束的执行不可靠，与 yansh 已知的 Pattern 14（否定约束识别困难）完全吻合
- 当前 skill 系统无仲裁机制，冲突时靠 agent 自行判断，且结果偏向正向指令

**三个实验汇总**：

| 实验 | 结论 |
|------|------|
| A（skill 注入有效性） | 有效，Google-style docstring 0%→100% |
| B（LLM vs 关键字匹配） | LLM 补救漏报 4/4，但无法纠正关键字误报（设计短路） |
| C（多 skill 冲突） | 正向约束胜出，负向约束被忽略，系统无仲裁机制 |
