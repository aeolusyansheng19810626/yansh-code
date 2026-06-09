---
name: solo-mode-lessons-learned
description: yansh solo mode R1-R19 全实验经验总结（架构>模型、规格>能力、有效/无效优化、设计原则）
metadata:
  type: project
---

# yansh-code Solo Mode 实验经验总结

## 核心结论

1. **架构 > 模型**：逐文件 context 割裂（R1–R9 全 0/10）是 miniQL 失败的根因，而非 LLM 代码能力；改用单一连续 context（solo mode）后，sonnet 0→4/10、opus 0→6/10，双模型独立验证。
2. **规格 > 能力**：R12 sonnet solo 4/10 → R13 强化 PROMPT 后 9/10（实际 10/10），4 个失败全因契约模糊（分隔线规则自相矛盾、限定名表头定义缺失），实锤「规格表达问题，非能力问题」。
3. **成本主因是模型单价，架构开销恒定**：solo vs 逐文件同模型约 2.3× 开销（ICA 无 prompt cache），sonnet+solo $13/跑，opus+solo $49/跑，约 6× 差距来自模型单价，与架构无关。
4. **symbol_contract 是 context 割裂的补丁**：R1–R5 打地鼠（import 名→枚举成员→方法名→方法 arity→函数 arity），每轮堵一个漏洞，R6 端到端 smoke test 才治本，solo 下这套机制根本不需要。
5. **框架不应依赖 LLM 自维护元信息**：凡框架可确定性获取的元信息（命令 exit code、文件写入历史），都不应靠 LLM 主动记录——框架自动维护状态文件（R17 首跑建立、R18 跨 run 注入）将探路从 28 轮降至 7 轮。

---

## R 系列完整数据表

| Run | mode | model | input tokens | cost | 黑盒 | 关键特征 |
|-----|------|-------|-------------|------|------|---------|
| mini-R1 | code | sonnet | 753K | $2.90 | 0/9 | 6 处跨模块 import 名不对齐，fix 3/3 耗尽 |
| mini-R2 | code | sonnet | — | $7.45 | 0/9 | import 名修后，运行时 TokenType.IDENT 成员名错 |
| mini-R3 | code | sonnet | — | $6.60 | 9/9 ✅ | 成员级 symbol_contract + fix 方向约束，fix 2 轮收敛 |
| miniQL-R1 | code | sonnet | 1,550K | $6.04 | 0/10 | import 自洽，首轮 150 pass，1↔82 震荡，evaluator.eval 方法名不对齐 |
| miniQL-R2 | code | sonnet | — | $9.57 | 0/10 | analyze 函数名/RuntimeError\_ 类名 import 不对齐 |
| miniQL-R3 | code | sonnet | 1,500K | $5.79 | 0/10 | architect 跳过 symbol_contract，load_csv 参数数量不对齐 |
| miniQL-R4 | code | sonnet | 8,820K | $27.80 | 0/10 | 强制 symbol_contract 生效，register_csv 参数数量不对齐，fix 6/6 耗尽 |
| miniQL-R5 | code | sonnet | 9,830K | $30.71 | 1/10 | build_logical_plan 裸函数 arity（仅覆盖 obj.method 形式） |
| miniQL-R6 | code | sonnet | 16,770K | $51.31 | 0/10 | 端到端 smoke test 首次打通接口层，暴露 tuple-as-key 业务逻辑 bug，$50 熔断中断 |
| miniQL-R7 | code | sonnet | 3,070K | $9.90 | 0/10 | fix loop compact 接入（未实际触发），build_logical_plan 91↔1 arity 震荡 |
| miniQL-R8 | code | sonnet | 5,900K | $17.01 | 0/10 | 裸函数 arity 检测（依赖 contract params，实际 0 次触发），Token.isdigit 逻辑 bug |
| miniQL-R9 | code | opus-4.8 | 1,080K | $21.00 | 0/10 | max_tokens 缺省截断 plan、no_progress 误熔断、executor 未落地（架构不适配 opus 探索模式） |
| miniQL-R10 | solo | opus-4.8 | — | ~$49 | 6/10 | solo 首跑，连续 context 天然跨文件一致，框架 bug 中断（Path/no_progress） |
| miniQL-R11 | solo | opus-4.8 | 3,000K | $48.88 | 6/10 | 修框架 bug 后，60 gate 测试绿，4 失败确定性（契约模糊+EXPLAIN oracle bug） |
| miniQL-R12 | solo | sonnet | 2,400K | $8.06 | 4/10 | solo+sonnet 验证：$8 拿 4/10，错误鲁棒性/节点名精度弱于 opus |
| miniQL-R13 | solo | sonnet | 5,270K | $16.80 | 9/10（实 10/10） | 强化 PROMPT（4 规格点）后 sonnet 全做对，唯一 case8 是 oracle 自身 bug |
| miniQL-R14 | solo | sonnet | 4,080K | $13.20 | 10/10 | 三项降本优化（截断+pytest-q+compact 40K），-22% input token，质量持平 |
| miniQL-R15 | solo | sonnet | 4,670K | $15.70 | 4/10 | summarizer⑥ 验证（探路 0 轮），但本跑实现质量波动导致 4/10 |
| miniQL-R16 | solo | sonnet | 4,440K | $14.30 | 9/10 | 状态文件 role 指令失效（agent 从未创建），框架注入也因文件不存在而跳过 |
| miniQL-R17 | solo | sonnet | 4,360K | $14.09 | 9/10 | 框架自动维护状态文件首跑，28 轮探路（无先验），状态文件成功创建（189 行） |
| miniQL-R18 | solo | sonnet | 4,930K | $15.84 | 10/10 | 状态文件跨 run 注入，探路降至 7 轮，自测 158/158，miniQL 首次功能满分 |
| miniQL-R19 | solo | sonnet | 4,450K | $14.13 | 3/10 | import 诊断注入使 agent 修实现 API 而非重写测试，120 轮未完成，已回滚 |

