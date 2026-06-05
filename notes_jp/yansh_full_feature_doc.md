# yansh-code 機能ドキュメント

## 1. プロジェクト概要

### 目標と用途

yansh-code は、LLM 駆動のローカルコード知能エージェント（coding agent）であり、完全な plan → code → test → fix サイクルをサポートします。設計目標は、ローカル開発環境での自律的なコーディングタスク実行と、十分なセキュリティコントロール、人的介入メカニズムの提供です。

### コア機能

- **マルチモードタスク実行**：auto/plan/code/audit 4 つの動作モード
- **ツール呼び出しシステム**：25 のツール、ファイル I/O、コマンド実行、AST 操作、コード検索、Web 検索をカバー
- **子 Agent アーキテクチャ**：最大 4 つの同時実行をサポートする子 agent 派遣
- **コンテキスト圧縮**：閾値超過時に自動的に Haiku で履歴を圧縮
- **拡張システム**：Skills、Memory、MCP、Hooks 4 つの拡張メカニズム
- **セキュリティメカニズム**：パス越界チェック、危険コマンド遮断、workspace 信頼、サンドボックス

### 技術スタック

| コンポーネント | 技術 |
|------|------|
| LLM 接続 | OpenAI 互換プロトコル（IBM ICA ゲートウェイ / OpenRouter / Vertex AI） |
| 構造化出力 | Pydantic v2（PlanResult / ReviewResult） |
| ターミナル UI | Rich カラー出力 + Windows Console API ネイティブ入力 |
| AST 操作 | tree-sitter + tree-sitter-python |
| Web ツール | requests + beautifulsoup4 + ddgs |
| サブプロセス管理 | psutil（クロスプラットフォーム プロセスツリー kill） |
| パスマッチング | pathspec（.gitignore スタイル） |
| パッケージエントリポイント | `yansh` → `main:main` |

---

## 2. ディレクトリ構造

```
yansh-code/
├── main.py               # CLI エントリ + インタラクティブメインループ
├── agent.py              # コア agent ロジック（3484 行）
├── config.py             # グローバル設定センター
├── llm_client.py         # LLM クライアントファクトリ + ストリーミング応答
├── tools.py              # 25 のツール実装
├── tools_schema.py       # ツール JSON Schema 定義
├── subagent.py           # 子 agent エグゼキューター
├── mcp_client.py         # MCP stdio クライアント
├── hooks.py              # イベント Hook システム
├── skills.py             # Skills ロードとマッチング
├── memory.py             # クロスセッション永続記憶
├── snapshot.py           # ファイルスナップショットとロールバック
├── task_log.py           # タスク実行ログ
├── state.py              # セッションレベル実行時状態カプセル化
├── linter.py             # プロジェクトタイプ検出 + Linter 実行
├── hil.py                # Human-in-Loop 人的確認
├── interrupt.py          # ESC キー中断検出
├── sandbox.py            # Docker サンドボックス（opt-in）
├── monitor.py            # ログ分析とモニタリング
├── procutil.py           # クロスプラットフォーム子プロセス管理
├── workspace_trust.py    # Workspace 信頼セキュリティチェック
├── frontmatter.py        # YAML frontmatter パーサー
├── console_shared.py     # Rich console シングルトン + JSON モード
├── pyproject.toml        # パッケージ設定（バージョン 0.1.0）
├── requirements.txt      # 依存宣言
│
├── .claude/
│   ├── settings.json       # Claude Code プロジェクト設定
│   └── settings.local.json # ローカル設定（git に含まない）
│
├── .github/workflows/
│   └── unit-tests.yml      # CI ユニットテストワークフロー
│
├── tests/
│   ├── run_all.py          # トップレベルテスト入口
│   ├── run_unit.py         # ユニットテスト実行器
│   ├── run_integration.py  # 統合テスト実行器
│   ├── unit/               # 22 個の pytest ユニットテストファイル
│   └── integration/        # 10 個のシナリオ式統合テストファイル（シナリオ 1–42）
│
├── workspace/              # デフォルト agent 作業ディレクトリ
│   ├── .agent_rules        # agent 行動ルール（プロジェクトレベル）
│   ├── .yansh/             # yansh ランタイムディレクトリ
│   │   ├── config.json     # プロジェクトレベル設定
│   │   ├── mcp.json        # MCP server 設定
│   │   ├── hooks.json      # Hooks 設定
│   │   ├── memory/         # プロジェクトレベル memory .md ファイル
│   │   ├── logs/           # タスクログ .jsonl
│   │   ├── snapshots/      # ファイルスナップショット
│   │   └── replay/         # 失敗リプレイパッケージ
│   └── tests/unit/
│
├── notes/
│   ├── SUMMARY.md
│   ├── yansh_features_spec.md
│   └── shadow/             # 開発ログ（命名：YYYY-MM-DD_NN-slug.md）
│       └── ab/             # AB テスト生データと比較レポート
│
└── scripts/
    ├── probe_ica_cache.py  # ICA キャッシュプローブ
    └── probe_ica_models.py # ICA モデルリストプローブ
```

---

## 3. コアモジュール

### 3.1 `config.py` — グローバル設定センター

**重要な定数**：

