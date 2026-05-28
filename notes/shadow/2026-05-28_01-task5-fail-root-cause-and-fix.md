# task #5 失败根因分析与修复方案

**日期**：2026-05-28
**来源**：AB 测试 v3 yansh task5 失败（test fail，16 errors）

---

## 现象

```
TypeError: _err() takes 2 positional arguments but 3 were given
16 failed, 72 passed
[警告] tools.py 已用尽 23 轮工具调用上限（expected_edits=60）
[Coder task_complete] 检测到 '无需修改' 信号 → 跳过剩余 2 个文件
```

## 根因（两层叠加）

### 1. sys_prompt 与 edit_strategy_hint 互相矛盾（主因）

`agent.py:2097`（existing file sys_prompt）：
```
For existing files you **must** use replace_in_file for precise replacement;
do not rewrite the whole file with write_file
```

`agent.py:2120`（edit_strategy_hint，expected_edits>=15）：
```
如果各 edit 点 old_str 各不相同，**强烈推荐用 write_file 一次重写整个文件**
```

两条规则冲突，sys_prompt 是 system role 权威更高，LLM 遵守 sys_prompt 用 replace_in_file，忽略 user message 里的 hint。结果：tools.py 60 处全用 replace_in_file 逐点改。

### 2. budget 公式不够用

`max(5, ceil(60/3)+3) = 23` 轮，每轮实际可完成 ~1-2 次 replace_in_file（含 read_file 开销），23 轮最多改 20-25 处。tools.py 60 处的 _err 调用点只改了约 1/3，签名没改完：
- `_err` 函数定义本身可能没改（或改了但某些调用点未同步）
- agent.py 的调用已按新签名传 3 个参数 → TypeError

## 修复方案

### Fix 1：解除 sys_prompt 矛盾（关键）

当 `expected_edits >= 20` 时，existing file sys_prompt 中"must use replace_in_file"规则改为允许 write_file：

```python
# agent.py:2097 附近，existing file sys_prompt
if expected_edits >= 20:
    write_rule = "- For large batch changes (this file has {N} edit points): prefer write_file to rewrite the whole file in one shot — faster and less error-prone than {N} replace_in_file calls."
else:
    write_rule = "- For existing files use replace_in_file for precise edits, never rewrite a whole file."
```

### Fix 2：compact 阈值 80K → 30K

`agent.py:2155`：`int(_cfg("compact_threshold_tokens") or 80_000)` → 改为 `30_000`

AB 测试实证：yscode 30K 阈值触发 2 次（saved 28K/次），yansh 80K 23 轮从未达到。

## 不需要改的

- budget 公式：Fix 1 修完后 LLM 用 write_file，1 轮搞定整文件，23 轮 budget 绰绰有余
- edits_per_round：同上