---

## 架构发现：逐文件 vs solo

### 逐文件为何 0/10？

**根因：每个文件重建独立 messages，coder 看不到自己已写的真实代码。**

- coder 写 executor.py 时，只有 architect 生成的 symbol_contract 描述 logical_plan 的接口——没有真实代码
- architect 把符号名写成自然语言（`build_logical_plan(stmt, catalog)`），但 coder 可能实现成 1 参
- symbol_contract 覆盖面不断扩展（import 名→枚举成员→方法名→方法 arity→函数 arity），R1–R5 每轮打一个地鼠，永远追不完
- fixer 修复时也是独立 context，看不到全局，引发震荡（改 A 修好→改 B 又破 A）

### solo 如何解决？

- 整个任务在单一连续 messages 中运行
- agent 写完 logical_plan.py 后，再写 executor.py 时可直接 read 自己刚写的真实代码确认签名
- 跨文件一致性天然成立，无需任何静态对齐扫描
- agent 可自产"活的 symbol_contract"（存在连续 context 里），随时更新

### trade-off

| 维度 | 逐文件 code | solo |
|------|------------|------|
| 简单单文件任务成本 | 低 | 略高（~$0.4-0.8 差） |
| 复杂多文件任务 | 0/10（接口死结） | 4-10/10 |
| ICA 无 cache 的影响 | 较小（各 session 独立） | 较大（长链全额重发） |
| 框架复杂度 | 高（symbol_contract/scan_* 等） | 低（连续 context 自然解决） |
| 当前默认 | 已改为 solo | — |

---

## Solo Mode 有效优化（已验证，按 ROI 排序）

### 1. execute_command 输出头尾截断
- **位置**：`tools.py`，新增 `_truncate_cmd_output()`
- **机制**：超 6000 chars 保留头尾各 3000，中段加 `[... N chars truncated ...]` 标记
- **效果**：pytest PASSED 中段行不再每轮重发；R14 vs R13 三项合计 -22% input token（$16.8→$13.2）

### 2. `_SOLO_ROLE` 加 pytest `-q` 指令
- **位置**：`agent.py _SOLO_ROLE` 自测段
- **机制**：role 指令引导 agent 默认用 `-q`，从源头压缩 pytest 输出量
- **效果**：R14 agent 确实使用 `-q`，是三项合计 -22% 的组成部分

### 3. compact threshold 60K → 40K
- **位置**：`agent.py _make_compact_state`
- **机制**：更早触发压缩，降低 O(N²) context 累积常数项
- **效果**：R14 第 49 轮触发，40K→2.5K（94% 压缩），降低后续峰值