```python
CLAUDE_OPUS    = "claude-opus-4-7"
CLAUDE_SONNET  = "claude-sonnet-4-6"
CLAUDE_HAIKU   = "claude-haiku-4-5"
ICA_GEMINI_3_PRO  # ICA ゲートウェイでアクセス可能な Gemini モデル
ICA_GPT_5_4       # ICA ゲートウェイでアクセス可能な GPT モデル
OPENROUTER_BASE_URL = "https://api.nextgen-beta.ica.ibm.com/ica/v1"
WORKSPACE_DIR  = "workspace"
QUALITY_CASCADE = [CLAUDE_SONNET, CLAUDE_HAIKU]  # モデルダウングレードチェーン
TOKEN_PRICE_TABLE  # 各モデル $/1M token 価格表
MAX_ATTEMPTS   = 3
```

**`_DEFAULTS` プロジェクト設定のデフォルト値**：

| キー | デフォルト値 | 説明 |
|----|--------|------|
| `model` | `claude-sonnet-4-6` | メインモデル |
| `mode` | `auto` | 動作モード |
| `safe_mode` | `true` | セーフモード |
| `compress_threshold` | `6000` | 圧縮トリガー token 閾値 |
| `keep_recent_turns` | `3` | 圧縮時の保持最新ターン数 |
| `coder_rounds_per_file` | `5` | ファイルあたりの最大 coder ラウンド数 |
| `fix_soft_limit` | `12` | fix ループツールラウンドソフト上限 |
| `max_attempts` | `3` | 最大再試行回数 |
| `human_in_loop` | `false` | HIL スイッチ |
| `test_command` | `""` | 自動検出テストコマンドのオーバーライド |

**主要関数**：

- `set_workspace_dir(path)` — 作業ディレクトリ切り替え
- `load_project_config()` — `<workspace>/.yansh/config.json` からの設定ロード
- `get_config()` — 現在有効な設定辞書を返却
- `override_config(**kwargs)` — ランタイム設定項目のオーバーライド
- `get_model_price(model)` — モデル価格の照会

---

### 3.2 `agent.py` — コア Agent ロジック

**Pydantic Schema**：

```python
class PlanFile(BaseModel):
    filename: str
    intent: str
    description: str
    expected_edits: list[str]

class PlanResult(BaseModel):
    files: list[PlanFile | str]
    test_command: str

class ReviewResult(BaseModel):
    approved: bool
    issues: list[str]
    suggestions: list[str]
```

**モジュールレベルの重要な定数**：

```python
_FIX_SOFT_LIMIT    = 12       # fix loop ツールラウンド上限
_AUDIT_SOFT_LIMIT  = 16       # audit loop ツールラウンド上限
_FIX_TOKEN_BUDGET  = 60_000   # fix 予算警告閾値
_AUDIT_TOKEN_BUDGET = 120_000 # audit 予算警告閾値
MAX_HISTORY        = 20       # 最大履歴ラウンド数
CHAT_CONTEXT_ROUNDS = 5       # chat モード保持ラウンド数
COMPRESS_MODEL     = "claude-haiku-4-5"  # 圧縮用モデル
_TOOLS_LOCK        # TOOLS リスト並行読み書き保護ロック
_MECH_ERROR_PATTERNS  # 機械的エラー検出正規表現（fix 追加予算トリガー）
```

**ロール Prompt 定数**：

```python
_ARCHITECT_ROLE  # plan 生成アーキテクト
_CODER_ROLE      # ファイル単位でコード書き
_REVIEWER_ROLE   # コードレビュー
_TESTER_ROLE     # テスト失敗修正
_AUDITOR_ROLE    # 読み取り専用監査
_PLANNER_ROLE    # Plan Mode マルチターン対話
```

**主要プロセス関数**：

| 関数 | 説明 |
|------|------|
| `run(requirement, mode="auto")` | トップレベルタスクエントリ；plan → code → test → fix → review を直列化 |
| `plan(requirement)` | Architect LLM を呼び出して構造化 plan を生成（JSON 出力） |
| `code(plan_result, requirement)` | ファイル単位ループで Coder LLM を呼び出し、auto-compact + read_cache + HIL 含む |
| `fix(test_result, requirement)` | テスト失敗後に Tester LLM を呼び出してバグ修正、ソフト上限 + token 予算保護あり |
| `audit(requirement)` | 読み取り専用監査モード、Markdown レポート出力 |
| `review(requirement, modified_files)` | コードレビュー、approved/issues/suggestions を返却 |
| `chat(user_input)` | 通常対話（非タスク）ブランチ |
| `classify_input(user_input)` | 入力が "task" か "chat" かを判定 |

**ツール分発関数**：

| 関数 | 説明 |
|------|------|
| `_dispatch_tool_call(tool_call, ...)` | 単一ツール分発エントリ（PreToolUse/PostToolUse hook トリガー） |
| `_dispatch_tool_call_inner(...)` | 実際の分発：ツール書き込み HIL 確認、audit 遮断、read_cache 重複排除、MCP ルーティング |
| `_dispatch_tool_calls(tool_calls, ...)` | バッチ分発；`dispatch_subagent` ≥2 時に ThreadPoolExecutor で並行 |

**履歴管理関数**：

