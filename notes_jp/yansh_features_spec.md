# yansh 機能要件チェックリスト（テキスト版）

commit `5c38fae`（task #5 v4 完了時点）での実装機能の完全セット。各項目は要件のみ記述し、コード実装の詳細は含まない。複数 agent による project 再構築の target spec として使用。

---

## 1. 製品ポジショニング

コマンドラインプログラミング助手 CLI：ユーザーが自然言語の要件を入力すると、LLM が自動的に **要件理解 → 計画立案 → コード記述 → テスト実行 → バグ修正** の全フローを完了し、最終的にコードを指定作業ディレクトリに書き込む。

対話モード（複数ターンの会話）およびバッチ処理モード（コマンドラインでの一度の要件入力 + JSON 出力）に対応。

---

## 2. 主要ワークフロー（コアループ）

4つのステージを直列実行：

1. **計画立案**
   - LLM は要件を N 個の変更/新規作成ファイルに分割し、各ファイルに簡潔な意図説明 + 予想改変量を付与
   - 同時に最終的な検収用のテストコマンド（例：`pytest tests/foo.py`）を提供
   - 計画は構造化 JSON で出力され、メインプログラムが解析して順番に実行

2. **計画実行とコード生成**
   - 計画ファイルリストに従って coder ループに順番に進入
   - 単一ファイルのループ内では、LLM が複数ターン（ファイル読取、ファイル変更、シンボル検索、コマンド実行など）でツール呼び出しを行い、そのファイルの完了を宣言するまで続く
   - 各ファイルにはラウンド数の上限保護があるが、計画内の「予想改変量」に基づいて動的に調整可能

3. **コード審査**（オプションステージ、徐々に coder 自己検査に統合中）
   - 独立した reviewer 役の LLM がコードスタイル、ロジック、規約を再確認
   - 不承認の場合は、意見を fix ループに返す

4. **テストと修正ループ**
   - 計画内のテストコマンドを実行
   - 失敗時は fix ループに進行：LLM がエラーログ + 現在のコードを確認 → コード変更 → テスト再実行、成功するか attempts 上限に達するまで繰り返す
   - max_attempts のデフォルトは 3 回

メインフローの完了後、成功/失敗 + サマリーを出力し、「実施内容」をセッション履歴に追加。

---

## 3. 実行モード

4つのモード（相互排他的）：

- **auto**（デフォルト）：plan → ユーザーが計画を手動確認 → code → review → test → fix
- **code**：auto と同じだが手動確認をスキップ（バッチ処理/CI 用）
- **plan**：計画のみ出力して実行しない、LLM の分解思考を素早くプレビューするのに使用
- **audit**：完全に独立した読み取り専用監査パス――ファイル書き込みなし、コマンド実行なし、fix ループ進入なし、markdown レポート出力のみ

モードはコマンドライン `--mode` または対話内 `/mode <name>` で切り替え可能。

---

## 4. Agent ロール（system prompt 切り替え）

異なるステージで異なる LLM パーソナリティプロンプトを使用：

- **Architect（アーキテクト / Planner）**：plan ステージを担当
- **Coder（コーダー）**：コード記述/変更
- **Reviewer（レビュアー）**：独立した再確認
- **Tester（テスター）**：テスト失敗分析、バグ修正
- **Auditor（監査官）**：読み取り専用監査モード
- **Subagent（サブ agent）**：以下の 3 つのロール
  - explorer（コード探索、情報検索、読み取り専用）
  - auditor（読み取り専用監査、メインの audit モードと類似）
  - general（汎用サブタスク）

---

## 5. ツールセット（LLM が呼び出し可能なツール）

約 25 個のツール、以下のように分類：

### 5.1 ファイル操作
- `read_file`：ファイル読み取り、offset / limit / max_bytes に対応
- `write_file`：ファイル全体を書き込み
- `replace_in_file`：正確な文字列置換（replace_all に対応）
- `append_to_file`：ファイルの末尾にコンテンツを追加
- `move_file`：ファイル名変更/移動
- `delete_file`：削除
- `apply_patch`：unified diff パッチを適用
- `list_files`：現在のディレクトリファイルツリーをリスト化
- `glob_files`：glob パターンでファイルにマッチ

### 5.2 コンテンツ検索
- `search_in_files`：ファイル横断 grep（正規表現 + ファイルタイプフィルタに対応）

### 5.3 AST シンボル検索（tree-sitter ベース）
- `list_symbols`：単一ファイル内の関数/クラスをリスト化
- `get_symbol_definition`：シンボル定義を取得
- `replace_symbol`：シンボル名で関数/クラス本体全体を置換
- `find_references`：シンボル参照を検索
- `workspace_symbols`：プロジェクト全体の関数/クラス一覧をスキャン（ファイル mtime でキャッシュ）

### 5.4 コマンド実行
- `execute_command`：shell コマンド実行（タイムアウト + 危険コマンド遮断付き）

### 5.5 プロジェクトナビゲーション
- `directory_summary`：ディレクトリ別ファイル数/種類分布のサマリー
- `git_diff`：現在の作業ツリーまたはステージ済みの diff
- `git_log`：最新 N 個のコミット

