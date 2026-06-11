# Fable 5 独立根因分析：sonnet solo 烧满 120 轮（2026-06-11）

> 对 ./2026-06-11_03-fable5-rootcause-prompt.md 六个问题的回答。
> 证据：两份 run.log 全文、agent.py（_SOLO_ROLE / _solo_drive / solo / _compact_messages）、
> tools.py（execute_command / _update_agent_state / _DANGEROUS_PATTERNS）、sonnet ws 的 agent_state.md。

## 0. 先修正三处转述不准的地方

1. **「`&&` 让 cd 失败后命令在错误 cwd 静默跑」——不对**。`&&` 短路：cd 失败后右侧命令根本不跑，
   错误信息明确存在（stderr 有 "cannot find the path"，工具结果会回传，只是 run.log 不打 stderr）。
   真正的「静默失败」另有来源（见根因 ①B shell 身份矛盾）。
2. **「轮1 起反复 cd /workspace」——实际轮 1-8 是连写 8 个模块没跑任何命令**，轮 9 才第一次尝试运行
   且被 `python -c` 安全拦截。开局违反了 _SOLO_ROLE「每单元立即验证」，且第一次验证就撞框架 deny 规则。
3. **「50 次烧在环境探路」是三个不同根因的混合**，不是一个（见下），其中约一半与「初始环境无知」无关：
   - A 初期环境先验错配：轮 9-25，约 17 轮
   - B **compact 灾难后的二次探路：轮 45-65，约 14 轮纯重复**（你的归因里没有这一条）
   - C stale-pyc 幻象：轮 94-103，约 10 轮

## 1. 根因排序（按对 ~69 轮浪费的贡献）

### ① 环境契约缺失 + shell 身份矛盾（~25-30 轮；框架可消除，确定性）

`execute_command` 是 `shell=True` → **cmd.exe**，但 PATH 上有 Git 的 unix 工具：
`pwd`/`which` 返回 `/c/Users/...` 的 MSYS 路径——**模型收到的全部证据都说「这是 bash」，
实际执行的是 cmd.exe**。决定性证据：轮 108 `echo "no error raised"` 输出带引号（cmd 特征，bash 去引号）。

于是 sonnet 的 bash 写法全部怪异失败且无清晰报错：
- 轮 17 `PYTHONUTF8=1 python3 ...`（cmd 不认 VAR=x 前缀）
- 轮 32 `cat > data/emp.csv << 'EOF'`、轮 78 `python3 << 'EOF'`（heredoc 在 cmd 无效）
- 轮 59 `echo "Exit: $?"`（$? 不展开）
- 轮 12 `2>&1 | cat`（管道吃掉退出码）

叠加三件框架从未告知的事：cwd 已固定为 workspace 根（无需 cd）、该用哪个解释器、
**PYTHONUTF8/PYTHONIOENCODING 框架已在 env 设好**（tools.py:417）——而 requirement 文本
反复强调 GBK 风险，等于主动诱发了 sonnet 的编码焦虑（轮 8/17/22 的 PYTHONUTF8 探路）。
`/workspace` 是容器时代训练先验，一行 prompt 即可杀死。

opus 的「容错」本质是**避开 shell 特性**：`python -c` 被拦后 1 轮内改写 _scratch.py 文件跑；
`;` 串联在 cmd 失效后（轮 35 无输出）1-2 轮内拆成单命令。它不是更懂这个环境，是**把 shell
当不可信黑盒，只发最简单命令**。sonnet 在同样的坑里 10+ 轮不更新假设。

### ② compact 过激（~15-20 轮；框架可消除）

轮 44 触发压缩：**40510 → 4701 tokens，丢 88%**，发生在 HAVING 深度调试中途。直接证据链：
- 压缩前（轮 23-41）sonnet 已学会裸跑命令（不带 cd）；
- 压缩后轮 52-60 **退回 `cd <完整绝对路径> && ...` 前缀**，并重跑早已验证过的
  `py_compile errors.py`、`python3 --version`、`pwd && ls`——「无需 cd」「这些文件已编译通过」
  的知识被摘要丢掉了。