| 関数 | 説明 |
|------|------|
| `maybe_compress_history()` | 閾値超過時に圧縮トリガー |
| `compress_history()` | Haiku で摘要生成、旧ラウンド置換 |
| `_compact_messages(msgs, keep_recent_pairs)` | coder ループ内 auto-compact（最新 N pair 原文保持） |
| `save_history()` / `load_history()` / `clear_history()` | 永続化 / ロード / クリア |

**Plan Mode 関数**：

```python
enter_plan_mode()      # Plan Mode 進入
cancel_plan_mode()     # キャンセル
approve_plan()         # ユーザー承認、code ステージに切り替え
plan_chat(user_input)  # Plan Mode マルチターン対話
is_plan_mode()         # 現在状態クエリ
get_plan_draft()       # ドラフト内容取得
```

**補助関数**：

- `_append_active_prompts(sys_prompt)` — アクティブ化された skill prompt + memory インデックスをシステム prompt に追加
- `_call_with_json_retry(stage, messages, parser_fn, ...)` — LLM 呼び出し + JSON 失敗時自動再試行 1 回
- `_infer_test_scope(plan_files)` — 変更ファイルから関連テストファイルリストを推論
- `create_replay_package(failure_reason)` — 失敗現場を `.yansh/replay/` にパッケージング
- `init_mcp(verbose)` / `shutdown_mcp()` — MCP server 起動/シャットダウン

---

### 3.3 `llm_client.py` — LLM クライアント

**重要な定数**：

```python
LLM_TIMEOUT_SEC          = 120
LLM_MAX_RETRIES_PER_MODEL = 3
_RF_UNSUPPORTED          # response_format 非対応モデル集合の動的探出
```

**主要クラス**：

- `_StreamToolCall` — ストリーミング累積の tool_call、`model_dump()` 互換インターフェース提供

**主要関数**：

| 関数 | 説明 |
|------|------|
| `call_llm(messages, tools, tool_choice, response_format, stream, model_override)` | メイン呼び出しエントリ、QUALITY_CASCADE ダウングレード、ESC 中断検出、429/5xx 指数バックオフ再試行 |
| `_get_ica_client()` | Lazy 作成 IBM ICA 専用 client |
| `_get_gemini_client()` | 毎回 OAuth token リフレッシュ Vertex AI client |
| `_client_for(model)` | モデル別ルーティング対応 client |
| `_handle_stream(stream_iter, model)` | ストリーミング応答消費、リアルタイム出力、擬似 response オブジェクト返却 |
| `set_quality_cascade(cascade)` | モデルダウングレードチェーン切り替え |
| `get_session_total_tokens()` | セッション累計 token 数返却 |
| `get_session_token_breakdown()` | モデル分類別 token 明細返却 |
| `show_stats()` | token 消費と費用推定を出力 |

---

### 3.4 `tools.py` — ツール実装層

**ファイル操作ツール**：

| ツール関数 | 説明 |
|---------|------|
| `write_file(filename, content)` | ファイル書き込み（パス安全チェック含む） |
| `read_file(filename, offset, limit, max_bytes)` | ファイル読み込み（デフォルト limit=2000 行/200KB） |
| `replace_in_file(filename, old_str, new_str)` | 正確な文字列置換 |
| `apply_patch(patch_text, file_path)` | unified diff patch 適用 |

**コード分析ツール**：

| ツール関数 | 説明 |
|---------|------|
| `get_symbol_definition(symbol_name, file_path)` | AST 関数/クラス定義ロケート |
| `replace_symbol(symbol_name, new_code, file_path)` | AST 関数/クラス全体置換 |
| `list_symbols(file_path)` | ファイルすべての関数/クラス列挙 |
| `search_in_files(pattern, regex, extensions)` | グローバルコンテンツ検索 |
| `workspace_symbols(extensions, path, recursive)` | ワークスペースシンボルリスト（階層モード） |
| `directory_summary(path)` | ディレクトリ概要（ファイル数/拡張名分布） |

**実行ツール**：

- `execute_command(command, _timeout_sec=30)` — コマンド実行（3 段階ポリシー：deny/safe/confirm）、sandbox 包装サポート

**Agent 制御 Sentinel**：

| Sentinel 関数 | 説明 |
|--------------|------|
| `task_complete(success, summary)` | LLM タスク完了宣言 |
| `dispatch_subagent(task, role, max_steps)` | 子 agent 派遣 |
| `update_plan_draft` / `exit_plan_mode_signal` | Plan Mode 専用 |
| `save_memory` / `recall_memory` | memory モジュール透過 |

**重要な定数**：

```python
READ_FILE_DEFAULT_LIMIT    = 2000
READ_FILE_DEFAULT_MAX_BYTES = 200_000
ERROR_KINDS  # 標準化エラー分類集合

# セキュリティポリシー
_DANGEROUS_PATTERNS  # 危険コマンド正規表現ブラックリスト（rm -rf/sudo/curl|sh/PowerShell -enc など）
_SAFE_PATTERNS       # 確認不要なセーフコマンドホワイトリスト（pytest/ruff/ls など）
_CONFIRM_PATTERNS    # ユーザー確認が必要なコマンド（pip install/git checkout など）
```