### 5.6 ネットワーク/参照
- `fetch_webpage`：ウェブページを取得して markdown に変換
- `search_docs`：ドキュメント検索

### 5.7 制御信号/メタツール
- `task_complete`：LLM が本ステージ完了を主動的に宣言（success フラグ + summary 含む）
- `update_plan_draft`：plan mode 内で計画草稿を更新
- `exit_plan_mode_signal`：plan mode から退出
- `dispatch_subagent`：サブ agent にサブタスク実行を派遣、結果を返す
- `save_memory` / `recall_memory`：長期記憶の読み書き（名前空間別）

ツールは実行モードに応じてトリミング：audit モード下では読み取り専用ツールサブセットのみ公開。

---

## 6. サブ agent 派遣（dispatch_subagent）

メインの agent は「個別の context が必要なサブタスク」をサブ agent に派遣可能：
- 入力：自然言語タスク説明 + role + max_steps
- サブ agent は独立した messages 履歴で、task_complete または max_steps 上限に達するまでツール実行ループを実行
- 返値：サブ agent の最終サマリー（内部 messages は非公開）
- サブ agent は再度派遣できない（再帰防止）
- explorer/auditor は自動的により安価なモデル（haiku）を使用、general はメインモデルを使用

---

## 7. モデル対応

複数の LLM をサポート：
- Claude シリーズ（Opus / Sonnet / Haiku、IBM ICA ゲートウェイ経由）
- DeepSeek（OpenRouter 経由）
- Gemini（Google Vertex AI）

特性：
- メインモデル + 自動ダウングレードカスケード（メインモデル失敗/タイムアウト時に自動で次を試行）
- コード記述モデル/Review モデルを個別に設定可能
- ストリーミング出力（トークンをリアルタイムに出力、黒画面での待機なし）
- モデルは呼び出し回数でトークン累計、最終的に価格表に基づいて費用推定を提供

---

## 8. 入力拡張構文

ユーザー入力は以下の特殊構文に対応（slash コマンドではない）：

- `@filename.py`：一時的に単一ファイルのコンテンツを本ターンのプロンプトに注入
- `@add_file <path>`：長期的にコンテキストに読み込む（毎ターン注入）、`@clear_files` までの間
- `@image <path/URL>`：画像を注入（ローカルパスまたは URL に対応、マルチモーダルビジョン）
- `@paste`：クリップボードから画像を読み込んで注入

---

## 9. コンテキスト管理

- セッション履歴は `<workspace>/.yansh/` 配下に保存され、次回起動時に自動読み込み
- 履歴がしきい値を超えると自動圧縮（最新 N ターンの元文 + 早期サマリーを保持）
- ユーザーが手動で `/compress` または `/clear` 可能
- プロジェクトレベルの規則ファイル `.agent_rules`：workspace ルートディレクトリに配置されたテキストファイル、毎ターン system prompt に注入
- read_file ヒット検出：単一タスク内で既に読み取った同じ ranges は messages に重複して含まれない

---

## 10. セキュリティメカニズム

- **危険コマンド遮断**：`rm -rf /`、`python -c <inline>`、許可されていない `pip install` など遮断またはユーザー確認をトリガー
- **パス越境保護**：write/replace/move の filename は workspace ルートディレクトリを超えることができない
- **タスク前スナップショット**：code ステージに進む前に計画リスト内のファイルをバックアップ（git stash 優先、git なし時はファイルコピーフォールバック）
- **スナップショット回復**：`/revert` でワークディレクトリをタスク前状態に戻す
- **サンドボックス**：オプション `--sandbox docker` で execute_command をコンテナ内で実行
- **workspace 信頼メカニズム**：新しい workspace 初回進入時にユーザーに trust を促す（悪意ある `.yansh/config.json` 注入防止）
- **HIL（Human-In-Loop）**：有効化後、各ファイル変更を diff で表示しユーザーに逐一確認させる（accept / reject / edit）
- **strict モード**：バッチ処理下で確認が必要なコマンドをすべて拒否（非対話的な再現性確保）

---

## 11. 組み込み slash コマンド（対話モード）

| コマンド | 機能 |
|---|---|
| `/mode <name>` | 実行モード切り替え（auto/code/plan/audit） |
| `/model` | 対話的にコード記述/Review モデルを切り替え |
| `/revert` | 前回タスク前の状態に回復 |
| `/context` | コンテキスト使用状況を表示 |
| `/history` | 対話履歴を表示 |
| `/stats` | トークン消費 + 費用推定 |
| `/config` | 現在有効な設定 |
| `/rules` | 現在の `.agent_rules` コンテンツ |
| `/hil [on/off]` | HIL モード切り替え |
| `/log` | 最近のタスクログ |
| `/compress` | 手動で履歴を圧縮 |
| `/clear` | 履歴を削除 |
| `/replay list/load` | タスク再生管理 |
| `/skill` | skill をリスト化/有効化 |
| `/memory` | 長期記憶をリスト化/表示/削除 |
| `/hooks` | 登録されたフック を表示 |
| `/mcp` | 接続された MCP server を表示 |
| `/subagent` | サブ agent 呼び出し統計 |
| `/plan_on` / `/plan_off` / `/plan` / `/approve` | plan mode 進入/退出/草稿表示/承認 |
| `/exit` `/quit` | 終了 |

