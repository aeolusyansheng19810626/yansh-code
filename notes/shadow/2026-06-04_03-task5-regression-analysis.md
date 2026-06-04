# task5 性能分析：coder 全量重读 + no-op 空转

日期：2026-06-04

## 现象

yansh task5（给 `_err()` 加必传参数，适配 ~65 处调用点）对比：
- 历史基准：469s / 948K tokens
- 本次（最新 workspace）：589s / 1,577K tokens（+26% / +66%）

## 根本原因（opus-4.8 分析）

### 1. 任务已半完成 + coder 缺少 no-op 早退（主因）

workspace 复制自最新 yansh-code，`_err` 签名和 agent.py 调用点已在之前改动中更新。实际只有 subagent.py 1 处需改，但 coder 仍对 4 个文件（tools.py / agent.py / subagent.py / test_tools.py）逐个"进入增量修改流程"——全量读文件 → 判定 no-op → task_complete。agent.py 最大，全量读一次就是大量 token，却是纯空转。

**缺失能力**：coder 入口没有"先 search_in_files 快速验证是否已符合要求 → 符合则 skip"的机制。plan 错误估算 agent.py 有 60 处改动，coder 盲目跟进。

### 2. 文件体积增大 + lint 范围太宽

P4-5 拆分 subagent.py 后，re-export 块让 agent.py 更大，每次全量读成本更高。ruff 扫描了 `.yansh/snapshots/` 快照目录，产生 275 个噪声 F401，fix 阶段把这些大块内容重复读入上下文两次。

### 3. AB 测试不公平

用"已含历史任务改动的最新 workspace"跑 task5 会系统性偏高——任务半完成态导致大量 no-op 空转，与历史基准不可比。

## 待修复项（新增技术负债）

### P1：coder no-op 早退机制

- **现状**：coder 对每个 plan 文件都全量读+多轮 LLM 判断是否需要改
- **目标**：coder 进入每个文件前，先用 `search_in_files` 快速验证改动是否已存在，已存在则 skip，不进入全量读循环
- **预期收益**：对"任务已部分完成"场景可节省大量 input token；对正常场景无影响
- **工作量**：半天 + 单测

### P2：lint exclude 快照目录

- **现状**：ruff 扫描 `.yansh/snapshots/`，产生大量无关 F401
- **目标**：在 pyproject.toml 或 ruff 配置里排除 `.yansh/` 目录
- **工作量**：5 分钟

### P3：AB 测试 workspace 公平性规范

- **现状**：每次从最新 yansh-code 复制 workspace，任务完成度不确定
- **目标**：task5 的基准 workspace 应固定为"任务未完成的初始状态"（从特定 git tag/commit 复制）
- **工作量**：明确 workspace 来源并写进 runner 注释

## 非回归结论

今天的三项改动（复杂度路由 / simple-fast / explorer 预算）均正常工作，task5 一次通过（attempts=1）。性能下降完全来自测试条件不公平 + coder 缺少 no-op 早退，不是功能回归。
