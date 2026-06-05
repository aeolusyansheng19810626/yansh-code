# Token削減リファクタリング + fix loop回帰定位 + promptアンチパターン修正法

前回のABテスト3ラウンド（task #1/#2/#3、commit `78b2f5d` / `137b647` / `52971b5`）に続く。本日は完全な「診断 → リファクタリング → 検証 → 回帰定位 → 二次修正 → 再検証」サイクルを完了した。

最終的なコミット：`6d99a70` (P1.x) → `a6fad9c` (P2.x) → `cce571a`（回帰定位+アンチパターン修正 v1）→ `174df32`（notes/shadow ハード依存の削除）→ `b1d890f`（v2 検証 + plan 解析 bug 修正）。

## 1. 診断起点

baseline ABテスト3ラウンドの結果（yansh vs CC サブエージェント）：

| Task | yansh tokens | CC tokens | yansh/CC |
|---|---|---|---|
| #1 (探索) | (small) | (small) | ≈ |
| #2 (コード作成 + fix loop) | 641K | 25K | **~25×** |
| #3 (アーキテクチャ論証) | 730K | 169K | **~4×** |

調査と grep の後、6つの削減ポイントを特定し、2段階で実装。詳細は [`../shadow/ab/20260523_token_reduction_compare.md`](./ab/20260523_token_reduction_compare.md) を参照。

## 2. Phase 1（高ROI低リスク）

### P1.0 ICA gateway cache_control 透過探査

`scripts/probe_ica_cache.py`（一度きりスクリプト）で2つの同一2022トークンリクエストを送信し、`cache_creation_input_tokens` / `cache_read_input_tokens` をテスト。結果：**ICAは透過しない**（cache_control フィールドが黙って無視され、エラーも出ず効果もない）。**P1.1 Prompt Cache は直接スキップ**——cache 自体が価値がないわけではなく、ICAがこのルートをサポートしていないため進めない。

### P1.2 システムプロンプトの英文化

`agent.py` 内のすべてのロールプロンプト（`_CODER_ROLE` / `_TESTER_ROLE` / `_AUDITOR_ROLE` / `_PLANNER_ROLE` / 各 plan/code/audit/fix の sys_prompt 構築ポイント）+ `subagent.py:_SUBAGENT_ROLE` をすべて英文に変更。末尾に固定文 `Always respond in Chinese (用户的项目规则要求中文回复)` を追加。

**変更しない部分**：CLAUDE.md（ユーザー作成のため一方的に翻訳すべきでない）、ユーザーメッセージ、ツール schema description（ついでに一部修正したが徹底していない）、ロール末尾の少数の中文ルール。

中文の BPE トークンは英文と同等の意味より1.5-2倍多い。各ラウンド input で2-5K 削減を期待、長タスクでは累計が著しい。

### P1.3 Fix loop テストスコープ

`linter._detect_python_test_cmd(ws, scope=None)` に `scope: list[str]` パラメータを追加：scope がマッチした場合 `pytest tests/unit/test_X.py` を返す（特定ファイル）、そうでなければ元の動作（フルセット）。

`agent._infer_test_scope(plan_files)` が推論：「変更された各非テストソースファイルについて、同名の `test_<basename>.py` を検索；変更されたのがテストファイル本身の場合は直接スコープに入れる」。

`_apply_test_scope_override(plan_result)` は `code()` / `audit()` が plan を受け取った直後、`plan_result["test_command"]` を即座に上書き——LLMが `pytest`/`pytest -v` のようなフルセットコマンドを指定した場合のみ有効、`make test` / `tox` のようなラッパーには触らない（LLMの明示的選択を尊重）。

スコープ注入の境界をカバーする16個の新規単体テストを追加（uv lock / Makefile / tox ラッパースキップ / マルチファイル結合 / 空 plan）。

## 3. Phase 2（中ROI中複雑度）

### P2.1 read_file ヒット検出（thread-local）

`agent._read_cache_state = threading.local()`、`init_task_log` 時にクリア。`_dispatch_tool_call_inner` は readonly_handlers ブランチ前に検出：`name == "read_file"` かつ `_read_cache_hit_or_record(args)` がヒット → スタブ `{"hit": "X.py L1-100 (cached)"}` を直接返却、元の read_file は実際にディスクを読んだり完全なコンテンツを messages に追加したりしない。

最初はモジュールレベルの `_READ_CACHE: set + Lock` として書いたが、`test_dispatch_tool_calls_subagent_exception_isolated` に並発サブエージェント共有キャッシュの相互浸透により検出された。`threading.local()` に変更後、各スレッドが独立。

`tests/unit/test_read_cache.py`（8個の単体テスト）を新規作成：thread-local 隔離、dispatch 統合、miss/hit ロジック。

