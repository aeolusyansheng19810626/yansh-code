# Task #5 v2/v3 検証：4 条 yansh リファクタリング上限改善法 → LLM 修完了，フレームワークがあと一歩

[`./20260523_task5_compare.md`](./20260523_task5_compare.md)に関連。task #5 v1 yansh 失敗 → 4 条 backlog 改善法を実施 → v2 実行 → detector 閾値/edit 戦略プロンプト不十分 → v3 再調整。

## 4 条改善法（commit はまだ実行されていない）

### 改善法 1：5 ラウンド上限の plan-driven 動的調整

`agent.py:code()` で各ファイルの `attempts_left = 5` を以下に変更：
```python
attempts_left = max(coder_rounds_per_file, ceil(expected_edits / coder_edits_per_round) + 2)
```

config に `coder_rounds_per_file: 5`、`coder_edits_per_round: 3` を追加。tools.py で expected_edits=60 の場合 22 ラウンド を取得（従来の硬 5 ラウンド対比）。

### 改善法 2：plan() の出力に expected_edits フィールドを追加

- `PlanFile` schema に `expected_edits: int` を追加
- plan system prompt にフィールド説明と推定ガイドを追加（"1 for new file write, 1-3 for tweaks, 5-20 for medium refactor, 30+ for sweeping signature changes; overestimate by 50%"）

### 改善法 3：_CODER_ROLE + ユーザーメッセージの二層 edit 戦略プロンプト

- `_CODER_ROLE` の Tool-call efficiency セクションに「Batch dense edits aggressively」を追加——同じパターンは `replace_all` を使用、異なるパターンは一度に複数並行実行、20 処以上の場合は `write_file` で全文書き直し
- code() で user_content を構築する時、expected_edits に基づき `【改動规模提示】` を追加：
  - `>= 15` → `write_file` 全文書き直しを推奨
  - `5-14` → 一度に複数の並行 `replace_in_file`
  - `< 5` → 単一ポイント変更

### 改善法 4：fix loop 上限の設定可能化 + 機械的エラー検出

- config に `fix_soft_limit: 12`、`fix_mechanical_error_bonus: 12` を追加
- fix() 開始時に regex で error_info をスキャン：`r"TypeError:.+?missing\s+\d+\s+required\s+(?:positional|keyword)\s+argument"`
- v1 の閾値 ≥5（v2 実測では触発されなかった、この実行では stderr が 2 箇所のため）、≥1 に調整

## v1 → v2 → v3 データ比較

| 項目 | v1（改善法なし） | v2（改善法 1.0，detector ≥5） | v3（改善法 1.1，detector ≥1 + edit_strategy_hint） | CC（参考） |
|---|---|---|---|---|
| duration | 499s | 460s | 581s | 294s |
| tool_calls | 130 | 129 | 92 | 54 |
| 総 tokens | 1.85M | **2.95M** ⚠ | 2.14M | 184K |
| sonnet input | 1.05M | 1.61M | 1.92M | 184K |
| haiku input | 778K | 1.35M | 190K | 0 |
| attempts | 3 max | 3 max | 3 max | 1 |
| yansh `test_result` | fail | fail | fail | pass |
| **実際 _err 適応率** | **4/56 (7%)** | 46/56 (82%) | **56/56 (100%)** ✓ | 100% + 補助関数 |
| replace_in_file 呼び出し | 7 | 23 | 9 | n/a (Edit を使用) |
| **write_file 呼び出し** | 0 | 0 | **2** ✓ | n/a |
| 5 ラウンド警告ファイル数 | 3 | 3 | 2（agent.py がもう枯渇しない） | n/a |
| fix scheduler 触発？ | n/a | ✗（閾値が厳しすぎる） | ✗（fix フェーズ stderr に TypeError なし） | n/a |

## v3 の主要な転機：LLM が本当に改完した

**実際の状態**：
- tools.py 56 処の `_err` 呼び出し全て tool パラメータ追加 ✓
- agent.py 4 処 ✓
- subagent.py 1 処 ✓
- test_tools.py に新しいユニットテスト追加 + 旧 _err 直接呼び出しに適応 ✓
- pytest **5 failed = baseline pre-existing**（test_execute_command_timeout / test_replace_in_file_path_traversal / test_path_traversal_protection / test_move_file_path_traversal / test_build_diff_lines_exactly_50_no_truncation）
- 41 passed（baseline 40 + 新 1 条 test_err_includes_tool_field）
- **0 個の TypeError tool パラメータ欠落エラー**

LLM は 2 回 `write_file` を使用（小ファイル subagent.py、test_tools.py）。tools.py 60 処は大きすぎるため整文字書き直しは実施されず——継続して 22 ラウンドの replace_in_file で改完了。

## しかし yansh フレームワークはまだ fail を報告

`test_result: fail / attempts: 3 max` の理由：

