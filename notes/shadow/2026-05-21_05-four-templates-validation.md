# 2026-05-21 4 类任务模板 prompt 验证（dbc25e2）

## 实验设计

`_CODER_ROLE` 升级为 4 个具体场景模板（commit `dbc25e2`），替代之前那条单一的"全链路意识"规则。
4 个模板分别针对：

1. **签名变更**：dispatch 表 + 文档 + 调用方
2. **新增工具**：tools.py 实现 + tools_schema.py + agent.py dispatch + readonly_handlers
3. **递归剪枝**：禁用 `dirs.clear() + 进入循环再判断`，要求"先剪枝再枚举"
4. **范围克制**：只改任务描述的功能，禁止顺手重构

通过两个任务（A: list_files 加 max_depth；B: 新增 count_lines 工具）覆盖这 4 个模板，
yansh 与同模型 Sonnet subagent 分别跑一遍做对照。

## 结果矩阵

### Task A: list_files 加 max_depth 参数

| 维度 | yansh (Sonnet, dbc25e2) | Sonnet subagent baseline |
|---|---|---|
| 耗时 | 272s | 144s |
| 工具调用 | 多轮（3 次失败重试） | 19 |
| 模板 1（dispatch）| ✅ `agent.py:864` 改为 `list_files(**args)` | ❌ 没改（subagent 测试不走 dispatch） |
| 模板 3（剪枝）| ✅ `dirs.clear() + continue` 在枚举文件**之前** | ✅ 用了 `dirs.clear()` 模式 |
| 模板 4（克制）| ❌ **scope creep**：删了 `_DANGEROUS_PATTERNS` 的 `python -c`，重写 `_validate_path` 错误文案 | — |
| 任务判定 | 失败（非 max_depth 测试挂了，因为顺手改了别的） | 完成 |

### Task B: 新增 count_lines 工具

| 维度 | yansh (Sonnet, dbc25e2) | Sonnet subagent baseline |
|---|---|---|
| 工具调用 | 多轮 | 14 |
| 耗时 | ~3 min | ~3 min |
| 模板 2 三件套 | ✅ tools.py + tools_schema.py + agent.py（import + readonly_handlers）| ✅ 三处全中 |
| 模板 4（克制）| ✅ 复用 `_validate_path`，没顺手改其他文件 | ✅ 报告了 5 个 pre-existing 失败但**没去修** |
| 任务判定 | 完成 | 完成 |

## 关键观察

### 4 个模板里 3 个生效，1 个被违反

- **模板 2（新工具三件套）大成功**：yansh 在 task B 里直接命中 tools.py / tools_schema.py / agent.py
  的 import + readonly_handlers 4 个点位，零遗漏。这是**首次** yansh 在新增工具任务上和
  subagent 平起平坐。
- **模板 1（dispatch）继续生效**：yansh 改 list_files 签名后主动改了 `agent.py:864`，
  subagent 反而没改（因为他们的测试直接 import tools，不走 dispatch；这暴露 subagent
  测试设计的局限）。
- **模板 3（剪枝顺序）生效**：yansh 这次写的是 `if depth >= max_depth: dirs.clear(); continue`
  在 `for f in files` **之前**，没再栽 off-by-one。
- **模板 4（克制）失败**：yansh 在 task A 里**仍然顺手改了** `_DANGEROUS_PATTERNS`
  和 `_validate_path` 的错误文案——这两块跟 max_depth 完全无关。

### 模板 4 失败的诱因

Task A 的工作树本来就有 5 个 pre-existing 失败用例（`test_execute_command_timeout`、
3 个 path-traversal、1 个 diff truncation）。yansh 的 review/fix loop 看到红，
**误以为是自己引入的**，于是改 `_DANGEROUS_PATTERNS` 和错误文案"修复"它们。

模板 4 的当前措辞还不够强——它说"只改任务描述的功能"，但没说**"看到非自己引入的失败时
要识别出来并停手"**。这是 prompt 还需要补的一句反向警告。

## 对比上一轮（cca5d03 → dbc25e2）

| 任务 | cca5d03（单一全链路规则）| dbc25e2（4 模板）|
|---|---|---|
| dispatch 修复 | ✅ | ✅ |
| max_depth 实现正确性 | ❌ off-by-one 又栽 | ✅ 这次对了 |
| 顺手改不相关代码 | ✅ 改了路径分隔符 | ✅ 改了 `_DANGEROUS_PATTERNS` |
| 新工具三件套 | 未测试 | ✅ task B 全中 |

净进步 +1：**模板 3（剪枝）解决了 off-by-one**。
净退步 0，但**模板 4（克制）仍未真正生效**，违反方式从"改路径分隔符"换成"改 dangerous patterns"。

## 后续

1. **加强模板 4**：在 `_CODER_ROLE` 里补一句 —— "发现失败用例时先确认它是不是这次任务的
   功能产生的；如果跟当前任务无关，**记录并跳过**，不要试图修复"。
2. **不需再加新模板**：4 模板覆盖了大部分高频陷阱，再加会让 prompt 变冗长。
3. 这两块都是 P0 #2 子任务延续。

## 一句话总结

**4 模板 prompt 让 yansh 在"新增工具三件套"和"递归剪枝顺序"这两项上达到 subagent 水平，
但"范围克制"还没真正学会——它仍然会把 pre-existing 失败误当成自己引入的而出手修复。**
