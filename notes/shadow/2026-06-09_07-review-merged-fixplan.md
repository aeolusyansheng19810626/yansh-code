# Solo Mode 两轮 Review 合并 — 最终修复清单

> 第一轮裁决 + 模型画像见 `./2026-06-09_05-review-verify-and-model-profiles.md`。
> 第二轮深挖结果（gpt-5.5 真值表 + opus 复核三裁决/深化超时/新地带）见 `./2026-06-09_06-solo-code-review-result.md`。
> 本文是合并两轮后的**可执行修复清单**，按优先级。开修直接照此。

## 第二轮关键结论

**opus 复核第一轮我的三条裁决——全部确认：**
- threading.Lock 非跨进程 bug（worktree 独立进程独立文件）；同进程 subagent 线程锁够用；唯建议加原子写（编排器超时 `procutil.kill_tree` 杀进程组时，持锁线程的 `write_text` 可能写坏文件 → 下次 run 注入坏内容）。
- compact M1 边界确认安全，给了完整不可达性证明（pair 恒 assistant 打头，零散 tool pair 要进 recent 必先触发 `len(pairs)<=keep` 提前 return）。
- 摘要丢 plan-anchor 确认成立，定修法 **(b) pin**（三维度全胜 a：更稳/更省/更不易回退）。

**纠错（gpt 推翻第一轮一个理解）：**
- 第一轮我写「agent 不留测试 → gate `break` 跳过复核」**不准**。实际 `_infer_test_scope` 空 → `_detect_python_test_cmd(scope=[])` **回退全量**，甚至兜底 `python -m pytest`。`not test_cmd` 的 break 只在「项目完全无测试/非 python」才走。
- **真正的系统性失效链**：`task_complete(success=true)` → snapshot 缺失或 scope 漏匹配 → gate 回退**全量** → 无关旧测试绿 → `signal["success"]=True` → 报成功但**本次改动从未被相关测试复核**。scope miss / move 未记录 / 命名不匹配 / 非 python 检测错，全部汇入同一个「绿测即覆盖」的错误假设。

**opus 新挖（高危）：solo gate 绕过 smoke test**
- smoke 强制并入（`tests/test_smoke.py`）只在 `_apply_test_scope_override`（plan 路径）。solo gate 直接 `_infer_test_scope`，**无 smoke 强并入**。
- 更狠：agent 越认真写对应单测 → scope 越满 → smoke 越被排除；只有一个单测都没写、scope 空回退全量时才跑到 smoke。
- 与 plan-anchor 丢失叠加 = 跨文件 CLI 调用链断裂（各模块单测各自 mock 着过、`python -m pkg` 崩）的两道防线同时失效。**本轮最值得修的一条。**

## 修复清单

### P0 — 阻塞（"假绿"咬合三角 + 信号覆盖，必须一并修）

1. **gate signal 单值覆盖**（gpt 真值表证实，agent.py:4188-4212）
   拆 `agent_status`(completed/abandoned/fused/silent/limit) × `gate_status`(passed/failed/skipped/no_command/coverage_unknown)。
   最终 `success = (agent_status==completed) and (gate_status==passed)`；无测试命令 / scope miss 回退全量 → 不得保留 true，降级 `success=false` 或标 `unknown`。
   gate 绿只在 `signal["early_exit"] and signal["success"]`（agent 自述成功）时才能确认；否则绿只记「测试环境当前绿」，不改 agent 失败状态。

2. **test gate 30s 超时 + judge 三类混淆**（opus 深化，agent.py:226/235/187，tools.py:97）
   - `test(test_command, timeout_sec=None)` 透传；solo gate 用 `_cfg("test_gate_timeout_sec") or 300`，agent loop 内 execute_command 仍 30s（隔离）。
   - `_classify_test_failure(tr)`：`error_kind=="timeout"`(rc=-1) / `uncollectable`(rc=2 或 ImportError/collected 0) / `assertion`(rc=1)，把 (kind, hint) 拼进回灌头部。
   - 超时早停：连续 2 轮 kind=="timeout" 且 err 高度相同 → 停止回灌，避免烧满 8 轮。

