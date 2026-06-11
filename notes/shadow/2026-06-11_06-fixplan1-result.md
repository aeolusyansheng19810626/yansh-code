# fixplan 阶段1 端到端验证结果：sonnet solo 首个 final_success=true（2026-06-11）

> 承接 ./2026-06-11_05（fixplan）。改动 commit 7adf42c（分支 exp/solo-fixplan-stage1）。
> ws=longrun-miniql-fixplan1（干净，仅 PROMPT 强化版+data），SOLO_TEST_ENFORCEMENT=gate，sonnet-4.6。

## 一、结果对照（baseline = exp1-gatev2，同任务同框架）

| 维度 | baseline (gatev2) | **fixplan 阶段1** | 判定 |
|---|---|---|---|
| 主动 task_complete | **从不**（烧满120轮） | **轮117 success=true** | ★质变 |
| final_success | false（假阴性） | **true** | ★质变 |
| gate_status | no_smoke（仅兜底裁定） | **passed**（gate 复核 smoke 全过） | ★质变 |
| tests/ | **空** | 完整 143（单元114+smoke29） | ★质变 |
| smoke | 无 | 有，subprocess 真入口，gate 复核 100% | ★质变 |
| 黑盒 run_accept | 9/10 | **9/10**（唯一 fail：用例10 非法语法误报 SemanticError 而非 ParseError，真 bug 非框架） | 持平 |
| 总轮次 | 120 烧满 | 117 | 略降 |
| cost | $12 | **$19.11** | ↑反升 |
| duration | 758s | 1466s | ↑反增 |
| input tokens | 3.75M | 5.94M | ↑反增 |

## 二、轮次归因验证（fixplan ①②④ 直接验证，数据极干净）

| 指标 | baseline | fixplan1 | 说明 |
|---|---|---|---|
| 环境/解释器探路（python3 命令） | 大量 | **0** | A 环境卡：轮1 直接写 errors.py，零探路 |
| 手动 PYTHONUTF8 前缀 | 多次 | **0** | 环境卡说明编码已设 |
| cd 命令尝试 | 轮1-22 反复 | **0** | 环境卡「永不需要 cd」生效 |
| compact 触发 | 轮44（丢88%→二次探路14轮） | **0** | B：阈值40k→60k，整跑没到阈值，根因②彻底消除 |
| smoke 硬卡前移触发 | —（v2 在 gate 够不到） | **1（轮40+）** | sonnet 响应后补了完整 tests/+smoke |
| 最后通牒（轮100） | 无机制 | **1** | 催出轮117 主动 task_complete |
| token 收敛提醒时机 | 轮28（O(N²)误报） | 轮84（=70%，合理） | 根因⑤修正 |

**A 首跑实证**：agent_state.md 首跑即含 `python --version`（探测自动记）+ 完整环境契约段；run.log 轮1 直接 `写入 miniql/errors.py`。

## 三、核心结论：质变成功，但经济性预期落空（诚实修正）

**成功的**：fixplan 把所有「可注入」项全部注入成功——
- 环境探路 50/120 → **≈0**（①确定性消除）
- compact 二次探路 14轮 → **0**（②阈值提高后根本不触发）
- 行为从 baseline 级 → **opus 级**：sonnet 自建完整多文件 tests/ + subprocess smoke + 主动 task_complete，这是它靠 _SOLO_ROLE 提示从来逼不出、需 enforcement 硬卡前移+最后通牒才达成的。
- 假阴性（9/10 但 success=false）→ **真阳性（9/10 且 success=true）**：交付可信度质变。**solo 框架下 sonnet 首个 final_success=true**（此前仅 opus）。

**预期落空的**：fixplan 假设「消除探路 = 省轮次 = 压到 ~60 轮 $6-8、性价比反超 opus 5×」。**实测相反**：117 轮 / $19.11，比 baseline 还贵。
- 根因：省下的 50 轮探路**没变成节省，而是转化为生产性工作**——sonnet 一旦不绕环境，就把精力投进「建 143 个测试 + 反复跑 + 调真 bug」。不是低效，是「做了 baseline 没做的对的事」，而对的事本身耗资源。
- 印证 Fable 5「可注入（先验）vs 不可注入（能力）」二分：可注入的（环境先验/写测试/收尾）已全部修复，sonnet 行为=opus；**不可注入的（单位轮次效率：opus 51轮 vs sonnet 117轮搞定同任务）仍是能力差距**。

**修正经济性**：sonnet $19.11 真阳性 9/10 vs opus $36.6 真阳性 10/10。sonnet 仍便宜 ~2×（非预期的 5×），但黑盒少 1 分。「框架优化让弱模型可靠」**成立**（false→true）；「更省钱」**不成立**。结论：要可靠+省钱选「修好框架的 sonnet」（$19/真阳性/9-10），要一次到位满分选 opus（$36.6/10）。