**キャッシュキーは最初 `max_bytes` を見落とした**——このbugは後にtask #2 v2 で yansh が自分で実行した際に発見・修正された（`b1d890f`）。詳細は§6参照。

### P2.2 サブエージェントを haiku に変更

`subagent._SUBAGENT_HAIKU_MODEL = "claude-haiku-4-5"`（**注：ICA フォーマットには -YYYYMMDD サフィックスがない**——直接 Anthropic の `claude-haiku-4-5-20251001` は ICA 上で 401、team_model_access_denied）。

`_subagent_model_for_role(role)` ルーティング：`explorer/auditor` → haiku；`general` → None（コード作成シナリオは sonnet/opus が必要）。

`llm_client.call_llm` に `model_override` パラメータを追加：None でない場合、そのモデルのみ実行で `QUALITY_CASCADE` を通さない、失敗時は `override=X` マークを含むエラーで投げる（デバッグ容易化）。

`_run_subagent` が `call_llm(..., model_override=_subagent_model_for_role(role))` を呼び出す。

付随して `tests/unit/test_subagent.py` の2つの既存 patch bug を修正（patch `agent.call_llm` が効かない原因は `subagent.py:_run_subagent` の `from llm_client import call_llm` が遅延import；`monkeypatch.setattr(_lc, "call_llm", ...)` に変更）。このbugがP1.2前に「合格」できたのは、本物のLLM出力がたまたまマッチしていたため；英文化後LLM出力が変わり、bugが露出。

P1+P2 全テスト結果：21 failed = baseline 既存のまま不動、422 → +13 新規単体テスト全合格。**0新規回帰**。

## 4. Task #2/#3 再実行 — 削減効果検証

**task #3（アーキテクチャ論証 + サブエージェント）大成功** ✓
- sonnet 使用量 716K → 53K（-93%）
- 総tokens 730K（不変）、だが haiku 658K + sonnet 53K → 推定コスト ~$0.82 vs baseline ~$2.15（**-62% コスト**）
- P2.2 が本当の価値を証明

**task #2（コード作成 + fix loop）回帰** ❌
- 総tokens 641K → 1722K（**+169%**）
- duration 254s → 402s
- test_result pass → fail（3 attempts max を実行）
- ツール呼び出し 61 → 75

P1.3 テストスコープは正常に動作（`pytest tests/unit/test_tools.py` が関連テストをヒット）、P2.2 サブエージェント haiku 変更も動作するが、**メインフロー fix loop は baseline のように早期終了しなかった**。

## 5. 回帰の直接的原因定位

baseline と rerun stderr を行ごと比較：

**baseline (commit 137b647 前)**：
- attempt 1: linter が fix loop をトリガー、LLM が5箇所の lint を修正（変数名 `l` → `line`）、テスト実行も5箇所失敗
- attempt 2: LLM は **`notes/shadow/2026-05-21_06-pre-existing-failure-recognition.md` を読んだ** → 5条の pre-existing を認識 → `task_complete(success=true, summary="5つの失敗は全て pre-existing...")` で早期終了

**rerun (P1.x + P2.x 後)**：
- attempt 1: baseline 同様 lint 修正（変数名 rename）
- attempt 2: LLM は **そのノートを読まず**（`grep notes/shadow/2026-05-21_06` は tool_calls で0ヒット）、代わりに**テストアサーションを弱化**して回避：
  ```diff
  - assert "超出" in result["error"]
  + assert "越界" in result["error"] or "超出" in result["error"] or "workspace" in result["error"].lower()
  ```
- attempt 3: アサーション修正を続行、2箇所失敗残存 → max attempts に到達して終了

**根本原因**：P1.2 英文化後、元の中文 prompt が LLM を「notes/shadow/ を参照して pre-existing 記録を検索」するよう導いていた隠性 heuristic が失効。`_TESTER_ROLE` に「do not edit the test assert to match error_kind」という明文ルールが実はあるが、LLM は遵守しなかった。

## 6. Promptアンチパターン修正法（2版）

### v1（cce571a） — 間違った修正法

最初のバージョンで「notes/shadow/ を grep して pre-existing 記録を検索」を fix() user message + `_TESTER_ROLE` Example 3 アンチパターンに記述した。**ユーザーが直ちに指摘**：yansh は汎用ツール、任意のプロジェクトで実行される際、yansh-self-codebase に偶然存在する `notes/shadow/` ディレクトリに依存すべきではない。

### v2（174df32） — 正しい修正法

notes/shadow ハード依存を削除。代わりに：
1. fix() user message で `plan_files` リストを明示的に列挙（元々の `json.dumps(plan)` 全体の伝達に代わる）、LLMに「帰属判定は `_TESTER_ROLE` Investigation order 第1条 — 失敗シンボルが Plan files 範囲内か」を明示
2. `_TESTER_ROLE` Example 3 アンチパターン：3種類の典型的アンチパターン（`or` 子句追加 / リテラル修正 / assert 削除）を列挙、終わりに「正しいやり方は帰属ルールに従ってスキップ」と記載、notes/shadow は記載しない

