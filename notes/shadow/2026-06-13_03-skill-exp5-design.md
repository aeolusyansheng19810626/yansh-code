# Exp5-A：Skill 注入行为影响测试

## 实验问题

skill 内容注入 system prompt 后，agent 真的按 skill 里的约束写代码吗？

## 设计

### 受控变量
- **任务固定**：实现一个小工具模块（5 个函数，类似 strkit），PROMPT 不提类型标注
- **唯一变量**：workspace `skills/` 目录是否存在 strict-typing skill

### Skill 内容（`skills/strict-typing.md`）
```markdown
---
name: strict-typing
description: 所有公开函数必须有完整类型标注
triggers: ["实现", "函数", "模块", "implement"]
modes: []
---
## 强制要求：类型标注

- 每个公开函数的参数和返回值**必须**有类型标注
- 例：`def add(a: int, b: int) -> int:`
- 禁止裸函数（无标注）进入最终实现
```

### 两组跑法
| 组 | skill 文件 | 触发词（task 里含） | 预期 |
|----|-----------|-------------------|------|
| 有 skill | 存在 | "实现" | 函数有类型标注 |
| 无 skill | 不存在 | 同上 | 函数无类型标注（baseline） |

### 验收（`accept.py`）

- **check1**：pytest 全绿（功能正确性）
- **check2**：AST 扫描实现文件，统计有/无类型标注的公开函数数量（可量化）
- **check3（仅有 skill 组）**：有标注函数数 / 总公开函数数 ≥ 80%

## 任务设计（ws 里的 task.md）

```
实现一个字符串工具模块 strutil.py，包含以下函数：
- truncate(s, max_len): 截断字符串到 max_len，超出加 "..."
- pad_center(s, width, char): 居中填充到 width
- count_words(s): 统计单词数（按空白分割）
- remove_duplicates(items): 列表去重保序
- flatten(nested): 二层嵌套列表拍平

测试写在 tests/test_strutil.py。
```

## 验收脚本关键逻辑

```python
import ast

def count_annotated_functions(filepath):
    tree = ast.parse(open(filepath).read())
    total, annotated = 0, 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("_"):
                continue  # 跳过私有
            total += 1
            has_all = (
                all(a.annotation for a in node.args.args) and
                node.returns is not None
            )
            if has_all:
                annotated += 1
    return total, annotated
```

## 预期结论

- 无 skill 组：baseline 函数基本无类型标注（sonnet 默认行为）
- 有 skill 组：≥80% 函数有完整标注（skill 改变了行为）

如果两组结果无差异，说明 skill 机制注入了但 agent 没遵守——需要进一步分析 system prompt 位置或强度。

## 后续

- 方向B：把 triggers 设成不会命中的词，改用 LLM 匹配，测准确率
- 方向C：加第二个冲突 skill（"禁止类型标注，保持简洁"），看 agent 取舍