**パス安全関数**：

- `_validate_path(filename)` — 絶対パス禁止、`..` 穿戻、シンボリックリンク逃亡禁止

---

### 3.5 `tools_schema.py` — ツール Schema 定義

**重要な定数**：

- `TOOLS` — 完全なツールリスト（25 のツール OpenAI function calling JSON Schema）
- `READONLY_TOOL_NAMES` — 読み取り専用ツール名集合、audit モード explorer/auditor ロールツールフィルター用

---

### 3.6 `subagent.py` — 子 Agent エグゼキューター

**重要な定数**：

```python
_SUBAGENT_HARD_CAP       = 16    # max_steps 上限
_SUBAGENT_CONCURRENCY_CAP = 4   # 並行子 agent 上限
_SUBAGENT_HAIKU_MODEL    = "claude-haiku-4-5"  # explorer/auditor デフォルトモデル
_WRITE_TOOLS             # 書き込みツール集合、general 子 agent 修正ファイル追跡用
```

**主要関数**：

| 関数 | 説明 |
|------|------|
| `_run_subagent(task, role, max_steps)` | 子 agent メインループ（独立 messages、再帰防止、thread-local 隔離） |
| `_subagent_handler(task, role, max_steps)` | `dispatch_subagent` ツール実際の処理エントリ |
| `_build_subagent_system_prompt(role)` | system prompt 構築（workspace シンボルインデックス + memory インデックス含む） |
| `_subagent_tools_for_role(role)` | ロール別ツール集合フィルター（explorer/auditor は読み取り専用ツールのみ） |
| `_subagent_model_for_role(role)` | explorer/auditor は haiku、general は親 cascade |
| `get_subagent_stats()` | 累計統計返却 |

---

### 3.7 `mcp_client.py` — MCP クライアント

**重要な定数**：

```python
_PROTOCOL_VERSION  = "2024-11-05"
_INIT_TIMEOUT_SEC  = 15
_CALL_TIMEOUT_SEC  = 60
```

**主要クラス**：

- `MCPServer` — 単一 MCP server のローカル stdio クライアント（JSON-RPC 2.0 プロトコル、tools/list + tools/call サポート）

**モジュールレベル関数**：

| 関数 | 説明 |
|------|------|
| `start_all_servers(workspace_dir, verbose)` | mcp.json に従ってすべての server 起動 |
| `discover_tools_as_schemas()` | server ツールを yansh TOOLS 互換 schema に変換（命名：`mcp__<server>__<tool>`） |
| `call_tool(prefixed_name, arguments, timeout)` | MCP ツール呼び出し |
| `shutdown_all()` | すべての server シャットダウン（atexit hook） |
| `load_config(workspace_dir)` | mcp.json ロード（workspace_trust セキュリティチェック含む） |

---

### 3.8 `hooks.py` — イベント Hook システム

**重要な定数**：

```python
_VALID_EVENTS      = ("PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop")
_HOOK_STDOUT_CAP   = 1MB     # hook OOM 防止
_HOOK_STDERR_CAP   = 256KB
```

**主要関数**：

| 関数 | 説明 |
|------|------|
| `run_hook_event(event, payload, match_target, workspace_dir)` | イベント トリガー、すべてのマッチング hook を直列実行、block/modify/system_messages/errors 統合 |
| `_run_one_hook(hook, payload, cwd)` | 単一 hook サブプロセス実行、stdout/stderr cap + タイムアウト kill 含む |
| `load_config(workspace_dir)` | hooks.json ロード（workspace_trust チェック含む） |
| `list_configured(workspace_dir)` | 現在設定一覧（`/hooks` コマンド用） |

---

### 3.9 `skills.py` — Skills システム

**主要クラス**：

```python
@dataclass
class Skill:
    name: str
    description: str
    triggers: list[str]
    modes: list[str]
    body: str
    source_path: str
```

**主要関数**：

| 関数 | 説明 |
|------|------|
| `discover_skills(workspace_dir)` | プロジェクトレベル + グローバル skill ディレクトリをスキャン（`<workspace>/.yansh/skills/` + `~/.yansh/skills/`） |
| `match_skills(user_input, skills, mode, use_llm)` | インテリジェントマッチング（キーワード fast path + LLM フォールバック） |
| `format_skills_prompt(matched)` | system prompt フラグメント フォーマット |
| `load_and_format(user_input, workspace_dir, mode, use_llm)` | ワンストップエントリ |

---

### 3.10 `memory.py` — 永続記憶システム

**重要な定数**：

```python
VALID_TYPES = ("user", "feedback", "project", "reference")
```

**主要クラス**：

```python
@dataclass
class Memory:
    name: str
    description: str
    type: str
    body: str
    scope: str       # "project" | "global"
    source_path: str
```

**主要関数**：

| 関数 | 説明 |
|------|------|
| `save_memory(name, type, description, body, scope, workspace_dir)` | memory 1 件書き込み + MEMORY.md インデックス更新 |
| `find_memory(name, workspace_dir)` | 名前で memory 検索（パス穿戻防止含む） |
| `discover_memories(workspace_dir)` | すべての memory をスキャン |
| `delete_memory(name, scope, workspace_dir)` | 削除 + インデックス更新 |
| `load_memory_index(workspace_dir)` | MEMORY.md インデックステキストロード system prompt 注入用 |

