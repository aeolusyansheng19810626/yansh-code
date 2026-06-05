# 3つのレビュアー（Gemini 3.1 Pro / Claude 4.7 Opus / Codex GPT-5.5）による包括的な発見

[_22](./2026-05-22_22-memory.md)を引き継ぐ：ROADMAP 12項目の完了後、3つの最高峰モデルにすべてのコードをレビューしてもらう（焦点：セキュリティ/バウンダリ、並行性、アーキテクチャ、テスト有効性）。
ソースファイル：`review_result.txt`。

## 3つのレビュアーの位置付け

| レビュアー | 実行時間 | 特徴 |
|---|---|---|
| Gemini 3.1 Pro | < 1 分 | カバレッジが広い、誤検出が混在 |
| Claude 4.7 Opus | 8m 34s | 最も深い、reader_loop EOF デッドロック等の独占的な真の問題を掘り出す |
| Codex (GPT-5.5) | 完了 | 2つのP0を独占的に発見（audit 回避、プロジェクト設定 RCE）+ tree-sitter 並行性 |

## 検証後の発見リスト（キーコードを読んで逐条検証済み）

### 🔴🔴 P0 深刻な問題 / 必須修正（Codex 独占）

**1. audit モードが `dispatch_subagent(role="general")` で回避可能**

チェーン：
- `tools_schema.py:459` `dispatch_subagent` が `READONLY_TOOL_NAMES` に存在
- `agent.py:916` audit がツール名でのみ遮断 → 許可する
- `agent.py:1978-1979` general role が `all_names - blocked` を取得、`write_file` / `execute_command` を含む
- `agent.py:2051` `dispatch_mode = "audit" if role in ("explorer","auditor") else "auto"` ← general は auto を実行
- `agent.py:2077` HIL/confirm はすべて禁止

PoC：audit モード下で LLM が `dispatch_subagent(task="write pwned.txt", role="general")` を呼び出す → 子 agent が実際にディスクに書き込む。audit の「読み取り専用の約束」が破られる。

修正方向：
- 親 mode=audit の場合、強制的に子 role を `explorer/auditor` に降格、または
- `dispatch_subagent` を `dispatch_readonly_subagent` / `dispatch_general_subagent` の2つの明示的なツールに分割、前者のみ READONLY に含める

**2. プロジェクトレベルの `.yansh/{mcp,hooks}.json` が無確認 RCE**

- `mcp_client.py:264` / `hooks.py:80` workspace が優先
- `hooks.py:145` `shell=True`、`mcp_client.py:77` 直接 `Popen`
- trust プロンプトなし

PoC：悪意のある repo が `.yansh/hooks.json` をコミット（UserPromptSubmit イベント）、ユーザーが clone + yansh 起動 + 最初の入力 → 任意コマンド実行。サプライチェーン攻撃の面。

修正方向：プロジェクトレベルの設定が初めて発見されたときに1度だけ trust 確認をポップアップ（`~/.yansh/trusted_workspaces.json` に書き込む）、認可されない場合はグローバル設定のみを読み込む。

### 🔴 P1 実際の脆弱性 / 今回必須修正

**3. `mcp_client._reader_loop` EOF が pending を起動しない**（Codex + Claude）
- `mcp_client.py:190-212` server がダウン/stdout が閉じる → for ループが終了 → `self._pending` が設定されない
- `_request` が `_CALL_TIMEOUT_SEC=60s` まで死等
- 修正：`reader_loop` に `try/finally` を追加、finally で pending をすべてエラー応答に設定 + `ev.set()`

**4. `mcp_client.shutdown` が子プロセスを強制終了しない**（Gemini + Claude）
- `mcp_client.py:71-88` Popen が `start_new_session` / `creationflags` を設定していない
- `hooks.py` と比較するとすでに正しく処理されている
- 結果：npx → node → mcp-server チェーンの子プロセスが孤児化
- 修正：hooks のセグメントからプロセスグループ作成をコピー；shutdown で `taskkill /F /T /PID` (Win) / `os.killpg` (POSIX) を使用