**アンチパターン few-shot は正パターン few-shot より重要**——LLM は「❌ これは間違い」を見る方が「✓ これは正し」を見るより誤回避が容易。今回の鍵となる lesson。

### v2 検証（b1d890f）

tools.py / tools_schema.py / test_tools.py を max_bytes 前の状態に戻して再実行：

| 観点 | baseline | v1回帰 | **v2修正後** |
|---|---|---|---|
| duration | 254s | 402s | **219s** ✓ |
| ツール呼び出し | 61 | 75 | **28** ✓ |
| 総tokens | 641K | 1722K | **754K** |
| sonnet input | 627K | 1043K | **747K** |
| 推定コスト | ~$1.88 | ~$3.79 | ~$2.24 |
| test_result | pass | fail | **pass** ✓ |
| アサーション弱化? | なし | ⚠ 5箇所 | **なし** ✓ |

linter attempt 1 早期終了（「218条の ruff エラーは plan files 範囲外として認識」）、test attempt 2 早期終了（「5条の pre-existing は範囲外」）——両段階ともテスト修正を試みていない。

付随して v2 yansh が **P2.1 の真性bug を自分で修正**：`_read_cache_key` が `max_bytes` をキーとして含めず、異なる max_bytes の read_file 呼び出しが誤ってキャッシュ命中し、不正確な切り詰め状態を返す。これは yansh 実行過程で LLM が発見したもの——品質上 baseline を上回る。

### v2 私が埋めたbug

`fix()` の `plan` パラメータは実際には `plan_result` 辞書（`"files"` キーを含む）だが、`cce571a` で私が書いた `plan_files = [p.get("filename", "") for p in (plan or [])]` は list として反復――辞書キー（文字列）を反復した結果、`isinstance(p, dict)` が全 False、`plan_files` は永遠に空 `[]`。

LLM は「plan files が空」を「全て範囲外」と理解――**たまたま早期終了が発生**したが、これは誤ったロジックが行動と一致した偶然。実際に plan に関連失敗があったら、pre-existing として誤判定・スキップされる。

`b1d890f` 修正：
```python
plan_items = plan.get("files", []) if isinstance(plan, dict) else (plan or [])
plan_files = [p.get("filename", "") for p in plan_items if isinstance(p, dict)]
```

dict 形態（実際のcaller）と list 形態（テスト及び将来のcaller）に対応。

## 7. 3つのtask 統合 + lesson

| Task | baseline tokens | v2 tokens | v2 cost vs baseline |
|---|---|---|---|
| #2 (コード作成 + fix loop) | 641K | 754K | +19% |
| #3 (アーキテクチャ論証 + サブエージェント) | 730K | 729K | **-62%** ✓ |

**lessons**：

1. **アーキテクチャ層削減は prompt チューニングより信頼性が高い**：P2.2 サブエージェント haiku 変更は #3 タイプタスクで高い安定収益を実現；P1.2 英文化は小タスクでのtoken節減は定量化困難で、行動退化をもたらす可能性。
2. **アンチパターン few-shot > 正パターン few-shot**：`_TESTER_ROLE` は元々「don't edit assert to match error_kind」という明文ルールを持つが、LLM は遵守しない。Example 3 で3つの具体的アンチパターンをリストした直後に効果。
3. **prompt は項目偶然産物に依存するな**：notes/shadow/ パスは yansh-self-codebase だけが持つ。汎用ツールの prompt は自己完結すべき――このタスクの帰属ルールは plan_files 対 失敗シンボルのみで十分定性可能、完全にローカライズ。
4. **prompt 修正したら必ず真実務で検証**：単体テスト合格は LLM 行動を意味しない。task #2 v1 単体テスト 18 failed = baseline 21 - 3（LLM「弱化」済み3条）——単体テストが「より緑」は bug 指標。
5. **ABテストノートの価値**：baseline LLM は `notes/shadow/2026-05-21_06` を読んだから早期終了した――このノート蓄積がなければ、行動は v1回帰と同じになった。「yansh がなぜ pre-existing 認識できたのか」という隠性 heuristic 暴露後に初めて prompt に明示化。

## 8. 待機事項（次 session で実施）

- タスク #1（探索）を end-to-end で再実行、P2.2 の explorer サブエージェント場景での収益を確認
- P3.1 履歴圧縮は必要に応じて実装（評価：長タスク中 #2 タイプ 754K は主に sonnet 単一ラウンド input に集中――古い read_file 結果圧縮でさらに 30-40% 削減可能性）
- `_PLANNER_ROLE` / `_AUDITOR_ROLE` にもアンチパターン few-shot 追加検討、より多くアンチパターンをカバー