---

### 3.11 その他のモジュール

**`snapshot.py`**：

| 関数 | 説明 |
|------|------|
| `create_snapshot(file_list)` | スナップショット作成（plan のファイルバックアップ） |
| `restore_snapshot(snap_info)` | meta.json に従ってファイル復元 |
| `cleanup_snapshot(snap_info)` | スナップショットディレクトリ削除 |
| `_gc_old_snapshots(keep=10)` | 最新 N 個スナップショット保持、旧ものクリア |
| `get_latest_snapshot()` | 最新スナップショット返却 |

定数：`_SNAPSHOT_IGNORE_DIRS = {".git", ".yansh", "__pycache__", "venv", "node_modules", ".pytest_cache"}`

**`task_log.py`**：

| 関数 | 説明 |
|------|------|
| `init_task_log(requirement, mode)` | 現在のタスクログ初期化、token baseline 記録 |
| `finish_task_log(success, attempts, test_result, task_complete_signal)` | タスクログ落盤（token delta 計算含む） |
| `record_file_modified(filename)` / `record_tool_call(name, safe_args)` | 増分記録（スレッドセーフ、ロック） |
| `show_recent_logs()` | 最新 5 件ログ摘要出力 |
| `get_last_task_log()` | バッチ処理 `--json` 出力用 |

**`state.py`**：

- `Session`（dataclass）— agent.py/tools.py すべてのモジュールレベル可変状態をミラー、`pull()` / `push()` / `reset(workspace_dir)` メソッド提供
- `scoped_session(workspace_dir)` — コンテキストマネージャー、進入時スナップショット撮影、終了時復元（ユニットテスト隔離用）

**`linter.py`**：

- `detect_project_type()` — workspace をスキャンしてプロジェクトタイプ判定（Python/Node.js/Go/Rust/Java）、`(type_str, test_cmd)` 返却
- `run_linter_for(project_type)` — 対応言語 linter 実行（ruff/mypy/go vet/cargo clippy）
- `_detect_python_test_cmd(ws, scope)` — Python テストコマンド検出（uv/poetry/pytest/tox/make サポート）

**`hil.py`**：

- `hil_confirm(filename, old_content, new_content, is_new_file)` — diff 表示、y/n/e/a を質問（e=エディタ開く、a=本ラウンド全部受け入れ）
- `show_diff(filename, old_str, new_str)` — カラー unified diff 出力
- `reset_auto_accept()` — 各新タスク時に「全部受け入れ」状態をクリア

**`sandbox.py`**：

- `SandboxConfig`（dataclass：enabled, backend, image, extra_args）
- `parse_cli_arg(value)` — `--sandbox docker[:image]` CLI パラメータ解析
- `wrap_command(command, workspace_dir)` — 設定に従ってコマンド包装（無効時は原状返却）
- 定数：`DEFAULT_IMAGE = "python:3.11-slim"`

**`workspace_trust.py`**：

- `check_or_prompt(workspace_dir, config_filename)` — プロジェクトレベル設定ロード前にコール
- `is_trusted(workspace_dir)` — ホワイトリストファイル照会（`~/.yansh/trusted_workspaces.json`）
- `mark_trusted(workspace_dir)` — ホワイトリストに書き込み
- ホワイトリストパス：`~/.yansh/trusted_workspaces.json`

**`procutil.py`**：

- `spawn_with_pgroup(cmd, **popen_kwargs)` — 子プロセス起動、独立プロセスグループに配置（Windows: CREATE_NEW_PROCESS_GROUP；Unix: start_new_session）
- `kill_tree(proc, timeout)` — プロセスツリー全体を kill（psutil 優先；fallback に taskkill/killpg）

**`frontmatter.py`**：

- `parse(text)` — `(meta_dict, body)` 返却、スカラー/リスト/1 レベルネスト対応、pyyaml 非依存

**`monitor.py`**：

- `analyze_logs(log_dir)` — 総タスク数/失敗率/平均再試行回数を統計
- `watch_errors(log_dir)` — 同一タスク連続失敗検出、警告出力

---

## 4. CLI インターフェース

### エントリコマンド

```bash
yansh [OPTIONS] [REQUIREMENT]
```

### コマンドラインパラメータ

| パラメータ | 説明 |
|------|------|
| `--mode {plan,code,auto,audit}` | 動作モード（デフォルト auto） |
| `--model MODEL` | デフォルトモデルをオーバーライド |
| `--json` | バッチ処理モード、JSON を stdout に出力、ログを stderr に出力 |
| `--strict` | 厳密モード（バッチ処理失敗時に非ゼロ終了コード） |
| `--cwd PATH` | workspace ディレクトリを指定 |
| `--sandbox [docker[:image]]` | Docker サンドボックス有効化 |
| `REQUIREMENT` | （位置パラメータ）タスク直接実行、インタラクティブ進入しない |

`VALID_MODES = {"plan", "code", "auto", "audit"}`

### スラッシュコマンド（`_SLASH_COMMANDS`、計 24 個）

**モードとモデル**：

| コマンド | 説明 |
|------|------|
| `/mode <mode>` | 動作モード切り替え |
| `/model <model>` | モデル切り替え |