3. **compact 丢 plan-anchor**（4 家共识，定修法 b，agent.py:1376-1380）
   pin 首个 assistant 规划 pair 进 head 不参与压缩。
   **实现陷阱（opus 警告）**：不能只靠位置——首次 compact 后 messages 变 `[sys,user,assistant_plan,sys_summary,recent...]`，第二次 compact 的 head 检测只抓 system+user，`assistant_plan` 会再落进 rest 被压。必须显式持久 pin（head 检测后 `if msgs[head_count] is assistant: head_count += len(该 pair)`，或单独存 `_solo_plan_pair` 每次重建强制置顶）。

4. **solo gate 绕过 smoke test**（opus 新发现，高，agent.py:180 / 08:73-75）
   抽 `_force_include_smoke(scope, ws)` helper，`_apply_test_scope_override` 与 solo gate 两路共用。消除「保险丝只挂 plan 一侧」隐藏耦合。

5. **回灌通道选择**（gpt #4 深化，agent.py:4197-4198，tools.py:410-459）
   弃 `stderr or stdout` + `raw[-4000:]`。构造确定性 payload：始终保 returncode+test_cmd；分别标注 STDOUT/STDERR 不二选一；pytest 优先截 stdout 中 FAILURES/FAILED/short summary/Traceback/AssertionError 窗口 + stderr tail；非 pytest 优先 stderr Traceback 窗口 + stdout tail；截断用「关键窗口+tail」非纯尾部。

### P1 — 应修（scope/数据流准确性 + 烧钱护栏）

6. scope 落空回退全量时标 `coverage=unknown`，不自动确认成功（gpt，与 #1 联动）。
7. `_infer_test_scope` 漏 `*_test.py`：预扫同时索引 `test_*.py` 和 `*_test.py`，源文件同时匹配 `test_<stem>` 与 `<stem>_test`（gpt，08:_infer_test_scope）。
8. `move_file` 成功不记录 modified → 记录 src+dst（至少 dst）（gpt，agent.py:1732-1741 / task_log.py）。
9. gate 内单次 `_solo_drive` 可烧光剩余 120 轮 → 加 `per_gate_round_limit`（5-10），总预算与每次回灌预算分开（gpt）。
10. 同错重复回灌无收敛检测 → (test_cmd + err 摘要 hash + modified snapshot) 三元组连续不变则提前失败退出（gpt）。
11. `total_rounds` 耗尽后还多跑一次测试 → gate 顶部先检查 `total_rounds>=soft_limit` 再跑（gpt，agent.py:4132-4148）。
12. solo gate 只用 python 探测 → 按项目类型选 detector，无则 `gate_status=no_command` 不伪造 pytest（gpt，node detector 已存在未接）。

### P2 — 低危跟进

13. 正则 `_STATE_CMD_RE` 缺词边界（tools.py:30）→ `^\s*(py\b|python[0-9.]*\b|pytest\b)`（确定真，低危污染）。
14. `agent_state.md` 原子写：tmp + `os.replace`（仍在锁内），防 kill_tree 写坏（opus，低危）。
15. no_progress 按 dispatch result 而非工具名（gpt/gemini）——**注意 R10 权衡**（连跑验证曾被误杀），改时保留 execute_command 算进展、仅排除「失败的写/重复失败命令」。
16. state 文件无界增长 → 每 section 保留最近 N 条 / 超 8KB 丢最旧。

### 待确认（不阻塞，审范围外 snapshot 模块）

- `_CURRENT_SNAPSHOT` 增量备份在并发 subagent（ThreadPoolExecutor）下是否加锁；是否 first-touch-only 语义（某文件第 1 轮写、第 5 轮又写，备份须留第 1 轮前原版，否则 /revert 还原错）。solo 多轮+gate 回灌比逐文件模式更易踩。开修前核 snapshot.py。

## 修复顺序建议

P0 五条一并修（#1#2#3#4 咬合成"假绿三角"，分开修无意义）→ 跑 `tests/unit/test_solo_loop.py` + 新增 gate 三态/超时分类/smoke 强并入用例 → P1 护栏 → P2 跟进。
先核 snapshot.py 定待确认项是否升级为 P1。
