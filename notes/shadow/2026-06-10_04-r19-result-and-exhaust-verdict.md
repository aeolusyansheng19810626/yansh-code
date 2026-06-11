# R19：最新代码端到端验证 + 轮次耗尽兜底（2026-06-10）

> 上游：四轮 review 修复（见 ./2026-06-10_03-fable5-fixplan.md）合入 main 后，首次端到端跑 miniQL 验证。
> ws：`AB-test/longrun-miniql-r19`（干净，仅 PROMPT.md 强化版 + data/）。模型 sonnet-4-6，mode=solo。

## 结果数据

| | 框架判定 | 黑盒验收 | 成本 | 耗时 | input token |
|---|---|---|---|---|---|
| R14 | success | 10/10 | $13.2 | — | — |
| R18 | success | 10/10 | $15.84 | — | — |
| **R19** | **❌ false（达 120 轮上限）** | **✅ 10/10** | **$11.79** | 961.72s | 3.62M |

- token：sonnet input 3.58M / output 65K；haiku 38K/3.6K（dispatch_subagent 分担）。
- files_modified：miniql 全套 13 模块 + tests/test_{lexer,parser,analyzer,executor}.py + 几个 `_test_*.py` 临时脚本。**无 tests/test_smoke.py**。

## 核心发现：success=false 是假阴性（功能满分，框架误判）

黑盒 10/10 全过（投影/三值逻辑、INNER/LEFT JOIN、GROUP BY+COUNT+AVG跳NULL、HAVING、ORDER BY+LIMIT+NULL末、子查询IN、EXPLAIN谓词下推、语义错误、解析错误）。功能完全正确，框架却报 false。

根因全是 **sonnet harness 效率短板，与本轮 review 改动无关**：
1. 干净 r19 无 agent_state.md 先验 → 早期约 6 轮试 `python3`/`python`/`which` 探路（轮 8-13）。
2. **轮 47 一次跑题 task_complete(success=true)**：summary 写的是"已提取 7 个文件签名…"——像把自己当成调研子任务，角色漂移（3.6M token 多次 compact 后典型现象）。随后还 dispatch_subagent 让 explorer 重读签名。
3. 后期轮 115-120 在反复手跑 `python -m miniql <各种SQL>` 验证而不 task_complete，直接撞 120 轮上限。
4. 最终 gate 走「主 loop 轮次已耗尽 → 跳过测试 → 直接 failed」分支，`test_command=""`（根本没跑测试）。

## 降本增效实证：本轮 review 改动这次几乎没体现

印证 ./2026-06-10_03 的判断（本轮是修判定准确性，不提解题效率）：
- **#6 no_smoke 没触发**：targeted 单测在轮次耗尽前从未全绿 → 到不了 gate 的 passed 分支 → #6 只在测试通过后检查 smoke，够不到。
- **#2/#3 收敛/确认改进没机会发挥**：最终走「轮次耗尽 → 直接 failed」分支，绕过了这些路径。
- 成本 $11.79 < R14 $13.2 不是因为优化，是**提前撞墙**。

## 新假阴性来源（review 未覆盖）

`#3` 修的是「gate 绿但 agent 没重宣告」假阴性；但**没修「轮次耗尽时功能其实满分却无条件 failed」**。同样污染 AB 成功率统计，只是触发路径不同（total_rounds 耗尽 vs 未重宣告）。

## 兜底修复方案（本笔记落地项）

**位置**：`agent.py` solo() gate 循环顶部「主 loop 轮次已耗尽」分支（约 4196-4200，原 `gate_status="failed"; break`）。

**改法**：轮次烧光 ≠ 失败，而是「用尽机会时做一次最终裁定」。break 前跑一次测试，按与正常 pass 分支**完全相同的语义**判 gate_status（只是不再回灌、不做确认 drive）：
- 测试红 / 无命令 → failed / no_command（与现状一致）；
- targeted 绿 + （改入口且无 smoke）→ no_smoke（暴露真实缺陷，比 failed 更准确——miniQL R19 即此情形）；
- targeted 绿 + 不缺 smoke → passed（救回假阴性）；
- 全量兜底绿 → coverage_unknown。

抽 helper `_final_gate_verdict(ws_path, timeout)` 复现 pass 分支判定语义（detector 选择 + judge + no_smoke 检查），轮次耗尽分支调用它。**只改这一个分支**——gate_round 达 8 轮上限、同错收敛、超时早停这些 break 时测试已确认为红，failed 正确，不动。

**预期对 R19 的效果**：success=false(120轮) → no_smoke（功能对但缺端到端 smoke），AB 统计里从"莫名失败"变成"缺 smoke"——准确归因。若 agent 写了 smoke 且全绿则 passed。

## 兜底实现 + 三轮验证（R19/R20/R21，全部 sonnet 干净 ws）

兜底已实现（`_final_gate_verdict`，merge 至 main）；R20 暴露 collected-0 边界，R21 验证修复。

| | 框架判定 | 真因 | 黑盒 | 成本 | 耗时 | tests/ |
|---|---|---|---|---|---|---|
| R19 | false（旧逻辑直接 failed） | 烧满120轮,test_cmd="" 没跑 | 10/10 | $11.79 | 962s | 写了 |
| R20 | failed（兜底裁定,但 collected-0 误判） | 没写tests,全量 pytest collected0(rc=5) | 10/10 | $13.51 | 869s | 没写 |
| R21 | **no_command（准确归因）** | 没写tests + HAVING bug | 9/10 | $13.86 | 536s | 没写 |

**修复演进**：
- R19→R20：加 `_final_gate_verdict`——轮次耗尽不再无条件 failed，会跑最终裁定。✓
- R20→R21：`_no_tests_collected`(rc=5 / "no tests ran" / "collected 0 items")→ no_command，正常 gate + 兜底两处统一。✓ R21 实测 `collected 0 → 最终裁定：no_command`。

**R21 是诚实判定的范例**：agent 没留测试 + HAVING 真有 bug（黑盒9/10，同 R17 易错点）→ 框架判 no_command（不假绿掩盖 bug，不误报失败，如实"无可复核测试"），success=False 结论正确。

## 重要观察（设计层缺口，非本轮范围）

**sonnet 三次都没建正式 tests/（R20/R21 只写根目录 `_test_*.py` 手测脚本）或写了但烧满轮次（R19），且三次都烧满 120 轮。** `_SOLO_ROLE`「必须留下可运行测试」+ #6 smoke 硬性判据**对 sonnet 约束力不足**——它倾向手测 SQL 而非建 tests/。后果：agent 系统性不写测试时，gate 永远 no_command，#6 smoke 永远够不到（#6 只在 passed 分支检查）。测试复核机制对"不写测试的 agent"整体失效。这是 prompt 遵从性 / sonnet harness 行为问题，需后续从 role prompt 强约束或 gate 端"无 tests/ 即判不完成"角度解决。

## 已完成
- `_final_gate_verdict` 兜底 + collected-0 边界（`_no_tests_collected`）→ 均 merge main，742+ passed 零回归。
- harness 效率短板（探路/角色漂移/手测烧轮次）是 sonnet 固有，agent_state.md 跨 run 注入可缓解探路（R18 已验证），干净首跑无解。