**5. `memory.find_memory` パストラバーサル**（Codex + 前回）
- `memory.py:141` `f = d / f"{name}.md"` が slugify されていない
- `save_memory:165` / `delete_memory:207` は slugify を実行、**読み取りパスのみが安全でない**
- PoC：`recall_memory(name="../../README")` → workspace/README.md を読み取る
- 修正：`find_memory` に `_slugify` + `Path.resolve().is_relative_to(target.resolve())` 二重検証を追加

**6. `_TS_PARSER` が並行性に対して安全でない**（Codex 独占）
- `tools.py:691` モジュールレベルのシングルトン、`706` ロックなし遅延ロード
- `tools.py:743` `parser.parse(src_bytes)` をスレッド間で共有
- tree-sitter Python バインディングの Parser はスレッドセーフではない
- 並行 subagent 起動 `agent.py:1995` がすべて `workspace_symbols()` を呼び出す → コールドキャッシュが parser を衝突
- 修正：parser parse にロックを追加、またはスレッドローカル parser を使用

### 🟠 P2 実際の問題 / 修正すべき

**7. `hooks.py` stdout に size cap がない → OOM**（Claude 独占）
- `hooks.py:167` `proc.communicate(input=stdin_text, timeout=timeout)` がサイズを制限しない
- 暴走した hook の数百 MB 出力が yansh をクラッシュさせることができる
- 修正：手動でループ読み + サイズ cap（デフォルト 1 MB）

**8. `task_log` グローバル list が並行 append される**（Claude + Gemini）
- `task_log.py:18-20` モジュールレベルのリスト ロックなし
- CPython GIL では現在クラッシュしない、free-threaded / 3.13+ 後にレース発生
- 複数 subagent がディスクに書き込む順序が乱れる
- 修正：`threading.Lock` を追加、append と snapshot はロック下で実行（15行）

**9. `init_mcp` `TOOLS[:]=...` が子 agent の反復とレース**（Claude 独占）
- `agent.py:2164` インプレース修正
- 子 agent `_subagent_tools_for_role:1974` が `{t["function"]["name"] for t in TOOLS}` を使用
- 子 agent 実行時に `/mcp restart` を実行 → `RuntimeError: list changed size during iteration`
- 修正：`init_mcp` にロックを追加、またはドキュメントで並行ホットリロードなしを明記

**10. `stderr_buffer.pop(0)` が buffer 読み取りとレース**（Gemini 独占）
- `mcp_client.py` `_stderr_loop` `pop(0)` が `call_tool` エラー診断読み `stderr_buffer[-3:]` ロックなしと競合
- 修正：`list` → `collections.deque(maxlen=50)`、1行修正

**11. `procutil.py` に抽出**（Claude 推奨）
- hooks のプロセスグループ kill セグメントを汎用に抽出（`spawn_with_pgroup` + `kill_tree`）
- 同時に mcp に使用、一挙に #4 を解決 + 再利用
- これが最もコストパフォーマンスの高い抽象化

### 🟡 P3 テスト品質 / クロスプラットフォーム

**12. `test_run_one_hook_timeout` 誤検出**（Claude + Codex）
- 現在テストしているのは「メインスレッド 1s 後に戻る」、taskkill が実際にプロセスツリーを削除したかを検証していない
- 修正：`psutil.pid_exists(pid)` を使用してプロセスが実際に終了したかを検証

**13. `test_run_subagent_max_steps_clamped_to_hard_cap` が実際に hard cap に到達していない**（Codex 独占）
- fake LLM が最初のラウンドで content を持つ → ループが直接終了
- 修正：fake LLM が毎ラウンド tool_call を返す必要がある、step が cap に実際に衝突させる

**14. GitHub Actions に ubuntu-latest matrix を追加**（Claude 独占）
- 現在のプロセスグループ kill は Windows でのみ実行；POSIX `os.killpg` に CI 検証がない
- 修正：matrix に ubuntu を追加、hooks/mcp フルスイートを実行

### 🔵 P4 アーキテクチャ改善 / 後続の磨き

**15. agent.py を `subagent.py` / `dispatch.py` に分割**（全員）
- 現在 2859 行
- 優先的に `subagent.py` を分割（最も独立、~250 行）

**16. `frontmatter.py` に抽出または pyyaml をインストール**（Gemini + Claude）
- skills.py / memory.py がそれぞれ半端な YAML パーサーを書いている、動作がすでに一致していない

