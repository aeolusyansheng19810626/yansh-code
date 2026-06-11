# 实验2：正则引擎泛化验证 — 功能泛化成立，但暴露 gate 确认循环对「早收尾 agent」的有害 churn（2026-06-11）

> 换非 SQL 强耦合任务（minire 正则引擎，Thompson NFA），验证 fixplan 结论是否过拟合 miniQL。
> ws=minire-1，sonnet+gate，PROMPT+oracle 由 sonnet 起草/opus 审校（oracle=Python re.fullmatch 当 golden）。
> command `.claude/commands/run-solo-exp.md` 已固化（本会话新建，需重启 CC 才在 slash 列表生效）。

## 一、结果

| 指标 | minire-1 | 对照 miniQL(fixplan2/3) |
|---|---|---|
| **黑盒（对照 Python re，15 组）** | **15/15 全过** | 10/10 |
| **agent 自测最终真实状态** | **175 passed 全绿** | 121-143 绿 |
| **框架 final_success** | **False** ❌ | true |
| **test_result（gate 记录）** | **fail** | pass |
| 轮次 | **119（churn 到上限）** | 79-83 |
| cost / duration / tokens_in | **$18.58 / 1522s / 5.83M** | $10.7-12 / ~1000s / 3.3-3.6M |
| gate 回灌 | **满 8 轮上限** | 1-2 轮 |

**核心矛盾**：功能 100% 正确（黑盒满分 + 自测 175 绿），但框架判 success=False。

## 二、双层结论

### 1. 功能泛化成立 ✅
fixplan 框架让 sonnet 在非 SQL 强耦合任务（正则引擎：lexer→parser→compiler→NFA matcher）上同样产出**完全正确**实现：黑盒 15/15 对照 `re.fullmatch`、自建 175 个分层测试全绿（test_lexer/parser/compiler/matcher + smoke）。环境卡同样生效（轮1 直接写 errors，零探路）。「修好框架→弱模型能做对强耦合任务」**普适，非 miniQL 特例**。

### 2. 框架判定层暴露新失效模式 ❌：gate 确认-回灌循环对「早收尾 eager agent」有害 churn
- minire 耦合度低于 miniQL → sonnet **轮32 就全绿主动 task_complete**（本该 success=true）。
- 但 gate（#3 fix「绿后补一次确认 drive」）反复回灌确认 → agent 把每次复核当成「再加测试」的邀请 → 扩到 175 个测试、中途引入并修复自身回归（`[\n]` 字符类转义 / `{...}` 计数量词的自写测试与实现不符）、甚至跑题去 `pip install -e` 打包。
- churn 到 **轮119 / gate 满8轮** 双上限耗尽，`_final_gate_verdict` 在耗尽点撞到某次 churn 的**失败快照** → 记 test_result=fail → success=False。
- **但进程结束后实测自测 175 全绿 + 黑盒 15/15** → 这是 **gate 自己制造的假阴性**，且把轮32的干净结果 churn 成轮119/$18.58。

### 3. 比实验1 残留缺口更尖锐
- 实验1 缺口：全绿但 agent 没宣告 → 不认可（依赖 `_ever_completed`）。
- 实验2 暴露：**gate 反复确认把本该 success=true 的早收尾干净结果，主动 churn 成假阴性 + 成本翻倍**。gate confirm-回灌制造了它本要防止的假阴性。

## 三、根因：框架过拟合 miniQL 的「接近上限才收尾」行为剖面

呼应 [[project-agent-task-difficulty]]：难度=耦合度+验证难度。
- miniQL 耦合度高 → agent 79-117 轮才收尾 → gate 回灌只跑 1-2 轮就停（agent 已接近上限），churn 被掩盖。
- minire 耦合度低 → agent 轮32 收尾 → gate 有大量剩余轮次反复回灌 → churn 充分暴露。
- fixplan 阶段1 的收尾/复核机制（最后通牒、gate 确认 drive、回灌）都是按 miniQL 剖面调的，**对「早收尾」任务剖面过拟合且净负面**。

这正是实验2 的目的（避免过拟合 miniQL）——结果：**功能泛化成立，但框架判定层确实过拟合了 miniQL 的行为剖面**。

## 四、修复方向（待评估 ROI）
1. **gate 确认循环止损**：agent 已 task_complete 且 gate passed 后，**不应无限回灌**；「绿后补一次确认」应是「确认一次即停」，而非每次 agent 再动就重新触发。当前疑似 #2/#3 交互让「本轮有新增写」持续重置收敛检测。
2. **_final_gate_verdict 用当前真实状态**：耗尽时应**重跑一次**全量测试取当前状态，而非用 churn 中的失败快照。
3. **gate 回灌应区分「红→要修」与「已绿→别催」**：对已全绿的 agent，gate 确认应只读不催，避免诱发 eager agent 扩测试 churn。

## 五、CC 练习产出
- `.claude/commands/run-solo-exp.md`：通用 solo 实验流程 slash command（建ws→后台跑→Monitor→黑盒→落盘），含已知坑防护（黑盒传绝对路径/PYTHONUTF8/勿传opus/oracle不进ws防过拟合）。本会话新建，重启 CC 后 `/run-solo-exp <template-dir> <run-tag>` 可用。
- `AB-test/minire-template/`：PROMPT.md（强契约）+ run_accept.py（re.fullmatch oracle，15 组自一致性验证通过）。sonnet 起草、opus 审校。