**履歴管理**：

| コマンド | 説明 |
|------|------|
| `/compress` | 履歴を即座に圧縮 |
| `/clear` | 履歴をクリア |
| `/log` | 最新タスクログ表示 |

**タスク制御**：

| コマンド | 説明 |
|------|------|
| `/revert` | 最新スナップショットにロールバック |
| `/plan_on` | Plan Mode 進入 |
| `/approve` | 現在の plan ドラフト承認 |

**拡張システム管理**：

| コマンド | 説明 |
|------|------|
| `/skill [list\|<name>]` | skills 列挙/表示 |
| `/memory [list\|save\|delete]` | 記憶管理 |
| `/hooks [list]` | hooks 設定表示 |
| `/mcp [list\|restart]` | MCP server 管理 |
| `/subagent [stats]` | 子 agent 統計 |

**その他**：

| コマンド | 説明 |
|------|------|
| `/help` | ヘルプ表示 |
| `/exit` / `/quit` | 終了 |

### 入力機能

`_read_input(prompt_str)` 実装（Windows ネイティブ Console API）：

- **Shift+Enter** — 改行挿入（マルチライン入力）
- **Tab** — スラッシュコマンド補完（`_match_slash` プレフィックスマッチ）
- **方向キー** — カーソル移動 + 履歴参照
- **ESC** — 現在の操作中断

---

## 5. Agent システム

### 動作モード

| モード | 説明 |
|------|------|
| `auto` | 自動判定：classify_input で task/chat 区分、task は完全 plan→code→test→fix |
| `plan` | plan のみ生成、実行しない |
| `code` | plan ステージをスキップ直接コード書き |
| `audit` | 読み取り専用監査、書き込みツール呼び出さない |

### ロールシステム

各 LLM 呼び出しは対応ロール prompt を使用：

- `_ARCHITECT_ROLE` — 要件受け取り、構造化 plan 生成（JSON 出力）
- `_CODER_ROLE` — plan の単一ファイル意図受け取り、ツール呼び出しコード書き
- `_TESTER_ROLE` — テスト失敗出力受け取り、ツール呼び出しバグ修正
- `_AUDITOR_ROLE` — 読み取り専用監査、`READONLY_TOOL_NAMES` ツールのみ使用
- `_REVIEWER_ROLE` — コードレビュー、`ReviewResult`（JSON）返却
- `_PLANNER_ROLE` — Plan Mode インタラクティブドラフト生成

### ツール分発メカニズム

```
_dispatch_tool_calls(tool_calls)
    ├── 単一ツール → _dispatch_tool_call(tool_call)
    │       ├── PreToolUse hook トリガー
    │       ├── _dispatch_tool_call_inner()
    │       │       ├── 書き込みツール HIL 確認（human_in_loop=true 時）
    │       │       ├── audit モード書き込みツール遮断
    │       │       ├── read_cache 重複排除
    │       │       └── MCP ルーティング（mcp__ プレフィックス）
    │       └── PostToolUse hook トリガー
    └── dispatch_subagent ≥2 → ThreadPoolExecutor 並行
```

### 子 Agent ロールと権限

| ロール | モデル | ツール集合 |
|------|------|--------|
| `explorer` | haiku | 読み取り専用ツール |
| `auditor` | haiku | 読み取り専用ツール |
| `general` | 親 cascade | 全ツール |

並行上限：`_SUBAGENT_CONCURRENCY_CAP = 4`；再帰深度保護：thread-local で子 agent が再び子 agent を派遣することを防止。

### プロンプト注入メカニズム

`_append_active_prompts(sys_prompt)` は各 LLM 呼び出し前に：
1. `skills.load_and_format()` を呼び出し現在入力にマッチング、アクティブ化された skill prompt を追加
2. `memory.load_memory_index()` を呼び出し MEMORY.md インデックス追加
3. `.yansh/.agent_rules` コンテンツ追加（プロジェクトレベルルール）

---

## 6. Compact/摘要メカニズム

### 2 段階圧縮戦略

#### レベル 1：グローバル履歴圧縮（`compress_history` / `maybe_compress_history`）

トリガー条件：履歴推定 token 数 > `compress_threshold`（デフォルト 6000）

プロセス：
1. Haiku を呼び出して旧ラウンドの摘要テキスト生成
2. 摘要を旧ラウンド置換、最新 `keep_recent_turns`（デフォルト 3）対を原文保持
3. 摘要を system メッセージ形式で履歴ヘッドに挿入

#### レベル 2：Coder Loop 内 Auto-compact（`_compact_messages`）

トリガー条件：coder loop 内メッセージリストが閾値超過

関数シグネチャ：`_compact_messages(msgs, keep_recent_pairs)`

ロジック：
- 最新 `keep_recent_pairs` ラウンドの原始メッセージ保持
- より古いメッセージ対して Haiku で摘要生成呼び出し
- 単一ファイル coder loop 内の履歴無制限増殖を防止（token 雪崩保護）

### 圧縮パラメータ

| パラメータ | ソース | デフォルト値 |
|------|------|--------|
| `compress_threshold` | `config.json` | `6000` |
| `keep_recent_turns` | `config.json` | `3` |
| `COMPRESS_MODEL` | 定数 | `claude-haiku-4-5` |

