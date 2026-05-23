# Token 削减验证：P1.0 + P1.2 + P1.3 + P2.1 + P2.2 后再跑 task #2/#3

**日期**：2026-05-23
**对比对象**：
- baseline = 同日 task #2 / #3 第一次跑（commit 137b647 / 52971b5 之前的 yansh 状态）
- rerun = commit a6fad9c 之后（含 P1.0/P1.2/P1.3/P2.1/P2.2）

提示词与 baseline 完全一致。两次都 `--cwd .`、`--mode auto/audit`、sonnet 4.6 主模型。

## 总览

| 维度 | task #2 baseline | task #2 rerun | Δ | task #3 baseline | task #3 rerun | Δ |
|---|---|---|---|---|---|---|
| 用时 (s) | 254 | **402** | +58% ❌ | 156 | **203** | +30% ⚠ |
| 工具调用 | 61 | **75** | +23% ❌ | 50 | **55** | +10% |
| 总 tokens (in+out) | 641K | **1722K** | **+169%** ❌ | 730K | **729K** | -0% |
| sonnet input | 627K | 1043K | +66% | 716K | **53K** | **-93%** ✓ |
| haiku input | 0 | 663K | (新增) | 0 | **658K** | (新增) |
| 测试结果 | pass | **fail** | ❌ | pass | pass | |
| fix attempts | 1 | **3 (max)** | | n/a | n/a | |
| 估算成本 ($) | ~1.88 | ~3.79 | **+101%** ❌ | ~2.15 | **~0.82** | **-62%** ✓ |

成本估算：sonnet $3/M-input、haiku $1/M-input（粗算，未含 output）。

## 结论

**task #3（只读论证）效果显著**：
- sonnet 用量从 716K → 53K，因为 P2.2 把 explorer/auditor 子 agent 切到 haiku，2 个子 agent 跑了大头探索
- 总 token 数没降，但**成本降 62%**（haiku 单价 1/3 sonnet）
- 工具调用质量没掉：依然给出与 baseline 接近的论证结构

**task #2（写代码 + fix loop）反而恶化**：
- P1.3 的测试 scope 生效（`test_command: pytest tests/unit/test_tools.py`，对的）
- 但 LLM 在 fix loop 里**没像 baseline 那样早退**——baseline 1 次 attempt 后 LLM 自己识别"5 个失败是 pre-existing，不修"调 task_complete；rerun 跑满 3 attempts 才结束，最终 `test_result: fail`
- 多花的 ~1M tokens 全是 fix loop 反复跑测、改代码、再跑测的循环
- 怀疑是 P1.2 英文 prompt 削弱了"识别无关失败、不修复直接收尾"这个 heuristic——baseline 中文版的 `_TESTER_ROLE` / fix() prompt 大概率有更具体的"无关失败请直接 task_complete"提示

## 分模型 token 明细

### task #2 rerun
| 模型 | input | output |
|---|---|---|
| sonnet 4.6 | 1,042,683 | 13,259 |
| haiku 4.5 | 663,052 | 4,207 |

### task #3 rerun
| 模型 | input | output |
|---|---|---|
| sonnet 4.6 | 52,641 | 5,548 |
| haiku 4.5 | 658,166 | 12,378 |

## 各阶段验证

| Patch | 预期效果 | 实际效果 |
|---|---|---|
| P1.0 ICA cache_control 透传 | 探测：透传则上 P1.1 | ❌ 未透传（测得 cache_creation/read 都 0），P1.1 跳过 |
| P1.2 英文化 system prompt | input 砍 30-40% | ⚠ 短期看 sonnet input 反而涨（fix loop 失控），无法独立验证 |
| P1.3 fix loop test scope | 不再跑全套 pytest | ✓ task #2 实际命令 `pytest tests/unit/test_tools.py`（仅本任务相关） |
| P2.1 read_file 命中检测 | 重读 30-40% 命中 | 未单独度量，task #2 read_file 调用数仍多 |
| P2.2 subagent 切 haiku | 子 agent 部分省 ~70% 钱 | ✓ 显著：task #3 sonnet 用量降 93%，子 agent 全走 haiku |

## 待办

1. **P1.2 回滚或微调**：fix loop 早退失效是真问题。两个备选：
   - a) 给 fix loop user message 加显式提示："如果失败明显与本次 plan 无关（如已知 pre-existing 失败），用 `task_complete(success=true, summary='...pre-existing 不修复')` 直接收尾"
   - b) 把 `_TESTER_ROLE` / fix prompt 局部回滚成中文（保留其他 role 英文）
   推荐 a：保留英文化 token 收益，只补一句具体规则。
2. **P2.1 read cache 命中度量**：加日志记录 cache hit 率，下次 task #3 看实际节省多少 read。
3. **task #2 重跑**：修完 P1.2 fix loop 退化后再跑一次，期望落到 ~250K（接近 baseline 一半，sonnet→haiku 折扣已生效）。

## 数据文件

- `20260523_task2_rerun_yansh.json` / `_stderr.log`
- `20260523_task3_rerun_yansh.json` / `_stderr.log`
- baseline：`20260523_task2_yansh.jsonl` / `20260523_task3_yansh.json`
