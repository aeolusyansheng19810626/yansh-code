# H2-v3 实验结果：PostToolUse pytest 闭环（运行时 bug）

参考：[./2026-06-14_01-hooks-exp-plan.md](./2026-06-14_01-hooks-exp-plan.md)
参考 v2：[./2026-06-14_03-hooks-exp-h2v2-results.md](./2026-06-14_03-hooks-exp-h2v2-results.md)

## 配置

| 项 | 值 |
|---|---|
| 模板 | AB-test/PostToolUse-v3 |
| workspace | h2-v3-lru-hook |
| 任务 | 修复 lru_cache.py（LRUCache `get`/`put` 不调 move_to_end）|
| 初始失败 | 6 failed, 10 passed |

## 关键指标

| 指标 | H2-v3 | H2-v2 | H2-v1 |
|---|---|---|---|
| 成功 | ✅ | ✅ | ✅ |
| 黑盒 | 全过 | 全过 | 全过 |
| 轮次 | 5 | 4 | 8 |
| write_file 次数 | **0** | 1 | 2 |
| replace_in_file 次数 | **2** | 0 | 0 |
| hook 注入次数 | **0**（hook 未触发）| 0（静默）| 4（import 错误）|
| cost | $0.17 | $0.14 | $0.28 |
| duration | 27s | 30s | 46s |

## 执行轨迹

```
轮1: read_file(lru_cache.py) + read_file(tests/test_lru_cache.py)
轮2: replace_in_file(lru_cache.py) — 修复 get() 中的 # BUG: 注释行
     → hook 未触发（matcher=write_file，实际工具=replace_in_file）
轮3: replace_in_file(lru_cache.py) — 修复 put() 中的 # BUG: 注释行
     → hook 未触发
轮4: execute_command(pytest tests/ -q) → 16 passed
轮5: task_complete
```

## 两个设计缺陷（实验无效根因）

### 缺陷1：stub 残留 `# BUG:` 注释

`# BUG: missing self._cache.move_to_end(key)` 直接告知 agent 修改位置，
等同于把答案写在代码里。agent 不需要 pytest 反馈，靠注释即可静态定位。

**修复**：下一版 stub 完全去掉注释，让代码看起来"正确"——只有跑测试才能发现逻辑错误。

### 缺陷2：hook matcher 只覆盖 `write_file`，未覆盖 `replace_in_file`

yansh 有两个写文件工具：`write_file`（全量覆盖）和 `replace_in_file`（局部替换）。
agent 对小改动倾向于用 `replace_in_file`，导致 hook 全程静默。

**修复**：hooks.json 增加 `replace_in_file` matcher，或使用 `"matcher": "*"` 覆盖所有工具。

```json
// 修复后的 hooks.json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "write_file",
        "hooks": [{"type": "command", "command": "python hooks_scripts/pytest_feedback.py", "timeout": 35}]
      },
      {
        "matcher": "replace_in_file",
        "hooks": [{"type": "command", "command": "python hooks_scripts/pytest_feedback.py", "timeout": 35}]
      }
    ]
  }
}
```

## 结论

**H2-v3 因两处设计缺陷，hook 全程未参与修复。**

H2 系列三轮共同暴露了"hook 驱动自修复"的触发难度：
- v1：基础设施问题（sys.path），不是逻辑 bug
- v2：静态可见 bug，sonnet 一次写完，hook 静默
- v3：# BUG 注释泄露答案 + matcher 未覆盖 replace_in_file，hook 未触发

## H2-v4 设计要求

1. **去掉所有 # BUG 注释**，让 stub 代码看起来完全合理
2. **hooks.json 同时匹配 write_file 和 replace_in_file**
3. **bug 选择**：逻辑正确但边界条件错误，例如：
   - `move_to_end(key)` 调用位置正确，但漏了 `last=True` 参数（默认值即 True，实际无 bug 效果——改成真实能错的）
   - 或换题：用 dict + list 手动实现 LRU，去掉 OrderedDict，让 bug 更自然
