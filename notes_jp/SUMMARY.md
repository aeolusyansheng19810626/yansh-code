# yansh ノート総覧（1つのファイルで全体を理解）

`notes/` 下の 40 篇のノートを 1 つにまとめました。イベントは時系列順、数字は保留します。

---

## 一言で言うと

yansh はコマンドラインプログラミングアシスタント、5/21 にフロー理解、5/22 に機能追加 + 大体検、5/23 に Claude Code サブエージェントと 5 回の AB 比較を実施 —— コード記述シナリオでは yansh は遅く、token 消費が多い（25×）、論証シナリオでは差が 4× に狭まり、ファイル間リファクタリングはフレームワークの制限を超えて初めて実行可能。

---

## Day 1：5/21 — 基本フローを整理

この日のメインの目標は「フローが実行可能か、実行可能ならば正しいか」を解決する。

1. **独立した reviewer を削除**（ノート 02）—— 独立した reviewer がコンテキストを見られないため、無限ループが発生、削除後に実行時間 272s から 164s に短縮。結論：単一エージェント自己検査 > 独立した reviewer Context 分割。
2. **dispatch の暗黙的な依存関係を修正**（ノート 04）—— Architect/Coder プロンプトに「関数署名を変更する場合は全ての呼び出し箇所（特に dispatch テーブル）を grep で確認する」を強制追加、yansh が初めて特定の次元で Claude Code を上回る。
3. **「所有権の事前識別」の機械的ルール追加**（ノート 06）—— LLM が pre-existing 失敗関連のコードを変更しようとするのを防ぐため、「この assert が参照するシンボルが今回の plan ファイルに含まれているか」の判定を追加、5 つの無関係な失敗が全て正しくスキップされるようになった。commit `34f22ce`。
4. **エラー復旧インフラストラクチャ**（ノート 07-10）—— `task_complete(success, summary)` ツール追加、token 予算警告、エラー標準化（21 個のツール 36 箇所）、fix/audit ソフトリミットを 12/16 に引き上げ、task_complete シグナルを fix ループ内から外層 attempts ループへ一貫させた。シナリオ B の実行時間は 31s から 2s に短縮。commit `7d1b399`。

---

## Day 2：5/22 — 機能追加 + 大体検 + 大修正

前半は多くの機能を追加、後半は 3 つの LLM レビューを取得して改善。

**前半に追加された機能（ノート 11-22、順序通り）：**

- 11：階層的シンボルインデックス、audit システムプロンプト injection 量 -74.5%
- 12：新しいツールが「自然に選択される」ようにする、LLM が強い推奨なしでも使用可能か検証
- 13：task_complete シグナルを task_log に永続化
- 14：JSON 解析の堅牢性 + グローバル状態リファクタリング（Session）+ Sandbox モード
- 15：Plan Mode 状態機
- 16：Skills システム（プロジェクトレベル + グローバル、frontmatter トリガー）
- 17：Skills に LLM セマンティックマッチング追加（キーワード不一致時のフォールバック）
- 18：サブエージェント派発（dispatch_subagent）、独立した messages が親 context を汚さない、10× token 節約
- 19：サブエージェント並行処理を改善（ThreadPoolExecutor）、3 並行実測 2.4× 高速化
- 20：MCP プロトコル統合
- 21：Hooks（4 つのイベント、3 つのアクション）
- 22：セッション間永続メモリ（4 つのタイプ、MEMORY.md インデックス）—— ROADMAP 完了

**後半の大体検 + 問題修正（ノート 23-28）：**

- 23：Gemini / Claude Opus / Codex 三社の同じコードレビューを取得、19 個の問題を発見
- 24：P0 修正（audit モード general subagent によるバイパス write file、プロジェクトレベル設定に trust 確認がなく RCE 脆弱性あり）
- 25：P1 4 つ修正（mcp デッドロック、孫プロセスリーク、memory パストラバーサル、tree-sitter 並行処理）
- 26：P2 5 つ修正（procutil 抽出、size cap とロック追加、stderr_buffer 長制限）
- 27：P3 テスト品質 3 つ + GitHub Actions ubuntu+windows CI 追加
- 28：P4 アーキテクチャ整理（frontmatter / append_active_prompts / subagent.py 分割抽出）、ROADMAP 全完結

---

## Day 3：5/23 — Claude Code サブエージェントと 5 回の AB 比較

1 つの token 削減計画と 5 回の比較実験を追加。

