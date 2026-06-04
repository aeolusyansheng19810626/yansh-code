# task7/9 误判分析：P0b 假阳性 + readonly 误分类

日期：2026-06-04  
发现来源：AB test task6-10（yansh vs CC 对比）

## 现象

| task | 类型 | 误判表现 | 实际结果 |
|------|------|---------|---------|
| task7 | bug fix（无 test 引导） | P0b 假阳性：found re.search → 早退 | files_modified=[]，bug 未修 |
| task9 | 跨文件新功能 | readonly 误分类 → audit 路径 | files_modified=[]，功能未实现 |

两者都显示 `success=true`，掩盖了 `files_modified=[]` 的异常。

---

## task7：P0b 假阳性（agent.py:2438-2453 + 2310-2311）

### 根因

P0b 的注入逻辑：`search_in_files 命中（total >= 1）→ 注入"已完成，立即 task_complete"引导`

这个逻辑对 **fix/修复类任务天然倒置**：
- 搜索 `re.search` 命中 → 证明 **bug 仍在**（待修的旧代码本身）
- P0b 解读为 → **改动已实现**，早退
- Coder 顺从，task_complete(success=True)，files_modified=[]

系统提示词 agent.py:2311 同样有问题：
> "Found (total >= 1): the change is ALREADY applied. Call task_complete IMMEDIATELY."

此规则假设"搜索的是新引入的标识符"，对修复已有代码的场景完全错误。

### 触发路径

1. 分类 → simple（正确）
2. simple-fast → 目标文件 tools.py（正确）
3. Coder pre-flight 搜索 `re.search` → 命中（bug 代码本身）
4. P0b 注入"已完成"引导（出现 2 次）
5. Coder task_complete(success=True)，未改任何文件

### 修法

**方案 A**：改 P0b 文案，从"命令早退"降级为"中性判断"，要求 Coder 自己区分"命中的是新代码还是待修旧代码"。

**方案 B**：fix 类任务（含"修复/修改/崩溃/bug/fix/crash"等词）彻底禁用 P0b 注入。

组合 A+B 最稳妥。

---

## task9：readonly 误分类（agent.py:273-285 + 355-364）

### 根因

双重失效：
1. **关键词表覆盖不足**：单字"加"、`raise`、`--`、`字段`、`单测`、`功能`、`捕获` 均不在 `_WRITE_NEGATION_KEYWORDS` 表中 → `has_write=False`
2. **守卫逻辑失效**：`agent.py:364` 的 `result=="readonly" and not has_write` 守卫，在 `has_write` 已误判为 False 时形同虚设
3. **haiku LLM 误判**：最终 haiku 把"给 yansh 加 token budget 功能（含编号步骤 1-5）"判为 readonly

### 触发路径

1. complex 关键词：无命中
2. 写入否定词：单字"加"/`raise`/`--`/`字段`/`单测` 全部不在表中 → `has_write=False`
3. readonly 关键词：无命中
4. 调 haiku LLM → 返回 readonly（误判）
5. 守卫 `not has_write=True` → 放行 readonly
6. → audit 路径，永不修改文件

### 修法

1. **补全写入词表**（必做）：加入 `功能`、`字段`、`单测`、`捕获`、`raise`、`--`、`CLI`、`feature`、`unit test` 等
2. **加 `_HARD_WRITE_SIGNALS`**（关键）：在 LLM 兜底之前，命中任一强写入信号直接设 `has_write=True`，否决 readonly
3. **补充 LLM prompt 规则**：含编号步骤 + "加 X 字段" + "加 N 个单测" + "CLI 加参数" → 一定不是 readonly

---

## 共性问题：success=true 掩盖 files_modified=[]

两个 task 都因为"没有新增失败"而报 success=true，但实际上任务根本没执行。

**建议兜底校验**：修改类任务（non-readonly）结束时，若 `files_modified=[]` 且 summary 未显式声明"无需改动"，应将 success 降级为 false 或触发复核。

---

## 经验教训

1. **P0b 的"命中即完成"启发式过于激进**：search 命中无法区分新旧代码，fix 类任务必然误判
2. **关键词表设计要覆盖"动词+名词"双维度**：单字动词（"加"）容易漏，领域名词（"字段/单测/CLI"）更稳
3. **守卫逻辑不能依赖自身可能误判的字段**：`has_write=False` 既是根因又是守卫条件，形成死锁
4. **新增 AB task 是发现 yansh 回归的有效手段**：task6/8/10 通过，task7/9 暴露了 P0b 和分类器的真实缺陷
