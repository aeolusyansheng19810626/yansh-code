---
name: parallel-worktree
description: 方案A — yansh 多实例并行编排（git worktree 隔离）+ 修 snapshot 回滚误删缺陷
metadata:
  type: project
---

# 方案 A：worktree 并行编排 + 修回滚误删

**日期**：2026-06-07

## 背景
用户想像 Claude Code 那样让多个 agent 并行改同一仓库。调研确认 yansh 无任何多进程隔离设计（所有 Lock/"isolation" 都是进程内线程级）。最危险：`/revert` 全量差集删除"不在快照基线里"的文件 → 多进程下 A 回滚删光 B 的产物。

用户拍板：**完整 worktree 并行编排命令**（对标 cc），**留各自分支手动 merge**（后追加：编排器自动 commit 到分支，否则分支空 merge 不到东西）。

## 交付

### Part 1：修 snapshot.py 回滚误删（真 bug）
- 根因：`_backup_file_if_needed` 对"将新建文件"分支注释说要在 meta 标记却没实现 → 回滚只能靠 `current - workspace_files_then` 全量差集 `unlink`，误删外部/他进程新建文件。
- 修法：`create_snapshot` meta 加 `created:[]`；`_backup_file_if_needed` src 不存在时记入 `meta["created"]`；`_restore_file_snapshot` 只删 `created` 中当前存在的文件，移除全量差集。
- 效果：回滚只回退 baseline + 删本任务真正新建的文件，绝不误删外部文件。已知限制：只覆盖走 backup 钩子的新建（execute_command 旁路生成的不删，属"宁可漏删不误删"安全方向）。

### Part 2：worktree 并行编排（新模块 parallel_orchestrator.py）
- 入口：`python -m main --cwd <repo> --parallel <tasks.json>`。tasks.json 是数组，每项 `{name, prompt, mode?}`。
- 流程：校验 git 仓库 → 取 HEAD commit 作 base_ref → 每任务 `git worktree add <repo>/.yansh/worktrees/<name> -b yansh/<name> <base_ref>` → ThreadPoolExecutor 并行起子进程 → 各 worktree 自动 commit → 汇总 + 手动 merge 指引（不自动 merge、不删 worktree）。
- 子进程：`[sys.executable, "-m", "main", "--cwd", <wt>, "--mode", mode, "--json", prompt]`，cwd=TOOL_HOME。**必须 `python -m main` 而非 yansh.exe**（绕开 editable finder 并发损坏）。procutil.spawn_with_pgroup + communicate(timeout) + kill_tree。
- 自动 commit（关键修正）：`git add -A`（遵守 .gitignore 静默跳过 ignored）+ `git reset -q -- .yansh .yansh_history.json`（撤下 yansh 产物，两种 gitignore 情况都安全）；`git diff --cached --quiet` 判空跳过；commit msg = `yansh并行任务: <name>`。
  - **踩坑**：最初用 `git add -A -- . :(exclude).yansh ...`，当 .yansh 被 gitignore 时显式 pathspec `.` 命中 ignored 文件会**报错退出** → commit 全失败。改 add -A + reset 解决。
- config：`parallel_max_workers: 4`（防 ICA 限速）。

## 端到端验证（临时 git 仓库，2 任务并行改同一 calc.py）
- ✅ 主仓库 calc.py 未污染、status 干净 —— 隔离成功
- ✅ 2 worktree + 2 分支建好、并行跑完、汇总+merge指引齐全
- ✅ commit 落各分支且**内容干净**（无 .yansh/.yansh_history.json）
- ✅ `git merge yansh/feat-sub` 成功；`git merge yansh/feat-mul` 报 CONFLICT（同文件同区域）→ 正是"留分支手动 merge"的价值：并行改同文件各自干净完成，冲突在 merge 阶段显式交人解决，而非并行写互相覆盖
- 注：子任务 success=False（yansh 仍建 tests/、质量门）属被测 yansh 指令遵循问题，非编排机制。

## 流程
opus 调研→opus 计划→sonnet 改+单测→干净 opus review（2 轮：初版可合入；commit 增量发现展示层 bug 已修）→opus 端到端验证（发现 add pathspec 坑、files列换行，修复）→落盘。17 单测全过。

## 范围外（保留技术负债）
- ① 并发安全修复测试（缺口 G / task70）保留
- 自动 merge / 冲突解决（用户选手动）
- 落盘 history/replay 加 pid 命名空间（worktree 隔离下不触发）
- 同一 --cwd 多进程并行 / 跨进程文件锁（worktree 方案不需要）
