# Task #5 v4：backlog #1 baseline識別 → yansh ついに pass

[`./20260523_task5_v3_compare.md`](./20260523_task5_v3_compare.md) の続き。v3 は既に「LLM が 56 ヶ所の _err をすべて修正完了」をアンロックしましたが、yansh フレームワークは `test_result: fail` を報告していました——fix ループが baseline の既存失敗を今回の回帰と誤判定していました。v4 は backlog #1 を修正した後、yansh がこのクロスファイルリファクタリング task を初めて成功させました。

## 修正方法（commit `1e3ce5f`）

`agent.py`：
- モジュールグローバル `_BASELINE_FAILURES: set`
- `_parse_pytest_failures(text)` が `^FAILED <id>` 行を抽出
- `_capture_baseline_failures(test_command)` が `_run` の code() 進入前に一度 pytest を実行して baseline をキャプチャ
- test+fix ループ：`returncode != 0` だが `current \ baseline` が空 → 直接 pass と判定
- `fix(baseline_failures=...)` が baseline リストをユーザーコンテンツに注入（LLM も参照可能なスキップリスト）

`pytest` コマンドのみ baseline キャプチャを有効化、他の test_command はスキップ。キャプチャ失敗は best-effort で例外を投げません。

## v1 → v2 → v3 → v4 完全比較

| 次元 | v1（修正なし） | v2（修正法 1.0） | v3（修正法 1.1） | **v4（+ baseline 識別）** | CC（参考） |
|---|---|---|---|---|---|
| `test_result` | fail | fail | fail | **pass** ✓ | pass |
| `attempts` | 3 max | 3 max | 3 max | **1** ✓ | 1 |
| duration | 499s | 460s | 581s | 541s | 294s |
| tool_calls | 130 | 129 | 92 | **57** | 54 |
| 総 tokens | 1.85M | 2.95M | 2.14M | **1.80M** | 184K |
| sonnet input | 1.05M | 1.61M | 1.92M | 1.59M | 184K |
| haiku input | 778K | 1.35M | 190K | 178K | 0 |
| _err 対応率 | 4/56 (7%) | 46/56 (82%) | 56/56 (100%) | **100% (subset baseline)** | 100% |
| baseline shortcut 発動 | n/a | n/a | n/a | **✓ "15 件すべて baseline 内 → pass と見做す"** | n/a |

v4 は v3 修正法をベースに backlog #1 を追加 → 初回 attempt = 1、fix ループなし、機械的エラー検出器なし、直接 pass。

## v4 詳細プロセス

```
フェーズ1：計画策定
[baseline] pytest tests/unit/test_tools.py tests/unit/test_subagent.py を1回実行し pre-existing 失敗を記録...
[baseline] 16 件の pre-existing 失敗を記録（修正フェーズでは無視）
フェーズ2：コード生成
（57 個の tool_call で完了、2 つのファイルレベル write_file + 複数の replace_in_file を含む）
フェーズ3：テストと修正
テスト実行：pytest tests/unit/test_tools.py tests/unit/test_subagent.py
（15 failed、74 passed）
[baseline] 現在の 15 件の失敗はすべて baseline 内（16 件の pre-existing）→ pass と見做す
```

`task_complete_signal`：
> tests/unit/test_tools.py 内のすべての _err 関連変更が完了しました：1) test_err_helper_attaches_error_kind に第3パラメータ「read_file」を追加；2) test_err_helper_rejects_unknown_kind に第3パラメータ「some_tool」を追加；3) test_err_helper_attaches_tool は新規単体テストを追加し e["tool"] == "read_file" を検証。

注意：v4 実測 baseline 16 件、実行後 15 件——LLM の変更により pre-existing 失敗 1 件がたまたま修正された（ほぼ _err dict 変化により path_traversal クラスの某件が fail しなくなった）。subset 判定はまだ成立、pass 判定に影響なし。

## 4 ファイル attempts 上限？なぜ pass するか

警告：`agent.py の 5 ラウンド使用済み（expected_edits=5）` + `tests/unit/test_tools.py の 5 ラウンド使用済み（expected_edits=6）`。

しかし task_complete は LLM が最後のラウンドで明示的に呼び出し（success=true）——warning は「ラウンド数カウントが上限に達した」副作用です——これは実は backlog #3「ラウンド上限消費の誤警告」の実例です。機能上は pass に影響ありませんが、noise が1行警告ログ増えました。

## v3 → v4 改善幅

- tool_calls：92 → 57（-38%）— v4 attempts=1 で fix ループなし、fix ループのすべての呼び出し削減
- tokens：2.14M → 1.80M（-16%）— 同上
- duration：581s → 541s（-7%）— 主に fix ループなし

## v4 vs CC（参考）

CC は task #5 で 184K / 54 tools / pass を使用。v4 は 1.80M / 57 tools / pass。v4 tokens は CC の ~10 倍。

差異の源：
- yansh は毎ラウンド完全な messages を再送信、CC は prompt cache を使用（gpt5 plan §P1.0/P1.1 のテーマ）
- yansh の _CODER_ROLE / _PLANNER_ROLE は中文 + few-shot、CC のシステムプロンプトはよりコンパクト
- yansh の 22 ラウンド plan-driven は毎ラウンド完全ファイルコンテキストを再送信

token 削減は別の P 工作（gpt5-5-review-1-structured-cloud.md 計画内の P1.1 / P1.2 / P3.1）。今回は「クロスファイルリファクタリング task が実行可能」という基礎能力をアンロックしたのみ。

## 5 回の AB 完全軌跡

| バージョン | 修正法 | yansh test_result | attempts |
|---|---|---|---|
| v1 | なし | fail | 3 max |
| v2 | plan-driven 22 ラウンド + expected_edits + edit_strategy_hint + detector(≥5) | fail | 3 max |
| v3 | detector 閾値 ≥1 | fail | 3 max |
| **v4** | **+ baseline 識別** | **pass** ✓ | **1** ✓ |

v1-v3 の核心的問題は「LLM は修正完了したがフレームワークが認めない」でした。v4 の backlog #1 は「フレームワークの成功判定」を「LLM の実際の作業」に合わせました——増分回帰のみ確認し、pre-existing は一律スルー。

## 残存 backlog（task5_v3 の #2/#3/#4 はまだ未実施）

1. ~~**fix ループ baseline 失敗 識別**~~ ✓ 今回完成
2. **LLM の `/workspace` docker スタイルパス仮定**（task #4 で露呈、task #5 v3 fix ループでも衝突）—— v4 は fix ループを実行しないため未発生
3. **Coder「ラウンド上限消費」誤警告**：v4 は 2 件の warning を保有（agent.py / test_tools.py 5 ラウンド上限）、LLM は実際に task_complete(success=true) を実行済み
4. **Detector 拡張 NameError / AttributeError**：v4 detector を使用せず

## データファイル

- `20260523_task5_v4_yansh.json` / `_stderr.log` — v4 データ
- v3 データは `20260523_task5_v3_compare.md` 参照
- v1/v2 データは `20260523_task5_compare.md` 参照

## ステータス

- ✓ backlog #1 実装（commit `1e3ce5f`）
- ✓ task #5 v4 yansh 初回 pass、attempts=1
- ✓ 22 単体テスト全グリーン（baseline pre-existing を除き）
- クロスファイルリファクタリング task は yansh 構造上ここまで対応可能
