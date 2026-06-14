# MCP Exp3 实验结果记录

## 实验信息

- 目录：`C:\Users\ShengYan\Projects\AB-test\mcp-exp3-tdd`
- 场景：strkit 字符串库（4模块15函数），全 stub 无测试，TDD 模式
- 模型：sonnet-4-6（Exp2 是 haiku，本次路由到 sonnet，费用差距 15x）
- 耗时：197.96s
- 费用：$3.65
- 轮数：27 轮
- 验收：**5/5 ✅**

## 实验结果

### 验收得分

| 检查项 | 结果 |
|--------|------|
| pytest 全绿（105/105） | ✅ |
| 测试文件存在且非空（4个） | ✅ |
| 测试在原始 stub 上全失败（TDD验证） | ✅ 105 fail |
| 实现已写完（无 NotImplementedError） | ✅ |
| pytest MCP 调用 ≥2 次（实际 6 次） | ✅ |

### Tool 使用分布

| 工具 | 调用次数 | 含义 |
|------|---------|------|
| `mcp__filesystem__read_multiple_files` | 1 | 批量读 stub 规格 |
| `mcp__filesystem__write_file` | 4 | 写 4 个测试文件（新建） |
| `mcp__pytest__run_tests` | 6 | 动态验证 |
| `mcp__filesystem__edit_file` | 15 | 修改实现文件（逐函数） |
| `task_complete` | 1 | 完成信号 |

### TDD 顺序验证

`files_modified` 字段清晰反映了 TDD 顺序：
1. 先写 4 个测试文件：`test_casing.py`、`test_textstats.py`、`test_brackets.py`、`test_transform.py`
2. 再写 4 个实现文件：`casing.py`、`textstats.py`、`brackets.py`、`transform.py`

**Agent 真正做了 TDD**（先红→后绿），files_modified 顺序是证据。

### pytest MCP 使用行为

- 第 1 轮：`read_multiple_files` 批量读全部 stub 规格
- 第 2-5 轮：`write_file` 写 4 个测试文件（105 个用例）
- 第 6 轮：`run_tests` → 105 failed（红色起点，确认测试在 stub 上全失败）
- 第 7-25 轮：`edit_file` 逐函数实现（每轮 1 个）
- 第 26-27 轮：`run_tests` × 多次确认全绿 → `task_complete`

## 与 Exp1/Exp2 对比

| 维度 | Exp1（filesystem 静态修复） | Exp2（filesystem + pytest 动态修复） | Exp3（TDD） |
|------|--------------------------|-----------------------------------|----|
| 验收 | 4/4 | 5/5 | 5/5 |
| 耗时 | 66s | 51s | 198s |
| 费用 | $0.43 | $0.24 | $3.65 |
| 轮数 | 19 | 10 | 27 |
| 模型 | haiku-4-5 | haiku-4-5 | sonnet-4-6 |
| pytest MCP | 无 | 2 次 | 6 次 |
| 测试数 | N/A（只改实现） | 17（预设） | 105（agent 自写） |
| TDD 顺序 | N/A | N/A | ✅ 先测后实现 |

Exp3 费用高主要因为模型（sonnet vs haiku），不是任务本身。

## 关键观察

1. **Agent 自然遵循 TDD**：prompt 明确要求 TDD 顺序，agent 完全遵守——先批量读规格，先写全部测试，确认失败后再实现。没有"跳步"行为。

2. **105 个测试是 agent 自主设计的**：规格里只有函数说明，测试覆盖度（含边界 case）由 agent 自主判断，质量高。

3. **`read_multiple_files` 效率**：agent 用一次 bulk read 读了所有 4 个 stub，比 Exp2 逐个 read 更高效。

4. **`files_modified` 的绝对路径问题**（P3 非阻塞）：MCP 写操作登记的是绝对路径，accept.py 不依赖此字段，不影响验收。

5. **模型差异**：Exp2 用 haiku（$0.24），Exp3 用 sonnet（$3.65）。如果 Exp3 用 haiku，费用可能在 $0.5 以内。

## 实验设计分工

- opus：设计 strkit 场景（4 模块 × 3-5 函数，stub + docstring）+ 验收脚本
- sonnet：恢复 stub、配 mcp.json、写 task.md、运行实验
- 无需 review（实验设计类，非 yansh 系统修改）
