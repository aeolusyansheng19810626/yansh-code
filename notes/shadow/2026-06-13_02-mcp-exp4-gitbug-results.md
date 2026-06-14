# MCP Exp4 实验结果记录

## 实验信息

- 目录：`C:\Users\ShengYan\Projects\AB-test\mcp-exp4-gitbug`
- 场景：numkit 数值库，git 历史 8 commits，其中 `6a28357` 引入 regression（`_round_boundary` 从 round-half-to-even 改成 round-half-up），要求 agent 用 git 历史定位 regression commit 再修复
- 模型：sonnet-4-6
- 耗时：61.25s
- 费用：$0.61
- 轮数：11 轮
- 验收：**5/5 ✅**

## 实验结果

### 验收得分

| 检查项 | 结果 |
|--------|------|
| pytest 全绿（47/47） | ✅ |
| 测试文件未被修改 | ✅ |
| bug fix 静态校验（恢复 floor % 2 偶数判断） | ✅ |
| git MCP 调用 ≥2 次（实际 2 次） | ✅ |
| 改动只在 numkit/ 下（只改 rounding.py） | ✅ |

### Tool 使用分布

| 工具 | 调用次数 | 含义 |
|------|---------|------|
| `mcp__pytest__run_tests` | 3 | 初始红 + 验证绿 × 2 |
| `mcp__filesystem__list_directory` | 1 | 探索目录结构 |
| `mcp__filesystem__list_allowed_directories` | 1 | 了解 MCP 根 |
| `mcp__filesystem__directory_tree` | 1 | 看树形结构 |
| `mcp__git__git_log` | 1 | 查 commit 历史 |
| `mcp__git__git_show` | 1 | 查 regression commit diff |
| `mcp__filesystem__read_text_file` | 1 | 读 rounding.py |
| `mcp__filesystem__edit_file` | 1 | 修复 _round_boundary |
| `task_complete` | 1 | 完成信号 |

### Agent 根因分析流程

1. 先跑 pytest，确认 7 个用例失败（边界舍入）
2. 浏览目录结构（`list_directory`, `directory_tree`）
3. `git_log` → 看到 `6a28357 refactor(rounding): simplify boundary branch`，判断可疑
4. `git_show 6a28357` → 看到 diff：`if floor % 2 == 0: return floor; return floor + 1` → `return floor + 1`
5. `read_text_file rounding.py` → 确认当前状态
6. `edit_file` 修复 `_round_boundary`：恢复 `if floor % 2 == 0: return floor; return floor + 1`
7. 多次 `run_tests` 确认全绿 → `task_complete`

**Agent 真正用 git 做了根因分析**——读 git_show diff 后才动手改代码，没有猜。

## yansh 报"任务失败"的原因（非实验问题）

yansh 内部 gate 逻辑判定 `coverage_unknown`（非 targeted coverage）：
- Modified file：`project/numkit/rounding.py`（绝对路径）
- Test file：`project/tests/test_rounding.py`（未在 `files_modified` 中）
- Scope 推断找不到"针对性测试文件"（测试是预存的，不在本次改动集合里）
- Gate 跑全量测试通过但标 `coverage_unknown`，最终 `final_success = agent_completed AND gate_status == "passed"` = False

**这是 yansh 对 MCP+子目录 场景的 scope 推断局限**，与实验正确性无关。accept.py 5/5 确认修复正确。此问题类似 Exp2 的 snapshot bug，可记入技术负债观测。

## 与 Exp1~3 对比

| 维度 | Exp1（静态修复） | Exp2（动态修复） | Exp3（TDD） | Exp4（git 根因） |
|------|----------------|----------------|------------|----------------|
| 验收 | 4/4 | 5/5 | 5/5 | 5/5 |
| 耗时 | 66s | 51s | 198s | 61s |
| 费用 | $0.43 | $0.24 | $3.65 | $0.61 |
| 轮数 | 19 | 10 | 27 | 11 |
| 模型 | haiku | haiku | sonnet | sonnet |
| 新增 MCP | — | pytest | pytest | git+pytest |
| 关键行为 | 静态分析改 | 测试驱动迭代 | TDD 先写测试 | git 历史根因分析 |

Exp4 效率高（11 轮，$0.61）：agent 用 git_log + git_show 快速定位 regression commit，只改 1 个函数，3 次 pytest 确认（红→绿）。

## 实验设计分工

- opus：设计 numkit 场景 + git 历史（7 commits，regression 在第 5 个）+ accept.py
- sonnet：创建 git MCP server（`mcp_server_git.py`，4 工具：git_log/git_show/git_diff/git_blame）、配置 .yansh/mcp.json、修复 task.md git 追踪问题

## 新发现：yansh coverage_unknown 局限

**现象**：MCP 任务只改实现文件（预存测试），yansh gate 判 `coverage_unknown`，报"任务失败"，但实际测试全绿。

**根因**：`final_success = agent_completed AND gate_status == "passed"`，而 scope 推断找不到"实现文件与测试文件的关联"（测试不在 `files_modified`），全量通过仅标 `coverage_unknown` 而非 `passed`。

**修法方向**：当全量测试绿且 task_complete(success=True) 时，对 MCP-only 任务放宽判定（`coverage_unknown` 且 agent 成功 → 也算 passed）。P3 观测项，不阻塞实验。
