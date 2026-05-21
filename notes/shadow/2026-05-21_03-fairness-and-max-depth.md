# 2026-05-21 公平性反思 + max_depth 任务复测

## 这次笔记的主要价值

记录一次**实验设计错误的发现和修正**——比记录"成功对比"更值钱，因为提醒自己以后做对照实验时不要犯同样的错。

## 之前 A/B 测试的硬伤

前面三轮"Claude Code vs yansh" 对比实际上**不公平**：

1. **bug 是我注入的**——第二轮的 1 行 bug 我直接知道在 `tools.py:287`，不需要排查
2. **任务是我设计的**——第三轮的 max_depth 我刚跟用户讨论完语义，"max_depth=1 只列顶层"对我是 ground truth，对 yansh 只是 prompt 文本
3. **代码库我已经熟了**——整个会话已经聊了几小时，我对 yansh-code 的内部结构早就有完整记忆，yansh 每次冷启动只看到任务一句话

所以"Claude Code 用了 X 步、X 秒"全部是**开卷选手 vs 闭卷选手**的数据，不能用。

## 哪些结论仍然成立、哪些不成立

| 结论 | 是否仍成立 | 理由 |
|---|---|---|
| yansh 的 review loop 死循环 | ✅ 成立 | 架构问题，跟我开卷无关 |
| yansh 经常过度发挥（改不相关代码） | ✅ 成立 | prompt 问题，跟我开卷无关 |
| yansh 实现/测试自相矛盾 | ✅ 成立 | plan/code 协议割裂，跟我开卷无关 |
| "Claude Code 用了 5 步、30 秒" 这类具体数字 | ❌ 不成立 | 全是 inflated 数据 |
| "Claude Code 比 yansh 快 9 倍" | ❌ 不成立 | 同上 |

## 公平测试方案：派 subagent

用 Agent 工具起一个 general-purpose subagent，**只给任务原话 + 项目位置**，不告诉它：
- 这是 A/B 测试
- yansh 怎么做的
- 任务里的隐藏陷阱（如 dispatch 需要改）
- 任何答案/暗示

subagent 没有当前会话的上下文，只能像 yansh 一样**冷启动**探索。

## 第一次公平测试结果（Opus 4.7 subagent vs Sonnet 4.6 yansh）

任务："给 list_files 工具加 max_depth 参数（max_depth=1 只列顶层）。改 tools.py + tools_schema.py，加单测。"

| 维度 | subagent (Opus) | yansh (Sonnet) |
|---|---|---|
| 耗时 | 99s | 242s |
| 工具调用 | 16 | 39 |
| 改动文件数 | 3 | 4 |
| 实现是否正确 | ✅ 对 | ❌ off-by-one |
| 单测是否通过 | ✅ | ❌ 自己写的测试和实现矛盾 |
| **agent.py dispatch 是否修** | ❌ **漏掉** | ✅ 修了（review loop 追到的） |
| 任务判定 | 完成 | 失败 |

**模型不一致** 是这次仍然存在的瑕疵——Opus 比 Sonnet 强一档，差距里有多少是模型能力、有多少是架构/prompt，分不清。下一轮要用 sonnet subagent 重跑。

## 真正有意思的发现：双方各漏一个 bug，方向相反

**subagent (Opus) 写出了正确的 list_files**——直接按用户原话翻译成简单 filter，逻辑稳。  
**但 subagent 漏了 agent.py 的 dispatch**——`list_files` 在 dispatch 里被硬编码不传参数，所以 LLM 用工具时 max_depth 还是被忽略（虽然单测过了，因为单测直接调函数绕过 dispatch）。

**yansh (Sonnet) 实现写错了**——剪枝逻辑 off-by-one，max_depth=1 实际返回顶层+一级子目录。  
**但 yansh 找到了 dispatch bug**——靠 review/fix loop 多轮迭代追到的。

这个不对称暴露了一个深的问题：

### 洞察：**subagent 缺乏"全链路意识"**

人工程师改一个函数签名后会立刻 grep 所有调用点。subagent 没有这个习惯——它严格按任务措辞干（"改 tools.py + tools_schema.py + 加测试"），不主动越界检查。

yansh 的 review/fix loop 是个**很丑的"全链路意识"补偿机制**——它没有"主动审视全链路"的智能，但它有"任务失败就反复重试"的笨办法，反而捞到了。

→ ROADMAP P0 #2 新增条款（针对 _CODER_ROLE）：
> 修改函数签名（增删参数）时，必须 grep 所有调用点，确认新参数能正确传递；不能只改函数本身和 schema 就停手。

这条规则的本质是教 LLM **拒绝把任务措辞当作严格边界**——任务说改 X，但要确认 X 的所有 user 都跟得上。

## yansh 的"全链路意识"反思

前面两轮笔记里，我把 yansh 的"找到 dispatch bug"当作 yansh 的优点。这次重新看，发现：

**yansh 不是"主动审视全链路"，是"出 bug 后死循环里碰到的"**。

如果 yansh 的实现第一遍就对了，它根本不会再看 agent.py。所以这个"优点"其实是 review/fix 架构的副产品，不是 yansh 设计的目的。

