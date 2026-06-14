# MCP Exp1 实验结果记录

## 实验信息

- 目录：`C:\Users\ShengYan\Projects\AB-test\mcp-exp1-fixtrace`
- 场景：DataKit 依赖链修复（5 个 bug，4 项验收）
- 模型：haiku-4-5（main.py 指定 sonnet，实际路由到 haiku，原因待查）
- 耗时：57s，$0.30
- 结果：**2/4**

## Agent 行为记录

### MCP 工具使用 ✅

Agent 完全遵守了 MCP-only 约束，全程未调用内置 read_file/write_file：

- 读：`mcp__filesystem__read_text_file` × 12、`mcp__filesystem__directory_tree` × 1、`mcp__filesystem__list_allowed_directories` × 1
- 写：`mcp__filesystem__edit_file` × 5

### Bug 发现与修复

| Bug | 位置 | 是否找到 | 修复方式 | 正确性 |
|-----|------|---------|---------|--------|
| BUG 1：cfg["input_field"] | processor.py:18 | ✅ | 改为 cfg["source_field"] | ✅ |
| BUG 2：import DEFAULT_INPUT | validator.py:1 | ✅ | **逆向**：processor 里把 DEFAULT_SOURCE 改为 DEFAULT_INPUT | ⚠️ 合法但触发 check3 |
| BUG 3：参数顺序 | runner.py:8 | ✅ | 改为 validate_record(record, field) + 动态 get_field_name() | ⚠️ mock 失效 |
| BUG 4：测试陷阱 | test_runner.py | ✅ | fixture + mock 改为 source_field | ✅ |
| BUG 5：误导性注释 | processor.py:6 | ✅ | 删除 | ✅ |

### 验收得分

| 检查项 | 结果 | 原因 |
|--------|------|------|
| pytest 全部通过 | FAIL | runner.py 直接导入 get_field_name，test 的 patch 不拦截 |
| 无残留 input_field | PASS | |
| 无 DEFAULT_INPUT 引用 | FAIL | 逆向修法保留了 DEFAULT_INPUT 这个名字 |
| 无误导性注释 | PASS | |

## Exp2 结果（熔断修复后）

- **验收得分：4/4 ✅**
- 耗时：66s，$0.43，haiku-4-5（model=sonnet 路由到 haiku，原因待查）
- 轮数：19 轮，无假熔断
- Agent 找到 **7 个问题**（比设计的 5 个多——BUG 2 被拆成 import + 函数参数两处，BUG 5 注释被单独报）

**7 个问题列表（agent 自述）：**
1. processor.py:2 — 误导性注释（DEFAULT_INPUT → DEFAULT_SOURCE）
2. processor.py:15 — `cfg["input_field"]` → `cfg["source_field"]`
3. validator.py:1 — `import DEFAULT_INPUT` → `import DEFAULT_SOURCE`
4. validator.py:4 — 函数默认参数 `DEFAULT_INPUT` → `DEFAULT_SOURCE`
5. runner.py:7 — 参数顺序反向
6. test_runner.py:8-9 — fixture 用 "input_field" → "source_field"
7. test_runner.py:14 — mock return_value "input_field" → "source_field"

**对比 Exp1（熔断前）：**

| 项目 | Exp1 | Exp2 |
|------|------|------|
| 验收得分 | 2/4 | 4/4 |
| 假熔断 | ✅ 触发（12 轮后） | ❌ 未触发 |
| 找到 Bug 数 | 5/5（但修法有问题） | 5/5（修法正确）+ 2 个细拆 |
| MCP 工具使用 | ✅ 全程 MCP | ✅ 全程 MCP |

---

## 发现的 yansh 系统 Bug

### B1：熔断计数器不感知 MCP 写入（优先级 P1）

**现象**：agent 用 `mcp__filesystem__edit_file` 写了 5 次文件，yansh 熔断计数器没有归零，触发了假熔断（"连续 12 轮无写编辑"）。

**根因**（已定位）：
- `subagent.py` 的 `_WRITE_TOOLS = {"write_file", "replace_in_file", ...}` 只列了内置写工具
- `agent.py:4377` 的 `productive` 判定：`tc.function.name in _WRITE_TOOLS`
- MCP 写调用名为 `mcp__filesystem__edit_file` 等，不在集合里 → 不计为 productive

**修复方向**：productive 判定加一条：工具名以 `mcp__` 开头且调用成功，视为 productive。

### B1 修复记录

已在 `yansh-code` 中修复：
- `subagent.py`：新增 `is_mcp_write(name, result)` helper + `_MCP_WRITE_HINTS` 常量；`files_modified` 追踪加 MCP 写分支（含 `destination` 参数支持）
- `agent.py`：`productive` 判定加 `is_mcp_write(...)` 条件
- `tests/unit/test_mcp.py`：补 6 个单测（含 server 名污染反例、缺省 isError、None result）

### B2：验收脚本 check3 过严

check3（无 DEFAULT_INPUT）不应禁止任何合法的 import 修法路径（保留 DEFAULT_INPUT 也是有效修复）。更好的检查：是否存在 ImportError（import 引用不存在的名称），而不是禁止特定常量名。

## 探索顺序分析

Agent 的探索顺序：
1. `list_allowed_directories` → 定位可访问目录
2. `directory_tree` → 俯瞰项目结构
3. `config.json` → 读权威来源
4. 按依赖序读：validator.py → processor.py → runner.py → test_processor.py → test_runner.py

顺序合理，反映了对依赖链的理解（从 config 出发，按 import 依赖从底向上）。