- compact 注入的 agent_state.md 救不了场：它只有命令白/黑名单，没有「cwd 即根目录」这类环境事实。

opus 全程未触发 compact（51 轮没到 40k 阈值）——**对照组天然干净，所以这条只打 sonnet**。
sonnet 后 76 轮是在「前 44 轮被压成 4.7k 摘要」的地基上跑的。

### ③ 调试幻象 + deny 规则火上浇油（~10 轮；半模型半框架）

轮 84-103：sonnet 修了 expression.py 但 `python3 -m miniql ... HAVING` 仍错（真 bug 在
optimizer 把含聚合的 HAVING 谓词下推过了 Aggregate，轮 106 才修对）。「修复无效」让它推断
**stale .pyc 缓存** → `find -delete` 被安全拦截 → `python3 -B` → importlib/inspect 查源码，
约 10 轮追幻象。假设更新慢是模型固有；但两条 deny 规则放大了代价：
- `\bpython\s+-c\b` 拦 `python -c` 却**放行 `python3 -c`**（sonnet 后期大量使用）——规则没防住
  任何东西，却把两个模型的第一次运行尝试都打断（sonnet 轮 9、opus 轮 5），并把 sonnet 推向
  「python3 + 根目录草稿脚本」工作流。
- `find -delete` 拦截让 pyc 幻象多绕了几轮。

### ④ 收尾机制缺失（不直接烧轮次，但决定 success=false；框架可消除）

agent **完全不知道 120 轮上限的存在**，也不知道自己用了多少轮。唯一的收敛信号是 token 预算
提醒——轮 28 就触发了（输入 token 累计 O(N²)，作为进度信号是噪音），之后 92 轮零信号。
轮 120 被掐时 sonnet 正在正常修真 bug（HAVING/除零/display_name 都修对了，黑盒 9/10 证明），
不是空转——所以无进展熔断永远不触发，gate 的 smoke 硬卡也永远够不到（它在 drive 之间才检查，
而 sonnet 从未走出第一个 drive）。**实验1 gate v2 在这种失败模式下结构性失效，确认实验1 结论。**

### ⑤ 模型固有（缓解不可消除）

Linux 习惯先验（python3 / /workspace / heredoc / VAR=x）、从矛盾证据更新假设慢（同坑 10+ 轮 vs
opus 1-2 轮）、深度优先调试不回头看全局清单、「先草稿脚本后正规测试」的偏好（tests/ 永远排在
「等会儿」）。①-④ 修掉后这些先验大多无处可踩。

## 2. opus 为何不踩：习惯先验与能力各占一半

- **可注入的（先验类）**：用 `python` 不用 python3、不依赖 cd、写测试收尾、删临时文件。
  → 环境卡 + prompt 可以给 sonnet。
- **不可注入的（能力类）**：1-2 轮内从异常输出更新假设、把 shell 当黑盒的防御性命令风格、
  「自测全绿 → 立即 task_complete」的完成感。
  → 框架方向不是教会它，而是**让这种能力不被需要**（确定性消除踩坑面）。

## 3. 最高杠杆 3 处改动

1. **启动环境卡（确定性，杀根因①）**：solo() 启动时框架自己探测（跑 `python --version`、
   检测实际 shell）并注入 system：
   > 命令由 cmd.exe 执行（不是 bash：禁 heredoc / VAR=x 前缀 / $? / 2>/dev/null）；
   > cwd 恒为 `<绝对路径>`，永远不需要 cd；解释器统一用 `python`（3.11.9）；
   > PYTHONUTF8/PYTHONIOENCODING 框架已设置，无需处理编码。
   同时把这段写进 agent_state.md（解决「干净 ws 首跑为空」）。
   更激进变体：execute_command 固定走 Git Bash（`["bash","-lc",cmd]`）——模型的 Linux 先验
   从 bug 变 feature，shell 身份矛盾根除；或至少拦截 `cd <不存在路径> &&` 返回结构化提示
   「cwd 已是 X，无需 cd」。
