# P0 #1 分层符号索引

ROADMAP P0 #1 三个未做项一起做完。

## 改了什么

### 1) workspace_symbols 改成支持分层

`workspace_symbols(extensions=None, path=None, recursive=False)`

- **默认 top 模式**（path=None, recursive=False）：只列顶层文件符号 + 子目录摘要
  （py_files / total_symbols 计数）
- **path 下钻**（path="sub/dir"）：返回该目录顶层文件符号 + 其子目录摘要
- **recursive=True**：旧全量行为，大项目慎用

返回结构加 `mode: "top" | "deep"` 区分；新加 `subdirs` 字段；空子目录（无 .py）不进
subdirs 减小噪音。

复用 `_parse_symbols_cached`、`_AST_CACHE`、`_WORKSPACE_SYMBOLS_IGNORE`——零改动。

新增内部 helper `_dir_symbol_count(dirpath, exts)`：递归统计某目录 .py 文件数 + 符号总数。

### 2) 新增 directory_summary(path=".") 工具

返回某目录整体感知：

```python
{
    "path": "src",
    "file_count": 12,
    "subdir_count": 3,
    "by_extension": {".py": 8, ".md": 2, ".json": 2},
    "key_files": ["README.md", "pyproject.toml"],
    "subdirs": ["agents/", "tools/", "tests/"],
    "files_sample": ["main.py", "agent.py", "...", "..."],
}
```

不递归——只看直接子项。`key_files` 候选清单覆盖常见 marker（README/pyproject/setup.py/
Makefile/Cargo.toml/go.mod/package.json/CLAUDE.md/ROADMAP.md/.agent_rules 等）。

错误：路径越界 → permission；目录不存在 → not_found；不是目录 → invalid_args。

### 3) audit() 改成顶层注入

agent.py:1349-1367 改写：默认调 `workspace_symbols()` 拿 top 结构，渲染顶层文件符号 +
子目录摘要 + 一行提示「用 path= 深入」。

`_AUDITOR_ROLE` prompt 加一段：
> 注入的是顶层结构。深挖某目录用 `workspace_symbols(path="...")` 或
> `directory_summary(path="...")`。**不要一次拿全树**（recursive=true 在大项目会撑爆 context）。

### 4) tools_schema.py 同步

- `workspace_symbols` description 改为反映分层语义；加 `path` / `recursive` 参数 schema
- 新增 `directory_summary` schema
- `READONLY_TOOL_NAMES` 加 `directory_summary`

### 5) agent.py dispatch 注册

import + readonly_handlers 加 `directory_summary`。

## 集成验证（yansh-code 自身）

| 模式 | 字符数 | 文件数 | 符号数 |
|---|---|---|---|
| top（新默认） | **3,314** | 12 顶层 + 2 子目录摘要 | 171 顶层 |
| deep（旧默认） | 12,975 | 40 全树 | 448 |

**缩减 74.5%**——这还只是 40 文件的中等项目。3000 文件的大项目按比例估算 deep 模式会
直接撑爆 200K context 窗口。

## 单测

`tests/unit/test_audit.py` 19 条全过：
- 旧 4 条 deep 行为用例：加 `recursive=True` 复原断言
- 新 7 条 workspace_symbols：top only / 嵌套递归计数 / path 下钻 / 空子目录跳过 /
  路径越界 / 不存在 / 不是目录
- 新 5 条 directory_summary：基本形态 / path 参数 / 路径越界 / 不存在 /
  不是目录 / files_sample 截断

`python tests/run_unit.py`：10/10 文件通过。

## 关键设计取舍

**为什么破坏默认行为而不是加 `mode="top"|"deep"` 参数**：
- ROADMAP 第 3 项明确目标是「audit 不再预注入全量摘要」——只有改默认才能让 audit
  自动收益
- LLM 看到的工具 description 默认会优先用——加参数让默认仍是全量等于没改
- 旧调用方（test_audit.py）显式标 `recursive=True` 表达意图，比"沉默继承全量"更可读

**为什么子目录摘要要递归计数**：
- 给 LLM 信息密度足够：「tests/ 有 23 个 .py / 265 个符号」一行可决策是否深挖
- 命中 `_AST_CACHE` 几乎零成本，不需要担心扫描开销
