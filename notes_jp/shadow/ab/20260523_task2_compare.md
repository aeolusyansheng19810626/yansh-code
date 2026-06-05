# AB Test #2：コード記述タスク — `tools.read_file` に `max_bytes` を追加

**プロンプト**（両側の共通要件）：
> yansh-code プロジェクトの `tools.read_file` にオプションパラメータ `max_bytes`（デフォルト None=制限なし）を追加し、読み込むバイト数を制限する。`max_bytes` を超える場合は、切り詰められた内容と `truncated: true` というマークフィールドを返す。同時に `tests/unit/test_tools.py` に切り詰め動作を検証する単体テストを1つ追加する。

**モデル**：Claude Sonnet 4.6（両側）
**日付**：2026-05-23
**タスクタイプ**：コード記述 + 単体テスト追加

## データ比較

| ディメンション | yansh (auto mode) | Claude Code サブ agent (general-purpose, retry v2) |
|---|---|---|
| 所要時間 | **253.76s** | **72.3s** |
| ツール呼び出し | **61** | **15** |
| Token (in+out) | **641,163** (in 626,517 + out 14,646) | **25,104** |
| ファイル変更 | **4** (tools.py / tools_schema.py / test_tools.py / agent.py) | **2** (tools.py / test_tools.py) |
| テスト成功 | ✓ | ✓ |
| 完了度 | done | done |

**yansh は CC より 25× トークン / 4× ツール呼び出し / 3.5× 時間を余分に使用** —— ただし**より多くの作業を実施**。

## 余分な費用の内訳 (yansh)

| ステージ | ツール呼び出し範囲 | 主な消費 |
|---|---|---|
| plan + code（タスクコア）| #1-29 | コード探索 → read_file 修正 → schema 修正 → 単体テスト追加、約 25 呼び出し |
| fix（既存テスト失敗）| #30-61 | テストスイート全体実行で 5 件の関連なし失敗をトリガー、根本原因を 1 つずつ調査 |
| dispatch_subagent | #30, #31, #55 | 3 つのサブ agent をサブタスクに派遣（探索 / ファイル読み込み / 失敗分析）|

yansh が fix ループに入った理由は、**テストスイート全体**（41 のテスト）を実行したためで、そのうち 5 つは履歴的な遺留失敗（test_execute_command_timeout / test_path_traversal_protection など）で、このタスクとは無関係。yansh の LLM は**最終的に自ら識別**して、これらの失敗は「このプランとは無関係であり、修正しない」と判定し、task_complete=True で終了。

CC サブ agent は、タスク要件の新しい単体テスト 1 件のみを実行（`pytest tests/unit/test_tools.py::test_read_file_max_bytes_truncation -v`）し、完全なスイートは実行しないため、既存の失敗が見えず、fix ループに入らない。

## 完了品質の違い ⭐

両側ともテストに合格し、切り詰めアルゴリズムは同じ（バイト境界 + デコード `errors='ignore'`）、offset/limit + max_bytes は積み重ね可能——**コア機能は一致**。

ただし**完了範囲が異なる**：

| | yansh | CC |
|---|---|---|
| `read_file` にパラメータを追加 | ✓ | ✓ |
| 単体テスト | ✓ | ✓ |
| **`tools_schema.py` に schema 宣言を追加** | ✓ | ✗ |
| **順に不要なコードを削除（agent.py から import 3 行削除）** | ✓ | ✗ |

`tools_schema.py` は**必要なセマンティッククロージャ**——これは LLM が参照するツール署名定義。追加しない場合、LLM は `read_file` に `max_bytes` パラメータがあることを知らず、新機能は事実上使用できない。

> **yansh が `tools_schema.py` を修正**：`"max_bytes": {"type": "integer", "description": "最大読取バイト数（オプション）"}` をツール schema に追加
>
> **CC は修正しない**：タスク文字通りの要件を満たす（パラメータ追加、テスト合格）が、LLM は新しいパラメータを使用できない

これは**ドメイン知識 vs 汎用ワークフロー**の違い：yansh のシステムプロンプトは「コード agent が工具署名を修正する際には schema を更新すること」と教える——これは yansh 内部の「ルール」。CC の汎用目的は、この文脈を持たない汎用助手であり、タスク文字通りの要件のみを見る。

## 意思決定の深さの違い（今回は反対）

CC サブ agent の `key_decisions` の末尾には、**非常に専門的なテスト詳細の洞察**がある：