**Token 削減改造（ノート 2026-05-23_01）：**
- ICA ゲートウェイが prompt cache を透過しないことを検出、スキップ
- system prompt 全て英語化（末尾に「Always respond in Chinese」を追加）
- fix ループテスト範囲の精密化（変更ファイルから test_*.py を推論、全套を実行しない）
- read_file キャッシュヒット検出（thread-local cache）
- サブエージェント explorer/auditor を haiku に変更
- 失敗教訓：英語化が「無関係な失敗早期終了」の暗黙的ヒューリスティックを弱め、`_TESTER_ROLE` に反例 few-shot を追加して復帰

**5 回の AB 結果（yansh vs Claude Code サブエージェント）：**

| Task | タイプ | yansh | CC サブエージェント | 結論 |
|---|---|---|---|---|
| #1 | 純粋な探索（並行条件の確認） | 25s / 2 ツール呼び出し | 22s / 4 ツール呼び出し | 同等；yansh はシンボルツールで設計意図を取得、CC は grep パス方法がより移植性が高い |
| #2 | コード記述 + 単体テスト | 254s / 61 / 641K | 72s / 15 / 25K | yansh は 25× 遅い、全テスト実行が 5 つの pre-existing 失敗をトリガーして順番に確認 |
| #3 | 純粋な論証（方案評価） | 730K | 169K | yansh は 4× 遅い、論証タスク優位性減弱；CC は yansh が見逃した 2 つの隠れた罠を捕捉 |
| #4 | bug 修正（パストラバーサル単体テスト） | 88s / 24 / 249K | 31s / 6 / 63K | 修正法は文字通り同じ；両者とも resolve ダブルチェック漏れ（失敗信号なしで停止） |
| #5 v1 | ファイル間リファクタリング（64 箇所呼び出し適応） | 499s / 130 / 1.86M / **fail** | 294s / 54 / 184K / **pass** | yansh フレームワーク制限により密集修正に対応できず、56 箇所中 4 箇所のみ修正（7%） |

**Task #5 その後 4 バージョン修正（v2-v4）：**
- v3 plan-driven 動的ラウンド上限 + expected_edits + edit 戦略提示 + 機械的エラー検出予算追加変更 → LLM が 56 箇所全て修正、ただしフレームワークは fail と判定（baseline 誤識別 + LLM が docker スタイル `/workspace` パスを仮定）
- **v4** 「baseline pre-existing 失敗識別」追加（commit `1e3ce5f`）→ yansh 初めて pass、attempts=1、1.80M tokens

---

## 現在位置

- 22 単体テスト全緑 + ubuntu/windows CI
- ROADMAP P0-P4 全完結
- 5 回の AB 完了、ファイル間リファクタリング実行可能に
- token は CC の ~10×、主な原因は prompt cache なし（ICA 未透過） + 中文 system prompt + 毎ラウンド完全 messages 再送

## 残りのタスク（難度順）

**P1 3 つの小タスク（半日で清掃可能）：**
1. LLM が `/workspace` docker スタイルパスを仮定 —— plan prompt に実際の WORKSPACE_DIR を注入（< 2h）
2. Coder「ラウンド使い果たし」誤警告 —— 既に task_complete なら warning を報告しない（< 1h）
3. Detector を NameError / AttributeError に拡張 —— regex に数パターン追加（< 30min）

**P3 2 つのタスク（半日で清掃可能）：**
4. 5 回の AB 総合 README —— task#1-5 を 1 つの意思決定マトリックスに統合
5. read_cache ヒット率計測 —— ログ 1 行追加

**P2 1 つのハードタスク（1-2 日）：**
6. Coder 単一ファイルループ履歴圧縮 —— 22 ラウンド毎ラウンド全ファイル messages 再送、token 暴増の主原因。難点は messages シーケンス構造合法性（tool_use/tool_result ペアリング） + 簡単な単体テストパスなし、実際に長タスク実行してリグレッション検証が必要。

## 既に実施済みのもの（重複討論回避）

- ✓ Prompt cache 検出 ICA **未透過**、スキップ
- ✓ System prompt 英語化（commit `6d99a70`）
- ✓ Fix ループテスト範囲（commit `6d99a70`）
- ✓ Subagent を haiku に変更（commit `a6fad9c`）
- ✓ read_file キャッシュヒット検出（commit `a6fad9c`）
- ✓ baseline 失敗識別（commit `1e3ce5f`、task #5 v4 検証 pass）