### 4. `_SUMMARIZE_SYSTEM` 强制项⑥（环境命令钉定）
- **位置**：`agent.py _SUMMARIZE_SYSTEM`
- **机制**：要求 summarizer 逐字保留已验证 shell 命令原文（`py -3.11 -X utf8 -m pytest`），不可泛化省略；摘要上限 800→900 字
- **效果**：R15 四次 compact 全程零重新探路（R14 round 88-104 的 ~15 轮浪费消失）

### 5. 框架自动维护状态文件（execute_command dispatch 层）
- **位置**：`tools.py execute_command` dispatch 层 + `solo()` 启动注入 + compact 时注入
- **机制**：命令首次成功自动追加白名单，非零退出追加黑名单；solo 启动时若文件存在则注入 system prompt；过滤多行/超 160 char 命令
- **效果**：R18 vs R17 探路从 28 轮降至 7 轮，首用 `py -3.11` 从轮 24 提前至轮 12，miniQL 首次 158/158 自测 + 10/10 黑盒

---

## 无效或有害的尝试（含根因）

### 1. symbol_contract 逐层扩展（R1→R8 打地鼠）
- **尝试**：import 名→枚举成员→方法名→arity，每轮加一层静态检测
- **实际**：R1–R5 每轮总有新的不对齐漏网；R6 smoke test 才治本；solo 下整套机制不再需要
- **根因**：symbol_contract 是 context 割裂的补丁，接口种类无穷无尽，只要 context 割裂就有下一个漏洞

### 2. keep_recent_pairs 调大应对 compact 退化
- **尝试**：compact `keep_recent_pairs` 从 2 增大
- **实际**：只推迟失效点，第 N 次 compact 仍会丢；token 代价 +500-800K，性价比可能为负
- **根因**：问题在"多次 compact 的摘要逐代退化"，不在"最近一对被切掉"；keep 治错了问题

### 3. role 指令让 agent 主动维护状态文件（R16）
- **尝试**：`_SOLO_ROLE` 写"发现有效命令时立即写入 `.yansh/agent_state.md`"
- **实际**：R16 状态文件从未被创建；框架 compact 注入也因文件不存在而跳过
- **根因**：奖励结构错配（写元信息零即时收益）+ 内省触发不可靠 + 无负反馈 + 可选措辞自我豁免

### 4. coder_no_progress_rounds 调高适配 opus（R9）
- **尝试**：`coder_no_progress_rounds` 4→8 给 opus 更多探索空间
- **实际**：executor 仍 8 轮熔断，调高无效
- **根因**：真正卡点是"强制 write_file 不限定 path"（opus 第一轮被迫写 CHECK.txt）+ read 缓存让探索拿不到新信息

### 5. solo 接入 `_scan_import_mismatches` 诊断注入（R19，已回滚）
- **尝试**：pytest ImportError 时自动调 `_scan_import_mismatches`，注入所有缺失导入名
- **预期**：把 35 轮逐个反查 API 压成 2-3 轮，节省 ~$2-3
- **实际**：R19 黑盒 3/10（vs R18 10/10），大幅退步
- **根因**：该功能仅适合"实现可完全控制 + 测试由 agent 自己写"场景；miniQL 预置了测试文件，R18 靠**重写测试文件**（正确策略）拿 10/10；import 诊断阻断这条路，转而走**改实现 API**（错误策略），23 个缺口 120 轮修不完，留下中间状态

### 6. symbol_contract 扩展到方法参数签名（R3 / R8）
- **尝试**：methods 字段扩展为含 params 的 dict；扫描扩展到 arity
- **实际**：R8 arity 检测 0 次触发——architect 只在 description 写自然语言签名，未用结构化 params 字段
- **根因**：schema 把 params 设成"建议"，architect 不采纳；真值源应是函数定义本身（AST），而非 architect 声明

---

## 框架设计原则（可复用）

1. **连续 context 优于静态契约**：多文件协作任务让 agent 在单一连续 messages 内工作，跨文件一致性天然成立；任何静态对齐扫描都是 context 割裂的补丁，非终态方案。