---

## 12. タスクログ/再生/モニタリング

- 各タスクには完全なログがある：requirement、mode、model、plan、変更ファイル、すべてのツール呼び出し（パラメータ）、テストコマンド、テスト結果、attempts、duration、トークン消費（モデル別分類）、警告
- 失敗/例外は自動的に replay パッケージを `.yansh/replay/` にパック、ログ + ワークスペーススナップショット含む、`/replay load` で復帰をサポート
- monitor モジュールは replay ログを分析、エラーパターンを watch

---

## 13. プロジェクトレベル設定

`<workspace>/.yansh/config.json` は以下のキーを永続化：
- `model`：デフォルトモデル
- `mode`：デフォルト実行モード
- `max_attempts`：fix ループの最大再試行回数
- `test_command`：自動検出テストコマンドをオーバーライド
- `safe_mode`：危険コマンド遮断スイッチ
- `compress_threshold` / `keep_recent_turns`：コンテキスト圧縮パラメータ
- `human_in_loop`：HIL デフォルト状態
- `coder_rounds_per_file` / `coder_edits_per_round`：単一ファイルラウンド予算
- `fix_soft_limit` / `fix_mechanical_error_bonus`：fix ステージラウンド予算

CLI パラメータ（例：`--model`）は config.json をオーバーライド、config.json はデフォルト値をオーバーライド。

---

## 14. テスト統合

- プロジェクトタイプを自動検出（python / node）+ 対応するテストコマンドを選択（pytest / npm test / pnpm test / poetry / uv など）
- test_command の scope を自動検出（plan files → 関連 test_*.py を推論）
- 自動的に ruff lint を実行、エラーは fix ループに進む
- workspace にテストファイルがない場合、最小限のテストケースを自動生成（通常/境界/無効の 3 シナリオ）
- fix ループ進入前に baseline 失敗リストを自動キャプチャ、ループ内では増分失敗のみ修正

---

## 15. 拡張ポイント

### 15.1 Skills
- ユーザーが `~/.claude/skills/` または workspace `.claude/skills/` 配下に markdown ファイルでカスタム指示を定義
- メインプログラムはユーザー入力に基づくキーワードマッチング + LLM 選択を実行し、ヒット後に skill コンテンツを system prompt に注入

### 15.2 Hooks
- 7つのイベントポイントにユーザー定義 shell hook を注入：`UserPromptSubmit` / `PreToolUse` / `PostToolUse` / `Stop` / `SubagentStop` / `Notification` / `SessionStart`
- hook はメインフローに情報を拒否、変更、または補充可能
- 設定ファイル：workspace `.claude/settings.json` または全体 `~/.claude/settings.json`

### 15.3 MCP（Model Context Protocol）
- 起動時に `mcp.json` で宣言された MCP server に接続、それらが公開するツールを自動的に TOOLS リストに追加
- ツール名に `mcp__<server>__` プレフィックスを付加して競合を回避
- stdio / sse の 2 つの transport に対応

### 15.4 Memory（長期記憶）
- タスク横断で永続化された記憶、user / project / feedback / reference の 4 種 type に対応
- LLM は `save_memory` / `recall_memory` ツール経由で読み書き
- インデックスファイル `MEMORY.md` は各セッション起動時に自動読み込み

---

## 16. 中断と制御

- ESC キーでいつでも現在の LLM 呼び出し/ツールループを中断
- 中断後に状態を保持し、次ターンで続行
- 長時間タスクは自動的に KeyboardInterrupt / SIGTERM をキャプチャして優雅に終了

---

## 17. 出力と可観測性

- Rich Console カラー出力 + ステージマーク
- ストリーミングトークン リアルタイム出力
- 各タスク終了時にトークン使用量 + 費用推定を出力（モデル別に計費）
- `--json` モード：stdout は最終 JSON 結果のみ、stderr はプロセスログを保持（batch / pipe 用）

---

## 18. クロスプラットフォーム

- Windows / Linux / macOS で実行
- shell コマンド実行は統一 procutil で差異を吸収
- パス処理は pathlib を使用、全体で正反斜杠を許容

---

## 機能範囲外（明確に実装しない）

以下は現在の yansh にはなく、project 再構築でも実装しない（スコープの爆発を回避）：

- GUI / Web UI なし
- マルチユーザーコラボレーション なし
- バージョン管理 なし（git を代替しない）
- 独自言語サーバー なし（tree-sitter に依存した軽量 AST）
- バイナリファイル編集 なし（画像は「表示」のみ、変更はできない）
- タスク間の LLM messages 永続化 なし（history サマリーのみ保存）

---

## 再構築優先度提案（分期実施の場合）

**P0 コア（必須）**：第 2 / 3 / 4 / 5.1-5.4 / 6 / 7 / 12 / 13 / 14
**P1 拡張**：8 / 9 / 10 / 11 / 16
**P2 生態系拡張**：15.1-15.4
**P3 体験**：17 / 18
