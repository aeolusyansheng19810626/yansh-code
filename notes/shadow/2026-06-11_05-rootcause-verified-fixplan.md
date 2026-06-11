# solo agent 提效 fixplan（根因已核实，已批准执行）（2026-06-11）

> 承接 ./2026-06-11_03（提示词）、_04（Fable 5 分析）。本文 = 我对 Fable 5 论断的**逐条核实结论**
> + 修正后根因 + 分阶段可执行计划。**已获用户批准执行**（先做 A+B+快赢，C 第二步）。
> 目标：sonnet 干净 ws 跑 miniQL，从「烧满120轮/success=false」改善到「~60轮内干净 task_complete」。

## 一、Fable 5 论断核实（全部成立，实测/源码逐条确认）

| 论断 | 核实 |
|---|---|
| **shell 真身=cmd.exe 非 bash** | 实测 `echo "hello"`→输出带引号 `"hello"`（cmd 特征）。tools.py:418 `Popen(shell=True)` 无 `executable=`，Windows 走 COMSPEC=cmd.exe |
| `&&` 短路（非「静默在错 cwd 跑」） | 我原归因错，Fable 5 对 |
| PYTHONUTF8/IOENCODING 框架已设 | tools.py:417，sonnet 编码探路纯多余；PROMPT 强调 GBK 反诱发焦虑 |
| deny `\bpython\s+-c\b` 放行 `python3 -c` | tools.py:252，零安全收益、却打断两模型首次运行、把 sonnet 推向 python3+草稿脚本流 |
| compact 轮44 **40510→4701 丢88%** | run.log:241-242 实锤；opus 0 次压缩（51轮没到阈值） |
| token 提醒轮28 喊收敛（O(N²)噪音） | run.log:87 `solo token 增量 626194 > 600000` |
| agent_state 被 gate 的 collected-0 `pytest` 污染 | sonnet ws agent_state.md「失败命令」首条=`pytest` |
| agent_state 漏记 `cd ... && python` 复合命令 | agent_state.md 无 cd 开头命令 |

Fable 5 挖出我漏的两条更深根因：**②compact 二次探路**（我误算进环境探路，修法不同）、
**shell 身份矛盾是「误导」非「缺失」**（`pwd`/`which` 返回 MSYS 路径，主动给「这是bash」假证据）。

## 二、修正后根因图景（按对 ~69 轮浪费贡献）

1. 环境契约缺失 + shell 身份矛盾（~25-30轮，确定性可消除）
2. compact 过激丢88%上下文→二次探路（~15-20轮，确定性可消除）← 被低估的主根因
3. 调试幻象 + deny 规则放大（~10轮，半模型半框架）
4. 收尾机制缺失（不烧轮次但决定 success=false）
5. 模型固有先验（①-④修掉后大多无处可踩）

## 三、分阶段 fixplan

### 阶段1：A 启动环境卡（确定性，杀根因①）
- **位置**：`solo()` 启动注入段 agent.py:~4230（`_role`/`sys_prompt` 组装处，状态文件注入在 4236-4245）。
- **做法**：solo() 启动时框架自探（`python --version` 实际可用解释器；记录真实 cwd 绝对路径），
  注入一段 system「环境契约」：
  > 命令在 **cmd.exe**（阶段3改 bash 后改此句）执行；cwd 恒为 `<abs>`，**永不需要 cd**；
  > 解释器统一 `python`（非 python3）；编码框架已设 PYTHONUTF8/IOENCODING，**无需处理编码**；
  > 复合命令风格按当前 shell（cmd：禁 heredoc/VAR=x前缀/$?/2>/dev/null）。
- **同时**把这段写进 `.yansh/agent_state.md`，解决「干净 ws 首跑为空」。

### 阶段1：B compact 结构化保底（确定性，杀根因②）
- **位置**：`_compact_messages`（agent.py:1360）**已有 plan_anchor 重注入挂点**（1434-1437）——
  把「环境契约卡」做成与 plan_anchor 并列的 `env_anchor`，每次 compact 前插入 system，照抄 plan_anchor 实现。
