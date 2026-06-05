# task_complete signal の task_log への永続化

[./2026-05-21_10-task-complete-signal-propagation.md](./2026-05-21_10-task-complete-signal-propagation.md) に続いて、末尾で残された「次のステップ」——signal を `task_log` に書き込んで永続化し、リプレイ/統計で LLM が主動声明した履歴を追溯できるようにする。

## 何を変更したか

### 1) `finish_task_log()` に signal パラメータを追加

`task_log.py`：

```python
def finish_task_log(success, attempts, test_result=None, task_complete_signal=None):
    ...
    if task_complete_signal:
        _current_task_log["task_complete_signal"] = {
            "early_exit": bool(...),
            "success": bool(...),
            "summary": str(...)[:500],   # ログ肥大化を防ぐため截断
        }
```

後方互換性：None を渡す場合、フィールドを**書き込まない**——古いログの読み込みに影響なし（フィールド欠落時はデフォルト None）。

### 2) `audit()` の戻り値に signal を追加

audit() が `_task_complete` sentinel を識別する際、戻り値に新たに `task_complete_signal` フィールドを追加。
fix() / code() は既にこのフィールドを持っている（注釈 _10 で実装済み）；今回は audit() というパス経由でこれを補完。

### 3) `run()` の 8 つの finish_task_log 呼び出しすべてに signal を渡す

現在保持している変数に応じてそれぞれ渡す：

| 呼び出し位置 | 渡す内容 |
|---|---|
| audit パス リターン | `res.get("task_complete_signal")` |
| Coder の主動放棄 | `coder_signal` |
| linter ステージ LLM 放棄 | `fix_signal` |
| テスト通過 | `coder_signal` |
| fix LLM 完了 | `fix_signal` |
| fix LLM 放棄 | `fix_signal` |
| 最大試行回数に達成 | `coder_signal` |
| plan-only / ユーザーキャンセル | 渡さない（これら2つのパス経由では signal ソースがない） |

### 4) `show_recent_logs()` に TC マーク表示機能を追加

```
2026-05-22T09:54:22 | ✓ | 4.42s | 0次 | TC:ok | ...
2026-05-22T09:48:13 | ✓ | 11.14s | 0次 | TC:ok | ...
2026-05-21T23:13:08 | ✗ | 274.95s | 3次 | ...           ← 古いログは signal がなくマークなし
```

`TC:ok` = LLM が主動で task_complete(success=true) を実行；`TC:give-up` = task_complete(success=false)；
マークなし = 無言終了 / 古いログ。

## 検証

### ユニットテスト（tests/unit/test_agent_loop.py、新規追加 4 件）

- `test_task_log_persists_task_complete_signal`：finish_task_log が signal を受信 → ログファイルにフィールドを含む
- `test_task_log_omits_signal_when_none`：signal なし → フィールドが省略される
- `test_task_log_truncates_long_summary`：500 文字超過時に截断
- `test_audit_returns_signal_on_task_complete`：audit() の戻り値が signal を含む

12/12 合格；フル実行 10/10 ファイル合格。

### 統合検証

audit 実行「yansh-code トップレベルに .py ファイルが何個あるか？」：

- コンソール：`審査完了（task_complete: 成功）...`
- ディスクログ jsonl：`task_complete_signal: {early_exit: true, success: true, summary: "..."}` を含む
- バッチ処理 `--json` 出力：同じフィールドを含む
- `show_recent_logs` 出力：行末に `TC:ok` を表示

## 評価

このステップで P0 #3 ジャンクションの最後の軌跡を接続：

- 第1ステップ（_07）：プロトコル層（task_complete ツール + error_kind + ソフトリミット + token 予算）
- 第2ステップ（_08）：prompt 強化 + 無言終了フォールバック
- 第3ステップ（_10）：signal が fix/code/run/report 全流程で通す
- **第4ステップ（本回）**：signal を task_log に永続化——リプレイ/統計で追溯可能に

履歴ログは3種類の結末を区別可能：
1. **TC:ok**：LLM が主動で task_complete(success=true)、自然終了
2. **TC:give-up**：LLM が主動で task_complete(success=false)、明確に放棄
3. **TC なし**：無言終了 / フォールバック / 古いログ

後続の「タスク結末分布統計」、「LLM 主動 vs 受動結末の行動分析」、リプレイレビューなどのデータ基盤を整備。