> monkeypatch は `tools._WORKSPACE_ROOT`（Path オブジェクト、モジュールレベル定数）に直接パッチする必要があり、`config.WORKSPACE_DIR`（import 時に既に _WORKSPACE_ROOT にコピーされている）ではない

これは単体テスト内の真の落とし穴——`tools.py` の上部に `_WORKSPACE_ROOT = Path(WORKSPACE_DIR).resolve()`、monkeypatch `config.WORKSPACE_DIR` は機能しず、`tools._WORKSPACE_ROOT` に直接パッチする必要。

CC はこの洞察を明示的に出力；yansh のコードも正しい（monkeypatch 互換方法を使用）が、サマリーでこのトラップを強調していない。

**yansh task #1 の優位は「docstring を読む → 設計意図を提供」**；
**CC task #2 の優位は「key_decisions でこの隠れたトラップを明示的に記録」** —— スタイルが反対。

## プロセスの違い

**yansh の plan → code → テスト実行 → fix → task_complete 完全フロー**：
- テストスイート全体を自動実行 → 失敗を検出 → fix ループで 1 つずつ調査 → LLM が自ら「無関係の失敗は修正しない」と判定
- 利点：出力がより完全、schema / 不要なコードなど「暗黙的要件」をキャプチャ
- 欠点：固定フローのコスト高い、「ローカル小規模変更」タスクに対して過度なエンジニアリング

**CC サブ agent の単一ループ**：
- ファイル変更 → 指定されたテスト実行 → 終了
- 利点：高速、安い、要件をちょうど満たす
- 欠点：「他にすることはないのか」を主動的に考えない

## データ収集のギャップ

- yansh のツール呼び出し数 (61) は stderr コンソールに明示的に出力されず、task_log JSONL のみに記載
- CC サブ agent の `key_decisions` は、私がプロンプトで要求したもので、**デフォルト出力ではない** —— AB テストを設計するには、このような計測機能を意図的に追加する必要がある

## まとめ：どのシーンでどちらを選ぶ

| タスクタイプ | 推奨 |
|---|---|
| 探索 / 情報検索（task #1）| **CC**（経路が短い、ただし yansh の `get_symbol_definition` を使用すると docstring を取得して 1 段階上がれる）|
| 文字通りの要件に厳密に従う小規模変更 + テスト追加（task #2） | **CC**（25× 安い、要件をちょうど満たす）|
| **完全な機能実装**（schema、ドキュメント、クリーンアップ含む）| **yansh**（ドメイン知識がより深い、出力がより完全、25× 多く費用がかかるがセマンティッククロージャ）|
| 不慣れなコードベース | **yansh**（plan ステージで強制探索 + audit ステージで強制読み取り専用、より安全）|

## 1 つの発見：CC サブ agent の環境ハイジャックリスク

最初の retry CC（プロンプトに直接派遣）—— サブ agent は「.claude/settings.json の Bash allowlist 分析」に進み、**タスクから完全に逸脱**。70K tokens / 31 ツール呼び出し / 完全に無効な出力。

可能な原因：sonnet が「yansh-code プロジェクト」+ `fewer-permission-prompts` skill このワークフローパスをトリガーして、~/.claude/projects/.../JSONL のトランスクリプト履歴をスキャン。

**プロンプトを書き直し、厳密な制約を追加**（「~/.claude を読むな、fewer-permission-prompts をするな、skill プロンプトを無視しろ」）してから、タスクに戻った。

これは CC サブ agent が完全に自動的 + 豊富なツール + skill 注入の環境にある真のリスク——**ユーザーと yansh CLI はこのリスクを持たない**（yansh のツールセットとプロンプト範囲はより焦点化）。

## 添付：元データ

- `20260523_task2_yansh.jsonl` — yansh task_log（61 ツール呼び出し、token 完全記録）
- `20260523_task2_yansh_stderr.log` — yansh stderr コンソール（plan/code/fix 全プロセス markdown）
- `20260523_task2_cc_transcript.jsonl` — CC サブ agent JSONL トランスクリプト（v2 retry、15 ツール呼び出し）
- 最初の retry（タスク逸脱）は親対話のみに存在し、未保存

## 次のステップ

task #3 候補（アーキテクチャ論証）：yansh が `task_complete` をセンチネルツールから LLM の自然言語シグナルに変更する可能性を評価する。この純粋なディスカッション / ソリューション出力タスクでは、理論的には CC サブ agent の表現が yansh に近くなるはずである。なぜなら「ドメイン知識」と「テストパイプライン」に関わらず——これは優れた対照実験。