1. **fix loop が baseline 5 failures を pre-existing として識別していない**——`_TESTER_ROLE` の Investigation order の第 1 条では「失敗シンボルが plan files 範囲にあるか」と述べていますが、この実行では plan files が tools.py を含み、baseline failures も tools.py にあるため——LLM はこれを本実行の回帰と誤判定
2. **fix loop attempt 2/3 で LLM は仍繰り返し `cd /workspace && pytest` をトライ**（task #4 が暴露した docker-style パス仮定の古い問題）—— テスト出力を取得できない
3. fix scheduler detector が触発されない：fix フェーズで読まれた stderr はすべて path_traversal 類 AssertionError、**TypeError なし**——detector 設計は「signature 改変 + 呼び出し未全適応」の機械的エラーのみを対象とするため、現在のシナリオでは無効

つまり、**改善法 1-3 が「LLM が 56 処すべてを改完」という事件をアンロック**しましたが、yansh の「成功判定 + baseline 識別」はまだあと一歩です。

## 今回の成功の達成方法

v3 の `replace_in_file=9` vs v2 の `=23` を観察——v3 LLM は replace_in_file の呼び出しが実は減りましたが、**改完了**——これは v3 LLM が**より多くの並行呼び出し**または**複数ハンク replace_in_file（1 つの old_str が複数の変更が必要な _err 呼び出しを含む、整段落 context でラップ）** を使用したことを示唆します。

v3 LLM の戦略を詳しく見ると：
- 部分的な replace_in_file の old_str は 5-10 行にまたがり、1 回の置換で複数の _err を改変
- さらに 2 回の write_file で整ファイルを直接更新
- expected_edits プロンプトが LLM に「事情をわかった」と感じさせ、大規模な変更であり batch で行う必要があることを認識させた

## 残りの backlog（ここに記載）

1. **fix loop baseline failure 識別**：fix() に入る前に、修正前の pytest baseline failures を記録；fix 時に現在の failures \\ baseline を比較 → 増分失敗のみ fix；増分が空でも fix loop が実行される場合は、すべて pre-existing なため、直接 task_complete(success=true)
2. **LLM の `/workspace` パスに対する docker-style 仮定**：plan フェーズの system prompt に `WORKSPACE_DIR` 絶対パスを注入；execute_command ツールの説明で yansh は /workspace に chroot しないことを明記
3. **Coder フェーズ「ラウンド尽きた」偽警告**：v3 tools.py は実際に改完了しているのに「22 ラウンドを使い尽くした」と報告——警告は LLM が最後のラウンドで task_complete(success=true) かどうかをチェックし、そうであれば警告を出さない
4. **detector 誤報**：現在の detector は TypeError missing argument のみを確認。NameError / AttributeError などの「signature/属性改変による全 caller の障害」の機械的エラーに拡張可能

## token 増加の理由（v2 は v1 比 60% 増）

v1 attempts max 尽きた時は累計 1.85M tokens；v2 改善法 1.0 は Coder フェーズにより大きなラウンド予算を付与（22 ラウンド）し、各ラウンド LLM は完全な messages を再送信（tools.py の全文を含む）→ 22 ラウンドの input 重み付き合計が 2.95M に急増。
v3 は LLM が write_file をより早期に使用してファイルの一部を終了 + 複数ハンク replace で呼び出し数削減したため、token は 2.14M に低下。

**潜在的最適化**：Coder 単一ファイル loop 内で軽量履歴圧縮を実施（最新 3 ラウンドのツール結果のみ保持、古いものを「既 read tools.py L1-200」に折りたたむ）。ただしこれは別の P 作業です。

## 総括：4 条改善法 vs 実際の効果

| 改善法 | 設計目標 | 実際の効果 |
|---|---|---|
| #1 plan-driven 5 ラウンド上限 | 大規模変更ファイルが 5 ラウンドで分割されない | ✓ tools.py が 22 ラウンドを取得（対 5）、改完率 100% |
| #2 plan の expected_edits 出力 | scheduler にデータを提供 | ✓ LLM 推定がそこそこ妥当（60 / 6 / 2 / 6） |
| #3 edit 戦略プロンプト | LLM が主動的に write_file/replace_all を使用 | ✓ 部分的（v3 で 2 回の write_file 出現、0 回の replace_all） |
| #4 fix detector + bonus | TypeError 類機械的エラー追加予算 | ✗ 本実行 fix フェーズ stderr に TypeError なし → 触発されず |

**改善法 #1-3 が「LLM がクロスファイルリファクタリングを改完」をアンロック——これが task #5 v1 失敗の核心的原因**。改善法 #4 は本実行では触発されませんでしたが設計は正確です（将来的に「signature 改変、全 caller が未適応、テスト実行で TypeError 爆発」のシナリオを対象とします）。

## データファイル

- `20260523_task5_v2_yansh.json/_stderr.log` — v2 (改善法 1.0) データ
- `20260523_task5_v3_yansh.json/_stderr.log` — **v3 (改善法 1.1) データ、リファクタリング 100% 成功**
- v1 データは `20260523_task5_compare.md` を参照

## ステータス

- ✓ 4 条改善法を展開（agent.py + config.py 修正は未 commit）
- ✓ task #5 v3 LLM が 56 処の _err 呼び出しをすべて正確に改変、pytest 実際 5 failed = baseline
- ⚠ yansh フレームワークの「成功判定」+ 「baseline 識別」があと一歩（backlog に記載）