### 手動トリガー

- インタラクティブコマンド `/compress` — 強制即座に `compress_history()` を実行

---

## 7. Baseline テスト

### テストフレームワーク構造

```
tests/
├── run_all.py           # python tests/run_all.py
├── run_unit.py          # python tests/run_unit.py
├── run_integration.py   # python tests/run_integration.py
├── unit/                # pytest スタイル（22 ファイル）
└── integration/         # 自己充足シナリオ式（10 ファイル、シナリオ 1–42）
```

### ユニットテスト（`tests/unit/`）

実行方式：`python tests/run_unit.py` または `pytest tests/unit/`

コアテストファイル：

| ファイル | カバレッジ内容 |
|------|---------|
| `test_tools.py` | read/write/delete/execute_command/replace_in_file |
| `test_security.py` | パス越界遮断、危険コマンド遮断 |
| `test_subagent.py` | dispatch_subagent、role→ツール集合マッピング、再帰防止、並行、context 隔離 |
| `test_agent_loop.py` | fix()/code() task_complete 信号伝播 |
| `test_plan_mode.py` | plan ステージツール収縮 |
| `test_hooks.py` | hooks システムイベントトリガー |
| `test_memory.py` | memory 永続化とパス穿戻防止 |
| `test_parser_concurrency.py` | JSON 解析並行セーフティ |
| `test_task_log_concurrency.py` | task_log スレッドセーフティ |
| `test_session_isolation.py` | `scoped_session` 状態隔離 |
| `test_workspace_trust.py` | workspace 信頼ホワイトリスト |
| `test_mcp.py` | MCP JSON-RPC プロトコル |
| `test_skills.py` | skills ロードとマッチング |

### 統合テスト（`tests/integration/`）

実行方式：`python tests/run_integration.py`

出力形式：各シナリオ出力 `[PASS] シナリオ名` または `[FAIL: 理由] シナリオ名`、`run_integration.py` 統計合計通過率。

シナリオ分布：

| ファイル | シナリオ範囲 | 主要内容 |
|------|---------|---------|
| `test_1_9.py` | 1–9 | auto/plan/code モード、危険コマンド遮断、パス越界、replace_symbol、自動圧縮、ロールバック |
| `test_10_12.py` | 10–12 | 追記/シンボル検索/list_files |
| `test_13_16.py` | 13–16 | snapshot マルチファイル/並行/大ファイル |
| `test_17_19.py` | 17–19 | apply_patch/find_references |
| `test_20_23.py` | 20–23 | バッチ処理モード/JSON 出力 |
| `test_24_25.py` | 24–25 | セッションログ/task log |
| `test_26_27.py` | 26–27 | move_file/audit |
| `test_28_31.py` | 28–31 | HIL y/n/a/無効化 |
| `test_32_35.py` | 32–35 | MCP/skill/hook |
| `test_36_42.py` | 36–42 | review 非 JSON、fix 切り詰め、瞬時エラー判定、batch strict、replace_in_file マルチマッチ、call_llm timeout |

### CI

`.github/workflows/unit-tests.yml` — GitHub Actions 自動ユニットテスト実行。

---

## 8. AB テストフレームワーク

### 全体設計

`AB-test/yscode/` はバージョン回帰スモークフレームワーク、各 patch バージョンの行動改善効果を検証。

**コア思想**：同一タスク prompt、異なる yscode バージョン、完了度 / token 消費 / コストを対比。

### Runner 構造

```python
# 各 runner の呼び出し方式
sys.argv = ["yscode", "--workspace", "<path>", "--mode", "code", "--json", "<prompt>"]
from yscode.__main__ import main
main()
```

各 runner の `docstring` は履歴各バージョン結果を記録、追跡可能な A/B 対比ログとして機能。

### バージョン進化記録（task5 例）

| バージョン | 終局 | Token | コスト | 主要問題 |
|------|------|-------|------|---------|
| v0.2.1 baseline | PlanFailed | 789K | $2.58 | — |
| v0.3-α | PlanFailed | 307K | $0.95 | M-02: plan 詰まり 21 回 read_file |
| v0.3-β | CoderBudgetExceeded(16) | 496K opus | $7.94 | budget 逼迫 |
| v0.3-γ | CoderBudgetExceeded(14) | 481K sonnet | $1.51 | 型変更後も budget |
| v0.3-δ | read-only cap(4w/42t) | 1.52M | $4.74 | 迂回路 patch スクリプト書き |
| v0.3-ε | CoderBudgetExceeded(14w) | 1.13M | $3.05 | 14/65 のみ完了(22%) |
| v0.3-ζ | CoderBudgetExceeded(55w) | 3.74M | $11.09 | 履歴未収束（token 雪崩 +231%） |
| v0.3-η | FixExhausted | 4.38M | $13.08 | 偶発 4/4 |
| v0.3-θ | Budget exceeded(58w/109t) | 3.57M | $9.74 | 単一ファイル詰まり |
| v0.3-ι | FixExhausted | 2.51M | $6.66 | 4/4 確定 + multi-block |

### 特項検証 Runner（`z06_verify_runner.py`）