- **摘要保留四段**（改 `_SUMMARIZE_SYSTEM` 强制项）：环境事实 / 已完成模块清单 / 当前bug假设与已排除假设 / 已验证命令。
- **阈值**：solo 长任务 40k 过激（run.log 实测 40510 触发）。提高阈值或加大 `keep_recent_turns`/`keep_recent_pairs`
  （config `compress_threshold`/`keep_recent_turns`，default 6000/3；solo ws 的 .yansh/config.json 实际设了 40k——确认并调高）。

### 阶段1：快赢 bug（确定性，低风险）
1. **deny 正则**（tools.py:252）：`\bpython\s+-c\b` → 一致化（也拦 `python3 -c`）**或直接放行**（超时已管控）。Fable 5 倾向放行/一致化。
2. **agent_state 不被 gate 污染**（tools.py:51 `_update_agent_state`，调用处 479）：gate 自己跑的 `test()` 也走
   execute_command→479 记录→collected-0(exit5) 被记成失败命令。修：gate test 路径不写 state，或 _update_agent_state 跳过 collected-0/exit5。
3. **agent_state 记录 cd 复合命令**：正则放宽，让 `cd ... && python` 这类失败命令留痕（最有诊断价值的反而没记）。
4. **token 提醒改按轮次/产出里程碑**（`_SOLO_TOKEN_BUDGET` agent.py:139=600_000，提醒逻辑仿 audit 2985）：
   现按累计输入 token（O(N²)）轮28 即误报，改按 rounds_used 或「无新产出」触发。

### 阶段1：drive 内里程碑 + 轮次可见（杀根因④，enforcement 前移）
- 每 25-30 轮注入确定性 checkpoint：「轮次 X/120。自检：tests/ 存在？pytest 绿？requirement 逐项核对，缺什么现在补。」
- 轮~100 注入最后通牒（把 `_final_gate_verdict` 提前为**回灌**而非事后裁定）。
- **smoke 硬卡从 gate 前移进 drive**：写过 __main__.py 且轮次>N 仍无 tests/test_smoke.py → drive 内注入硬要求
  （这才解决实验1 v2「硬卡在 gate 够不到」——sonnet 从未走出第一个 drive）。
- 防误杀：只注入信息/要求，熔断逻辑不变（现行已区分「跑命令也算进展」），正常 agent 只多读一条消息。

### 阶段2：C execute_command 固定走 bash（确定性根除根因①核心，需先验证）
- **位置**：tools.py:418 `Popen(run_command, shell=True, ...)` → 改 `["bash","-lc",command]` 形态（或 `executable`）。
- **收益**：shell 身份矛盾根除；**sonnet 的 Linux 先验从 bug 变 feature**（heredoc/VAR=x/$?/2>/dev/null 全生效），50 次探路一大半消失。
- **风险/前置**：确认 Git Bash 可用路径、不破坏现有 Windows 命令/sandbox docker 路径/超时与 interrupt 逻辑；单独 A/B 验证。

## 四、验证方法
改完阶段1 → sonnet 同任务干净 ws 重跑 → 看：①「环境/解释器/编码探路」从50降到多少；②是否仍触发 compact 二次探路；
③总轮次；④能否 120 轮内干净 task_complete（success=true）；⑤黑盒维持~10/10。对照 baseline（exp1-gatev2: 120轮/$12/success=false/9-10黑盒）。

## 五、经济性
sonnet 烧满120轮仅 $12 vs opus 51轮 $36.6。阶段1 若把 sonnet 压到~60轮且可靠收尾 → 单跑 $6-8、success=true，
性价比反超 opus 5×。这是「做框架优化」而非「换 opus」的经济理由（与 _02「单次交付选 opus」不矛盾：那是交付视角，这是框架产品视角）。
