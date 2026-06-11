# Workflow 编排多模型对抗 review 实验（2026-06-11）

> 用 Claude Code 的 Workflow 工具，把 06-09 人肉跑的「多模型对抗 review」自动编排一遍。
> 目标代码：commit e424447「fix(gate): 消除 gate churn 假阴性（Fix A-E）」本身。
> 人肉版对照见 ./2026-06-09_05、./2026-06-09_07。

## 一、编排结构（Workflow 脚本）

`pipeline(REVIEWERS, reviewStage, rebutStage)` → `opus adjudicate`：
- **Review**：opus / sonnet / haiku 各自对照源码独立 review Fix A-E，结构化输出 findings。
- **Rebut**：每条 finding 由**另外两个模型**对抗反驳（prompt 逼「尽量反驳，无代码证据默认 real=false」）。
- **Adjudicate**：opus 终审，对照源码裁决真/伪 + 三模型画像，不只数票。

运行数据：**40 agents、1.76M tokens、15min**；2 个 rebut agent socket 断连失败（filter(Boolean) 自动吞掉，不影响）。

## 二、模型路由现状（方案1：ICA 连 Claude 家族）

- **fable** 在 ICA 不可用 → 排除。
- **haiku** 在 workflow agent 里 model ID 必须写全 `claude-haiku-4-5`（别名 `haiku` 不行）。
- **opus / sonnet** 用别名即可，ICA 路由通（冒烟确认：opus→claude-opus-4-8[1m]、sonnet→claude-sonnet-4-6）。
- workflow agent 的 cwd = 主 session repo 根，能直接 `git show` / Read 取证。
- gemini/gpt 走 ICA 但 review 质量低；deepseek 无 ICA 走自费——本次都不用。

## 三、裁决结论（opus 终审）

**Fix A 杀根因正确，无高危新 bug。** 5 confirmed / 7 refuted（18 findings）。

confirmed：
- **[medium] F3（三方独立命中）**：Fix B 全量回退绿时 coverage 被改成 `"full"` → `gate_status=coverage_unknown` → `final_success=False`。对「targeted collected-0 但项目全量绿 + agent 完成」仍是假阴性，根因「功能满分却 success=False」在该路径未真正消除（比改前的硬 failed 温和，但没断干净）。`_final_gate_verdict` 同病。
  - 修：Fix B 全量绿时给 passed 出口（标 `_arbitrated_green`，或重建过滤后的 targeted 命令保持 coverage=targeted）。
- **[medium] 测试缺口（三方一致）**：新增 test_gate_churn_fix.py 4 用例只覆盖 Fix A；Fix B/C/E 零单测，Fix D 的 delta（gate 回灌耗尽 while-else 调 _final_gate_verdict + _ever_completed 认可）也无测试驱动到达。
- **[low]×3**：Fix D 只对称化 while-else，timeout/同错收敛的两条 `failed; break` 仍硬编码绕过重测；`_final_gate_verdict` 缺 Fix B 的全量回退仲裁；Fix C 一次性标志终身不重置（设计取舍，可不修）。

## 四、对抗机制的价值（本实验最大收获）

**cross-model rebuttal 真的纠错了**：opus 提的唯一一条 **high 危 F1**（「`_no_tests_collected` 触发门与根因信号 rc=4 'file or directory not found' 错位，多层防御只 Fix A 生效」）被 **sonnet + haiku 各自实测 pytest 推翻**——pytest 对不存在文件 rc=4 时 stdout 仍含 'no tests ran' / 'collected 0 items'，门正常命中。opus 只读 rc 文档没实测运行时输出，对抗层把这条核心高危误报拦了下来。没有对抗，这条会带着「整套防御只有一层生效」的吓人结论进裁决。

## 五、模型画像（本次任务，用于选型）

| 模型 | 信噪比 | 强 | 弱 |
|---|---|---|---|
| **opus** | high | 行号/控制流追踪最准（F3 的 4486→4524→4634 链）、给可达性与后果分级、自我反驳诚实 | 唯一「只读文档不实测」翻车的高危误报 F1 |
| **sonnet** | medium | 实测驱动（跑 pytest 拿证据反驳 F1）、pathlib 逐场景手工展开、对自己 finding 不护短 | 重复提交编号混乱、F4 把止损归属搞错 |
| **haiku** | low | 也做了实测、test-gap 枚举完整 | 两条高危全误报、verdict 立场自相矛盾（判 real=true 却列理由证明不成立）、机械事实=finding |

选型一句话：要可信裁决 + 控制流精度用 opus，但**涉及运行时行为（pytest 输出/退出码）必须配实测**——这正是 sonnet 本次的强项。haiku 适合廉价铺量初筛，不能单独信。

## 六、与 06-09 人肉对抗的对照

- **自动化成功**：Workflow 把「N 模型并行 review → 跨模型反驳 → 裁决」从人肉一轮轮跑变成一条脚本，~15min 出结构化结论。
- **仍需人核**：裁决层 opus 已能拦误报 + 分级，但「coverage_unknown 是设计语义还是 bug」这类判断仍建议人复核（opus 自己也标 medium 而非拍死）。
- **跨厂商缺失**：人肉版的 gpt/gemini/deepseek 独家视角（gpt 抓信号语义硬伤、gemini 概念层）本次缺位——ICA 里这些要么质量低要么自费。Claude 家族梯度对抗能纠 opus 误报，但视角多样性不如跨厂商。

## 七、跟进项（待批准）

1. Fix B / `_final_gate_verdict` 加 passed 出口，消除 coverage_unknown 假阴性（F3，medium）。
2. 补 Fix B/C/D-delta/E 桩单测，含一条 coverage_unknown 假阴性回归测试。
