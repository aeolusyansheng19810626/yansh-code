# Code Review 能力对比报告

**日期**：2026-05-30  
**对象**：yansh vs yscode 生成的 agent-skills 项目  
**评审者**：Claude Sonnet 4.6（本体）、yansh、yscode  

---

## 一、任务结果

| 评审者 | 能否完成 review | 耗时 | 产出 |
|---|---|---|---|
| **Claude（本体）** | ✅ 完成 | — | 完整 6 维度报告，含代码引用、严重度分级 |
| **yansh** | ❌ 失败 | 57.2s | 占位文件（"正在读取源文件，稍后更新..."） |
| **yscode** | ❌ 失败 | 67.1s | 无文件（exit=1） |

---

## 二、为何 yansh/yscode 无法完成 review

两者均为 **代码生成工具**，设计流程为 `plan → code → test`：

- **yansh**：coder 阶段创建了占位文件（说明 plan 阶段理解了任务），但 tester 阶段无可执行测试，任务提前标记完成，review 内容从未填写
- **yscode**：exit=1，plan 阶段可能判断"无代码需要生成"而直接终止任务

根本原因：两者均无"纯分析/报告生成"模式，缺少以下能力组合：
1. 系统性地读取多个文件（无规划顺序地遍历 16 个文件）
2. 跨文件关联分析（如 evaluator.py 的 import 与 llm_client.py 的导出对照）
3. 将分析结论结构化写入 markdown（而非通过 test 验证完成度）

---

## 三、Claude 本体 review 结果摘要

### yansh 生成代码（2 Major + 3 Minor）

| 维度 | 评级 | 关键发现 |
|---|---|---|
| 架构完整性 | ✅ | 16 文件齐全 |
| LLM 客户端 | ⚠️ | stream_chat 缺 system 参数，传入被忽略 |
| Router 逻辑 | ✅ | 括号深度计数 + _try_fix_json，健壮 |
| Pipeline 执行 | ✅ | 递归设计清晰，并行用 Queue+Thread |
| 技能实现 | ❌ | **Major**：evaluator.py `from llm_client import call_llm`（ImportError）；web_search yield dict 而非 str |
| 错误处理 | ✅ | 各路径有兜底 |

### yscode 生成代码（0 Major + 3 Minor）

| 维度 | 评级 | 关键发现 |
|---|---|---|
| 架构完整性 | ✅ | 16 文件齐全 |
| LLM 客户端 | ✅ | 单例模式，接口干净 |
| Router 逻辑 | ⚠️ | rfind 提取 JSON（值含 `}` 时误截）；参数规则硬编码在 system prompt |
| Pipeline 执行 | ✅ | ThreadPoolExecutor，有超时检测 |
| 技能实现 | ✅ | 四技能均严格 yield str，接口契约正确 |
| 错误处理 | ✅ | 各路径有兜底 |

---

## 四、综合对比

### 代码质量

| 指标 | yansh | yscode |
|---|---|---|
| Major 问题 | 2（evaluator ImportError + yield 类型错误） | 0 |
| Minor 问题 | 3 | 3 |
| 能否直接运行 | ❌（evaluator 必然 crash） | ✅（Minor 不影响主流程） |
| 架构设计 | Router 更健壮（括号深度 + JSON 修复） | 技能接口更规范（yield str 契约） |

### review 能力

| 指标 | Claude | yansh | yscode |
|---|---|---|---|
| 能否完成 review | ✅ | ❌ | ❌ |
| 跨文件关联分析 | ✅ | — | — |
| 代码引用精准度 | 高（含行号和代码片段） | — | — |
| 严重度判断 | 有 Major/Minor 分级 | — | — |
| 适用场景 | 静态分析、审查 | 代码生成、修改 | 代码生成、修改 |

---

## 五、结论

1. **yansh 和 yscode 不具备代码 review 能力**——这不是失败，而是设计定位不同。它们是代码生成工具，不是静态分析工具。

2. **代码生成质量**：yscode 生成的代码可直接运行（仅 Minor 问题）；yansh 生成的代码有 2 个 Major bug 阻断核心功能（evaluator），但 Router 的 JSON 处理更健壮。

3. **各有侧重**：yansh 在防御性编程（JSON 容错）上更好；yscode 在接口契约（技能统一 yield str）上更规范。两者组合才是完整的"生成 + 审查"能力。

---

*原始详细 review 见 `AB-test/review_by_claude.md`*