2. **compact 结构化保底（杀根因②）**：摘要 prompt 强制保留四段：环境事实 / 已完成模块清单 /
   当前 bug 假设与已排除假设 / 已验证命令。环境卡像 plan_anchor 一样每次 compact 重注入
   （_compact_messages 已有 plan_anchor 挂点，照抄即可）。solo 长任务 40k 阈值过激：
   提高阈值或加大 keep_recent_pairs。
3. **drive 内里程碑 + 轮次可见（杀根因④，enforcement 前移）**：每 25-30 轮注入确定性 checkpoint：
   「轮次 X/120。自检：tests/ 存在？pytest 绿？requirement 清单逐项核对。缺什么现在补。」
   轮 ~100 注入最后通牒（把 _final_gate_verdict 提前为回灌而非事后裁定）。
   smoke 硬卡从 gate 前移进 drive：写过 __main__.py 且轮次 > N 仍无 tests/test_smoke.py → 注入硬要求。
   不误杀：只注入信息与要求，熔断逻辑不变，正常工作的 agent 只多读一条消息。

## 4. 确定性 vs 概率性

| 改动 | 性质 |
|---|---|
| 框架自探环境 + 注入环境卡 | 确定性 |
| execute_command 固定 bash / cd 拦截改写 | 确定性 |
| 轮次计数注入 / 里程碑 checkpoint / 最后通牒 | 确定性（注入必达；遵从概率性但有兜底） |
| smoke 硬卡前移进 drive | 确定性 |
| agent_state 扩大记录范围（含 cd 前缀/复合命令失败） | 确定性 |
| deny 规则修正（python -c 与 python3 -c 一致化或放行） | 确定性 |
| prompt 行为指令（别 cd / 用 python / 及时收尾） | 概率性 |
| compact 摘要质量 | 概率性（但保留字段清单是确定性约束） |

## 5. 收尾问题：大部分是下游，不是独立根因

烧满时 sonnet 在干正事（修真 bug 且修对了）。没收尾的链条：前期浪费挤掉测试预算 →
草稿脚本工作流没有「全绿」里程碑可触发完成感 → 不知道预算存在 → 没有任何外部信号说「该收了」。
独立成分只有「tests/ 永远后置」的偏好，这正是 smoke 硬卡前移要解决的。
防误杀：checkpoint 只问不杀；真熔断仍按无进展判定（现行逻辑已区分「跑命令也算进展」，保留）。

## 6. 盲点（你没列、可能更深的）

1. **compact 是被低估的主根因**（根因②）——你的归因表把轮 45-65 算进「环境探路」，
   实际是压缩丢上下文后的重复劳动，修法完全不同。
2. **shell 身份矛盾**比「没告诉它 cwd」更深：环境主动给出「这是 bash」的假证据（pwd/which 的
   MSYS 路径），这不是信息缺失而是信息误导。只补 prompt 不改 shell 一致性，模型仍会被骗。
3. **`python -c` deny 规则**：可被 python3 -c 绕过 = 零安全收益；却塑造了两个模型的整个
   验证工作流。建议放行（超时已有管控）或一致化。
4. **agent_state 三盲区**：正则 `^(py|python|pytest)` 漏掉所有 `cd ... && python` 灾难命令
   （最有诊断价值的失败不留痕）；首跑为空；**最终 gate 自己跑的 `pytest`（collected 0 → exit 5）
   被记成失败命令，污染该 ws 下次先验**（已在 sonnet ws 的 agent_state.md 实锤）。
5. **token 预算提醒是噪音信号**：按累计输入 token（O(N²)）轮 28 就喊收敛，与真实进度无关；
   若 agent 真听了反而可能挤掉建 tests/ 的意愿。应改按轮次/产出里程碑。

## 经济性注脚

sonnet 烧满 120 轮才 $12，opus 51 轮 $36.6。若上述改动把 sonnet 压到 ~60 轮且可靠收尾，
单跑 $6-8、success=true——性价比反超 opus 5×。这是做框架优化而非「换 opus」的经济理由；
与 2026-06-11_02 的「省心选 opus」不矛盾：那是单次交付视角，这是框架产品视角。
