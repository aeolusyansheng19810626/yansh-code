# MCP Exp1：filesystem 工具调用能力测试

## 实验目录

`C:\Users\ShengYan\Projects\AB-test\mcp-exp1-fixtrace`

## 场景：DataKit 依赖链修复

一个 Python 数据处理库经历了半完成的字段重命名重构（`input_field` → `source_field`），`config.json` 已正确更新，其余文件有 5 个断点。

### 5 个 Bug

| # | 文件 | 问题 | 难度 |
|---|------|------|------|
| 1 | `core/processor.py:18` | `cfg["input_field"]` → `cfg["source_field"]` | 低 |
| 2 | `core/validator.py:1` | `from core.processor import DEFAULT_INPUT` → `DEFAULT_SOURCE` | 低-中 |
| 3 | `pipeline/runner.py:8` | `validate_record("source_field", record)` 参数顺序颠倒 | 中 |
| 4 | `tests/test_runner.py` | fixture 用 `input_field` + mock 返回 `input_field`，测试内部自洽但验证旧行为 | 高（陷阱） |
| 5 | `core/processor.py:6` | 误导性注释 `# NOTE: DEFAULT_INPUT kept for backward compat, see validator.py` | 高（噪音） |

### 难度设计逻辑

- BUG 1+2 表层，任何 agent 能找到
- BUG 3 需要理解函数签名，不是 trivial 的字符串替换
- BUG 4 是"测试陷阱"：修完 1/2/3 后 pytest 通过，但 test_runner 在验证旧字段名。差的 agent 到这里就停了
- BUG 5 是噪音：comment 声称 DEFAULT_INPUT "仍然保留"，可能让 agent 误以为 validator.py 的 import 是有意的

## MCP 配置

`.yansh/mcp.json` 配置 `@modelcontextprotocol/server-filesystem` 指向 `project/`，agent 必须用 `mcp__filesystem__*` 读写文件。

## 验收

```bash
python run_accept.py .
```

4 项检查：
1. `pytest tests/` 全部通过（捕获 BUG 1/2/3，因为 BUG 4 的 mock 自洽）
2. 无残留 `input_field`（捕获 BUG 1 和 BUG 4）
3. 无 `DEFAULT_INPUT`（捕获 BUG 2 和 BUG 5）
4. 无 `DEFAULT_INPUT kept` 注释（捕获 BUG 5）

### 评分矩阵

| 修复范围 | Check1 | Check2 | Check3 | Check4 | 得分 |
|---------|--------|--------|--------|--------|------|
| BUG 1+2+3 only | ✓ | ✗ | ✗ | ✗ | 1/4 |
| BUG 1+2+3+4 | ✓ | ✓ | ✗ | ✗ | 2/4 |
| 全部 5 个 | ✓ | ✓ | ✓ | ✓ | 4/4 |

## 实验目的

1. Agent 是否真的调用 `mcp__filesystem__*` 而不是 fallback 到内置工具？
2. 探索顺序是否反映了对依赖链的理解（先读 config.json，再按依赖序读各层）？
3. 是否发现测试陷阱（BUG 4）？
4. 是否被噪音注释（BUG 5）误导，还是识破并删除它？