**17. `_ACTIVE_*` を Session.pull/push に移動**（Gemini + Claude）
- `_ACTIVE_SKILLS_PROMPT` / `_ACTIVE_MEMORY_INDEX` / `_SUBAGENT_STATS` がミラーリングされていない
- 単体テストが相互に汚染（現在各テストが自分で対処）

**18. `build_system_prompt(role)` を関数に抽出**（Claude 独占）
- 5箇所で `_ACTIVE_SKILLS_PROMPT + _ACTIVE_MEMORY_INDEX` の連結が重複

**19. general subagent が親 agent に変更されたファイルを通知**（Gemini 独占）
- 親 agent が子 agent がどのファイルを変更したかを知らない → Lost Update レース
- summary の末尾に修正ファイルリストを追加

### ❌ 明確に却下（誤報）

- **hooks `shell=True` インジェクション**：stdin JSON がコマンドに連結されない、cmd は静的に hooks.json から来る
- **save_memory パストラバーサル**：`_slugify` が防止（`../../etc/passwd → etc-passwd`）
- **hooks/mcp 共通 base class を抽出**：Claude が反対、長期実行非同期 vs 短命同期の差が大きすぎる
- **Hooks daemon モード で遅延低下**：パフォーマンス最適化、現在 yansh は高頻度シナリオではない

## 3つの比較 / 教訓

- **Gemini 3.1 Pro**：カバレッジが広いが誤検出がある（shell インジェクション誤報）、アーキテクチャ提案（「オニオンモデル」）は価値があるが理論的
- **Claude 4.7 Opus**：最も深い、reader_loop EOF のような「コードを見て細部を理解する」独占的発見を掘り出す
- **Codex (GPT-5.5)**：権限境界を正確に捉える――audit 回避とプロジェクト設定 RCE は他の2つが完全に見逃した2つの P0

**重要な教訓**：
- 信頼境界が交差する（audit モードシグナル → subagent ツールセット）と、LLM は最も薄い穿孔を見つける
- プロジェクトレベルの設定ファイルは「コードベースを通じて移動する実行可能な実体」――trust モデルを持つ必要がある、デフォルトロードはできない
- 3つの組み合わせは単一よりもはるかに包括的――どの単一でも少なくとも1つの P0/P1 を漏らしている

## 修正順序（推奨）

1. **第1波（P0、必ず最初）**：audit コンテキスト降格 subagent role + プロジェクトレベル設定 trust prompt
2. **第2波（P1）**：mcp 3点セット（reader_loop / shutdown / procutil 抽出）+ memory.find_memory + tree-sitter ロック
3. **第3波（P2）**：hooks stdout cap + task_log ロック + init_mcp ロック + stderr_buffer deque
4. **第4波（P3）**：テスト psutil + max_steps 真テスト + GitHub Actions ubuntu
5. **第5波（P4）**：アーキテクチャ整理（subagent.py 分割 + frontmatter.py + Session ミラー）

各波に対応する単体テストを追加する必要があります。

## キーファイル

| ファイル | 変更ポイント |
|---|---|
| `agent.py:2040-2051` | audit コンテキスト降格 subagent role |
| `mcp_client.py:264` / `hooks.py:80` | プロジェクトレベル設定 trust prompt |
| `mcp_client.py:75-103` | Popen にプロセスグループを追加；shutdown がプロセスツリーを kill |
| `mcp_client.py:190-212` | `_reader_loop` finally が pending を起動 |
| `memory.py:141` | `find_memory` に slug + パス検証を追加 |
| `tools.py:691,743` | `_TS_PARSER` ロック追加またはスレッドローカル |
| `procutil.py`（新しいファイル） | `spawn_with_pgroup` + `kill_tree` |
| `hooks.py:167` | stdout cap |
| `task_log.py:18-20` | Lock を追加 |
| `agent.py:2155-2173` | `init_mcp` にロックを追加 |
| `mcp_client.py` stderr_buffer | `list` → `deque(maxlen=50)` |
| `tests/unit/test_hooks.py` | timeout psutil アサーション |
| `tests/unit/test_subagent.py` | max_steps 真テスト hard cap |
| `.github/workflows/*.yml` | ubuntu-latest matrix |
