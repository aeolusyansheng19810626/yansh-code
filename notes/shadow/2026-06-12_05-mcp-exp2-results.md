# MCP Exp2 实验结果记录

## 实验信息

- 目录：`C:\Users\ShengYan\Projects\AB-test\mcp-exp2-dynfix`
- 场景：statkit 数值统计库，7 个 bug，动态修复（Shell MCP + filesystem MCP）
- 模型：haiku-4-5（model=sonnet 路由到 haiku，同 Exp1，原因待查）
- 耗时：51s，$0.24
- 轮数：10 轮
- 验收：**5/5 ✅**

## 实验结果

### 验收得分

| 检查项 | 结果 |
|--------|------|
| pytest 全绿（17/17） | ✅ |
| 测试文件未被修改 | ✅ |
| import 链完整 | ✅ |
| pytest MCP 调用 ≥2 次（实际 2 次） | ✅ |
| 关键 bug 点静态校验（B2/B6/B7） | ✅ |

### Agent 修复的 7 个 bug

| # | 位置 | 修复方式 | 正确性 |
|---|------|---------|--------|
| B1 | outlier.py: import stdev | 加别名 `stdev = std_dev`（而非改 import） | ✅ 合法 |
| B2 | series.py: cumulative_sum | total = data[i] → total += data[i] | ✅ |
| B3 | basic.py: variance | / len(data) → / (len(data)-1) | ✅ |
| B4 | basic.py: median 偶数 | 只取右中位 → 两数均值 | ✅ |
| B5 | series.py: moving_average | data[i:i+window-1] → data[i:i+window] | ✅ |
| B6 | series.py: diff | 可变默认参数 → def diff(data) + 内部 result=[] | ✅ |
| B7 | outlier.py: detect_outliers | > threshold → >= threshold | ✅ |

### MCP 工具使用行为

- 第 1 轮：先用 `mcp__pytest__run_tests` 跑测试（观察失败）
- 第 2-3 轮：`mcp__filesystem__read_text_file` 读源码 + 测试文件
- 第 4-9 轮：`mcp__filesystem__edit_file` 逐一修复（每轮 1 个 bug）
- 第 10 轮：再次 `mcp__pytest__run_tests` 确认全绿 → task_complete

**迭代循环确立：运行测试 → 读源码 → 修复 → 重跑验证，pytest MCP 真实参与了调试流程。**

## 与 Exp1 对比

| 维度 | Exp1（filesystem only） | Exp2（filesystem + pytest MCP） |
|------|------------------------|--------------------------------|
| 验收 | 4/4 | 5/5 |
| 耗时 | 66s | 51s |
| 费用 | $0.43 | $0.24 |
| 轮数 | 19 | 10 |
| 模型 | haiku-4-5 | haiku-4-5 |
| 策略 | 静态分析一遍改完 | 先跑测试再修，更高效 |

Exp2 轮数更少、费用更低——动态验证能力（知道要改什么）比静态分析猜测更高效。

## 发现的 yansh 系统 Bug（新增）

### B3：snapshot 系统不追踪 MCP 写入

**现象**：agent 用 `mcp__filesystem__edit_file` 成功修复 7 个 bug、pytest 17/17 通过，但 yansh 输出"任务失败"并保存 replay。

**根因**：snapshot 系统（`snapshot.py`）基于内置工具的写操作记录 files_modified，MCP 写操作不被纳入 snapshot diff，导致 yansh 认为"无文件修改"→ 判任务失败。

**与 B1（熔断计数不感知 MCP）同根，未修复。**

修法方向：在 `subagent.py` 的 MCP 写追踪（已修 files_modified）基础上，通知 snapshot 系统记录 MCP 写入的文件路径。

## 实验设计过程记录

- opus 负责场景设计（7 bug + 遮蔽链设计）
- 我（sonnet）负责代码实现
- opus review 发现 2 个 P1 数学冲突（test_detect_basic 样本 std 下不可检测 + B7 边界点在样本 std 下落在阈值内侧）
- 修复后端到端验证 17/17 通过