→ 这个反思也调整了我对 reviewer 架构的看法。**之前砍掉 reviewer 时，我说"减法的胜利"——现在意识到 reviewer 的死循环里有时会捞到额外信息**。砍掉 reviewer 后，yansh 的 fix-test loop 里如果第一遍跑通就不会再迭代，少了"碰运气找 bug"的机会。

但这不构成保留 reviewer 的理由——**主动审视全链路**应该靠 prompt 教 coder 干，不靠"出错+循环"的运气机制。Claude Code 的 subagent 也没干这件事，说明 Anthropic 的 prompt 里也没专门教这个。**这是 LLM agent 共性的盲区**。

## 总结性思考

1. 设计对照实验时，**永远不要让 LLM 帮你测它自己**——它知道答案
2. 做 A/B 测试要起 subagent 冷启动；模型也要对齐
3. 单次测试看不出全部——双方各漏一个 bug，靠**多任务覆盖率**才能看清谁更全
4. yansh 的 review/fix 是丑陋的补偿机制，能捞到 bug 但效率极低；正解是 prompt 教会 coder 主动审视全链路
5. 公平测试本身比测试结果更值得记录——这种"我做错了什么"的反思才是 shadow 笔记的真正价值

## 第二次公平测试：Sonnet subagent vs yansh（同模型）

为了排除模型差距，重跑 subagent 时显式指定 `model: sonnet`。

### 三方完整对比

| 维度 | Sonnet subagent | Opus subagent | yansh (Sonnet, mode=code) |
|---|---|---|---|
| 架构 | 单 agent | 单 agent | plan/code/test 多阶段 |
| 耗时 | 134s | 99s | 242s |
| 工具调用 | 23 | 16 | 39 |
| 改动文件数 | 3 | 3 | 4 |
| 实现是否正确 | ✅ | ✅ | ❌ off-by-one |
| 单测是否通过 | ✅ | ✅ | ❌ 实现/测试矛盾 |
| **agent.py dispatch 是否修** | ❌ 漏 | ❌ 漏 | ✅ 修（fix loop 副产品） |
| 任务判定 | 完成 | 完成 | 失败 |

### 排除模型差距后的真·结论

1. **同模型（Sonnet）下，yansh 比 subagent 慢 1.8x、多 70% 工具调用、还做错**
   - 慢 1.8x 是**架构差距**（plan/code/test 多阶段串行 vs 单 agent）
   - 多工具调用是**协议差距**（角色间传递必须重新读取上下文）
   - 实现错误是 **plan→code 上下文割裂**——不是 Sonnet 模型不行（subagent 同模型做对了）

2. **dispatch 漏修是所有 LLM agent 的共性盲区**
   - Opus subagent 漏 → Sonnet subagent 漏 → 不是模型能力问题
   - 是**全链路意识盲区**：LLM agent 普遍严格执行任务措辞，不主动越界检查
   - **Anthropic 的 Claude Code subagent 也没解决这个**

3. **yansh 的"找到 dispatch"是补偿机制的副产品**
   - 不是主动审视全链路，是因为 impl 错了，fix loop 反复跑追到的
   - 多花 100+ 秒最后判失败，效率极低
   - 但**确实捕获了 subagent 错过的问题**

4. **Sonnet subagent 的"踩坑自查"展示单 agent 优势**
   - subagent 自报："第一次 `>= max_depth` 写错了，自己测出问题，改成 `>= max_depth - 1`"
   - **自我验证回路在同一个上下文里发生**——这是单 agent 设计的核心优势
   - yansh 的 mode=code 没有这种自查（它的"测试"是被自己污染的）

### 之前的认知 vs 数据告诉我的

| 之前我以为 | 数据告诉我 |
|---|---|
| Claude Code 比 yansh 快 9 倍 | **同模型下单 agent 比多 agent 快约 2 倍**——9 倍是开卷选手作弊数据 |
| reviewer 是 yansh 的核心问题 | reviewer 已删但**架构差距还在**——plan/code/test 多阶段也比单 agent 慢 |
| yansh 的"全链路意识"是优点 | 那是 fix loop 副产品；**Claude Code 同样没主动审视全链路** |
| 模型能力是关键变量 | **架构 + prompt > 模型**——同 Sonnet 下 yansh 慢且错，subagent 快且对 |

### 给 ROADMAP 的优先级调整

| ROADMAP 项 | 调整 |
|---|---|
| P0 #2 prompt 调优 | **拔高第一条**："修改函数签名时必须 grep 所有调用点"——连 Claude Code subagent 也没解决的共性盲区，yansh 做对了反而能领先 |
| P0 #3 错误恢复闭环 | **降低优先级**——subagent 不需要这个机制也跑得很好。把"主动审视全链路"和"减少 plan/code 协议割裂"做好后，错误恢复的需求会大幅减少 |

新洞察：**架构升级（单 agent 化 + prompt 教全链路意识）**比错误恢复闭环 ROI 高得多。

---

## 后续

- 把"修改函数签名时必须 grep 所有调用点"作为下一条 prompt 改进，加到 _ARCHITECT_ROLE 和 _CODER_ROLE
- 新做对照实验**默认用 subagent，永远显式指定模型**——这次踩的坑不要再踩
- ROADMAP P0 #3（错误恢复闭环）优先级降低，可以后做