独立の `z06_verify_workspace/`（意図的にダメージ受けたテストファイル含む、exit_code=2 unparseable シナリオトリガー）を使用、Z-06.1/Z-06.3 具体修正点を検証、テストコスト上昇と雑音を削減。

### 検証方式

特定ログキーワード grep で内部動作を検証：

```python
# 典型検証パターン
assert "[fix]" in stderr
assert "[edit]" in stderr
assert "unparseable" not in stderr
```

---

## 9. 設定システム

### 環境変数（`.env`）

| 変数 | 説明 |
|------|------|
| `CLAUDE_API_KEY` | IBM ICA ゲートウェイキー（主要） |
| `CLAUDE_BASE_URL` | ICA エンドポイント（デフォルト `https://api.nextgen-beta.ica.ibm.com/ica/v1`） |
| `OPENROUTER_API_KEY` | OpenRouter キー（非推奨だがサポート） |
| `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` | バックアップ直接接続 |
| `GEMINI_API_KEY` / `GOOGLE_CLOUD_PROJECT` / `GOOGLE_CLOUD_REGION` | Gemini/Vertex AI |
| `HUMAN_IN_LOOP` | グローバル HIL スイッチ（デフォルト false） |
| `YANSH_TRUST_PROJECT_CONFIG` | workspace trust ポリシー（always/never/auto） |

### プロジェクトレベル設定（`<workspace>/.yansh/config.json`）

| キー | タイプ | デフォルト値 | 説明 |
|----|------|--------|------|
| `model` | string | `claude-sonnet-4-6` | メインモデル ID |
| `mode` | string | `auto` | 動作モード |
| `max_attempts` | int | `3` | 最大再試行回数 |
| `test_command` | string | `""` | 自動検出テストコマンドのオーバーライド |
| `safe_mode` | bool | `true` | セーフモードスイッチ |
| `compress_threshold` | int | `6000` | 圧縮トリガー token 閾値 |
| `keep_recent_turns` | int | `3` | 圧縮保持ラウンド数 |
| `human_in_loop` | bool | `false` | HIL スイッチ |
| `coder_rounds_per_file` | int | `5` | ファイルあたりの最大 coder ラウンド数 |
| `fix_soft_limit` | int | `12` | fix loop ツールラウンドソフト上限 |

### ランタイム設定関数

```python
get_config()              # 現在有効な設定を返却
override_config(**kwargs) # ランタイムオーバーライド（CLI パラメータ用）
load_project_config()     # config.json から再ロード
```

### 拡張システム設定

| ファイルパス | 説明 |
|---------|------|
| `<workspace>/.yansh/mcp.json` | MCP server 定義（command/args/env） |
| `<workspace>/.yansh/hooks.json` | Hooks 定義（event/match/command） |
| `<workspace>/.yansh/skills/*.md` | プロジェクトレベル skills（frontmatter + body） |
| `<workspace>/.yansh/memory/*.md` | プロジェクトレベル memory（frontmatter + body） |
| `~/.yansh/skills/*.md` | グローバル skills |
| `~/.yansh/memory/*.md` | グローバル memory |
| `~/.yansh/trusted_workspaces.json` | workspace trust ホワイトリスト |

### Skill/Memory ファイルフォーマット

```markdown
---
name: skill-name
description: ワンライン説明
triggers:
  - キーワード1
  - キーワード2
modes:
  - auto
  - code
---

# Skill 本文

具体的指示内容...
```

---

## 10. 依存と設置

### コア依存

| パッケージ | バージョン要件 | 用途 |
|----|---------|------|
| `openai` | >=1.0.0 | LLM 呼び出し（OpenAI 互換プロトコル、Claude/DeepSeek/Gemini 接続） |
| `pydantic` | >=2.0.0 | 構造化出力 schema（PlanResult/ReviewResult） |
| `python-dotenv` | >=1.0.0 | `.env` 環境変数ロード |
| `rich` | >=13.0.0 | カラーターミナル出力 |
| `tree-sitter` | >=0.25.0 | AST 解析 |
| `tree-sitter-python` | >=0.25.0 | Python AST シンボル操作 |
| `requests` | >=2.31.0 | Web スクレイピング |
| `beautifulsoup4` | >=4.12.0 | HTML 解析 |
| `ddgs` | >=7.0.0 | ドキュメント検索 |
| `prompt_toolkit` | >=3.0.0 | スラッシュコマンド補完 |
| `pathspec` | >=0.11.0 | .gitignore スタイルパスマッチング |
| `Pillow` | >=10.0.0 | 画像注入 |
| `psutil` | >=5.9.0 | クロスプラットフォーム子プロセスツリー管理 |

### Python バージョン要件

`requires-python >= 3.9`

### 設置方式

```bash
# ソースコードからのインストール（開発モード）
pip install -e .

# 依存のインストール
pip install -r requirements.txt

# インストール後利用可能コマンド
yansh [OPTIONS] [REQUIREMENT]
```

### 設定初期化

```bash
# 環境変数テンプレートをコピー
cp .env.example .env
# CLAUDE_API_KEY と CLAUDE_BASE_URL を記入

# 初回実行時に自動的に workspace/.yansh/config.json 作成
yansh
```
