# task_complete signal 持久化到 task_log

承接 [./2026-05-21_10-task-complete-signal-propagation.md](./2026-05-21_10-task-complete-signal-propagation.md)
末尾留下的「下一波」——把 signal 写进 `task_log` 持久化，让回放/统计能追溯
LLM 主动声明的历史。

## 改了什么

### 1) `finish_task_log()` 加 signal 参数

`task_log.py`：

```python
def finish_task_log(success, attempts, test_result=None, task_complete_signal=None):
    ...
    if task_complete_signal:
        _current_task_log["task_complete_signal"] = {
            "early_exit": bool(...),
            "success": bool(...),
            "summary": str(...)[:500],   # 截断防止日志膨胀
        }
```

向后兼容：传 None 时**不写入字段**——老日志读取不受影响（缺字段默认 None）。

### 2) `audit()` 返回值带上 signal

audit() 里识别 `_task_complete` sentinel 时返回值新增 `task_complete_signal` 字段。
fix() / code() 之前已经有这个字段（笔记 _10 实现）；这次补齐 audit 这条路径。

### 3) `run()` 8 个 finish_task_log 调用点全部传 signal

按当前持有的变量分别传：

| 调用点 | 传入 |
|---|---|
| audit 路径返回 | `res.get("task_complete_signal")` |
| Coder 主动放弃 | `coder_signal` |
| linter 阶段 LLM 放弃 | `fix_signal` |
| 测试通过 | `coder_signal` |
| fix LLM 完成 | `fix_signal` |
| fix LLM 放弃 | `fix_signal` |
| 达到最大尝试 | `coder_signal` |
| plan-only / 用户取消 | 不传（这两个路径根本没 signal 来源） |

### 4) `show_recent_logs()` 显示 TC 标记

```
2026-05-22T09:54:22 | ✓ | 4.42s | 0次 | TC:ok | ...
2026-05-22T09:48:13 | ✓ | 11.14s | 0次 | TC:ok | ...
2026-05-21T23:13:08 | ✗ | 274.95s | 3次 | ...           ← 老日志没 signal，无标记
```

`TC:ok` = LLM 主动 task_complete(success=true)；`TC:give-up` = task_complete(success=false)；
缺标记 = 沉默退出 / 老日志。

## 验证

### 单测（tests/unit/test_agent_loop.py，新增 4 条）

- `test_task_log_persists_task_complete_signal`：finish_task_log 收到 signal → 日志文件含字段
- `test_task_log_omits_signal_when_none`：不传 → 字段缺省
- `test_task_log_truncates_long_summary`：超 500 字符截断
- `test_audit_returns_signal_on_task_complete`：audit() 返回值含 signal

12/12 通过；全套 10/10 文件通过。

### 集成验证

跑一次 audit「yansh-code 顶层文件有几个 .py？」：

- 控制台：`审计完成（task_complete: 成功）...`
- 磁盘日志 jsonl：含 `task_complete_signal: {early_exit: true, success: true, summary: "..."}`
- 批处理 `--json` 输出：含同字段
- `show_recent_logs` 输出：行尾显示 `TC:ok`

## 评估

这一波让 P0 #3 闭环最后一条轨迹接通：

- 第一波（_07）：协议层（task_complete 工具 + error_kind + 软上限 + token 预算）
- 第二波（_08）：prompt 加固 + 沉默退出兜底
- 第三波（_10）：信号在 fix/code/run/report 的全流程贯通
- **第四波（本次）**：signal 持久化进 task_log——回放/统计能追溯

历史日志能区分三类结局：
1. **TC:ok**：LLM 主动 task_complete(success=true)，自然收尾
2. **TC:give-up**：LLM 主动 task_complete(success=false)，明确放弃
3. **无 TC**：沉默退出 / 兜底 / 老日志

为后续做"任务结局分布统计"、"按 LLM 主动 vs 被动结局做行为分析"、回放复盘等留好了数据基础。