2. **框架不应依赖 LLM 自维护元信息**：凡框架可确定性获取的元信息（exit code、写入路径、成功/失败），必须由框架自动记录，而非 role 指令——LLM 奖励结构导致它总跳过无即时收益的维护动作。

3. **独立 oracle 不可被自测替代**：agent 自写测试会编码与实现相同的误读（R11/R12 gate 60 测试全绿，黑盒仍 4 红）；关键质量门必须用独立黑盒验收。

4. **fix loop 的 compact 是必要安全网**：fix loop 每轮 append messages、全量重发，无 compact 会 O(N²) 爆炸（R6 $51 中 92% 烧在 input）；未触发时无害，触发时救命。

5. **端到端真实入口 > 内部单测**：smoke test 用 subprocess 跑真实 CLI（R6 引入），一次打通 R1–R5 五轮 symbol_contract 扫描未解决的接口层死结；fixer 看到真实失败信号才能有效工作。

6. **规格模糊是首要排查项**：失败前先验证 oracle 自身是否正确（case8 oracle bug）、契约是否自洽（分隔线两例子矛盾）；规格盲区能让满分实现的产物过不了验收。

7. **框架参数要覆盖目标模型的工作模式**：`max_tokens`/`no_progress` 等按 sonnet 调优的参数对 opus"先探索后下笔"模式不适配（R9）；调参要针对实际工作模式而非假设行为。

8. **新框架特性要在 benchmark 特性充分已知后再优化**：R19 失败因没充分理解 benchmark（预置测试 + R18 靠重写测试成功）就盲目接入诊断功能；每次实验前先验证"新增功能与 benchmark 场景是否兼容"。

---

## 待解决问题（含已知根因）

1. **solo 探路浪费（R18 仍 7 轮）**
   - 根因：工作目录绝对路径未被记录（非 python 命令，被内容过滤排除）
   - 方案：框架在 solo 启动时自动把 workspace 绝对路径写入状态文件首行

2. **solo import error 仍需多轮反查 API（R18 轮 48–82 共 35 轮）**
   - 根因：`_scan_import_mismatches` 只在 `fix()` 路径触发；但 R19 证明 solo 接入需谨慎（预置测试场景下有害）
   - 方案：仅在"agent 自写测试 + 未检测到预置测试文件"时触发，需先实现场景识别

3. **gate 回灌 stderr 在 O(N²) 尾部累积**
   - 现状：gate 回灌 `[-4000:]` stderr，3 次失败 = 12K 字常驻最贵位置
   - 方案：改为"失败用例名+单行错因"摘要（~$0.5-1），无需场景识别，安全可实施（R19 一并回滚，可单独重做）

4. **compact 仍有概率退化（summarizer⑥ 是概率性保障）**
   - 根因：summarizer 是 LLM，未来仍可能遗漏
   - 方案：完成状态文件完整覆盖（工作目录 + 更多环境事实），实现 100% 确定性恢复

---

## 明确不做的事（含理由）

1. **不退役逐文件 `code()` / symbol_contract / `_scan_*`**：简单单文件任务逐文件成本更低；solo 已设默认，两套并存按需路由。

2. **不针对业务逻辑 bug 加静态检测**：每轮 LLM 生成轨迹不同，逻辑 bug 落在随机位置；这类 bug 需模型真正理解语义，加检测是更深层打地鼠。

3. **不为每跑 $49 的 opus 冲高分**：solo+sonnet 强化 PROMPT $13.2 可稳定 10/10（R14）；opus 多花 $35 仅在「无强化 PROMPT」场景有边际优势。

4. **不修 ICA 不透传 prompt cache**：ICA 网关结构性限制，yansh 不可控；作为成本基线接受。

5. **不把 PROMPT 规格点写成对应答案**：R13 强化的是"合理规格澄清"（分隔线列宽公式、限定名表头规则），不是贴答案；规格必须独立于实现。

6. **solo 不接入 `_scan_import_mismatches`（当前）**：R19 证明预置测试 benchmark 场景下有害（3/10 vs R18 10/10）；需先实现"有无预置测试"场景识别再考虑有条件接入。

---

## 相关

- 全程笔记：`2026-06-07_02` ~ `2026-06-08_04`（mini/miniQL R1-R19）
- memory：[[project_solo_mode]]
- 成本诊断：`2026-06-07_10-cost-diagnosis`