## 四、第二次跑（fixplan2，+PROMPT 规格补强用例10）— 推翻悲观经济性结论

ws=longrun-miniql-fixplan2，PROMPT 在 ParseError 条加规格：「`SELECT FROM emp`/保留字出现在期望表达式位 → parser 阶段判 ParseError，不可延迟到语义层报 SemanticError」。

| 维度 | fixplan1 | **fixplan2（+PROMPT）** | baseline |
|---|---|---|---|
| final_success | true | **true** | false |
| 黑盒 | 9/10（用例10 fail） | **10/10**（用例10 修复） | 9/10 |
| 轮次 | 117 | **79** | 120 |
| cost | $19.11 | **$11.95** | $12 |
| duration | 1466s | **1097s** | 758s |
| tokens_in | 5.94M | **3.62M** | 3.75M |
| 收尾触发 | 最后通牒（轮100） | **自主（轮79，没等通牒）** | 从不 |
| smoke 前移 / compact | 1 / 0 | 0（早自建smoke）/ 0 | —/丢88% |

**★ solo 框架下 sonnet 首个真正满分：final_success=true + 黑盒 10/10**（此前仅 opus）。

**修正三-的经济性结论（之前过于悲观）**：fixplan1 的「$19/117轮比baseline贵」只是**偏贵的一次**。fixplan2 显示**方差大**：便宜端 79轮/$11.95，几乎和 baseline 同价（$12）却质变成功+满分。两次共性 = **可靠性稳定（都 success=true）**；差异 = 成本方差 $12-19（便宜端≈baseline，贵端 +60%）。fixplan 原「压到~60轮」预期在 fixplan2（79轮）**接近兑现**。
- ② PROMPT 规格补强直接 9/10→10/10，且 fixplan2 没绕用例10 的弯路（可能也是更早收尾的部分原因）——规格清晰度同时影响正确性与效率。
- 综合两次：「框架优化让弱模型可靠」**成立且稳定**；「更省钱」**取决于方差**——好的时候几乎免费拿质变，差的时候 +60%。仍远比「假阴性烧满轮次」可用。

## 五、方差坐实（n=4，2026-06-11）

| 跑 | PROMPT | 黑盒 | success | 轮次 | cost | tokens_in | 收尾方式 |
|---|---|---|---|---|---|---|---|
| fixplan1 | 旧 | 9/10 | true | 117 | $19.11 | 5.94M | 通牒后 |
| fixplan2 | +规格 | 10/10 | true | 79 | $11.95 | 3.62M | 自主 |
| fixplan3 | +规格 | 10/10 | true | 83 | $10.73 | 3.28M | 自主 |
| fixplan4 | +规格 | 10/10 | true | 107 | $14.98 | 4.72M | 后期催 |
| baseline | 强化 | 9/10 | **false** | 120烧满 | $12 | 3.75M | 从不 |

- **可靠性稳定**：4/4 全 final_success=true（baseline=false）。fixplan 把「假阴性烧满轮次」彻底修成「可靠真阳性」。
- **补强 PROMPT 后 3/3 黑盒满分**：规格清晰度（②，ParseError vs SemanticError）是 10/10 关键。
- **成本方差中等**：$10.7–19.1 / 79–117 轮。便宜端（f3 $10.73）低于 baseline，贵端（f1 $19.11）+60%，无极端坏案例。
- **担心的「耗尽假阴性」4 次均未触发**：通牒（轮100，静默注入 messages 不打 console）+ 预算提醒（轮84）最终都催动收尾（2 自主 / 2 后期催）。但遵从是概率性，n=4 没踩到 ≠ 缺口不存在——「轮次耗尽 + 全程没 task_complete」时兜底认可仍依赖 `_ever_completed`，理论上可假阴性。若要确定性消除，需把「轮次耗尽时全绿+smoke在 → 无条件认可」从依赖 _ever_completed 改为依赖 gate 客观结果（后续可选）。

## 六、待办
- 用例10 ParseError vs SemanticError 是 PROMPT 可强化的规格盲区（仿 R13 把判据写进 PROMPT），非框架问题。
- 阶段2 C（execute_command 固定 bash -lc）：本次环境探路已被 A 注入压到 0，C 的边际收益从「消除50轮探路」降为「让 sonnet 的 bash 先验从 bug 变 feature」——优先级可下调，因为 A 已基本解决问题。是否还做需重新评估 ROI。
- 方差：单次结果，行为塑造是概率性（最后通牒/smoke 硬卡是确定性注入但遵从概率），需多跑看稳定性。
