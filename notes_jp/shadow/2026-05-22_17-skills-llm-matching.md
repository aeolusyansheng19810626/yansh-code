# Skills LLM インテリジェントマッチング（P2 #8 続き）

[_16](./2026-05-22_16-skills-system.md) を受け継いで：ユーザーが「GitHub の skill をインポートできるのか」と言及したことから、
「モデル自身に skill を呼び出すべきかどうかを判断させたい」という質問へと進みました。今回はこれを追加します。

## 何が変わったか

### skills.match_skills が階層的決定へアップグレード

```
1. mode フィルタリング → 候補リスト candidates
2. candidates が空 → [] を返す（LLM を呼ばない）
3. キーワードマッチング（match_skills_keyword）→ ヒット時は fast path を返す（LLM を呼ばない）
4. use_llm=True → _llm_select_skills を呼ぶ
5. use_llm=False → [] を返す（後方互換性/テストシナリオ）
```

短絡設計の目標：**最も一般的な「明示的キーワードヒット」ゼロコストゼロ遅延**；キーワードがあいまいな場合のみ LLM を呼んでコストがかかります。

### `_llm_select_skills(user_input, candidates, mode)`

`llm_client.call_llm` を現在の cascade で呼び出す（強制的に Haiku に切り替えず、client 状態を変更しない）。
prompt は候補リストの (name, description, triggers の最初の 5 つの例) を与える；JSON 出力 `{"skills": ["name1", ...]}` を要求；
**無いよりマシ** —— 確実でなければ空を返す。

エラーハンドリング：
- LLM がエラーを投げる → None を返す → 呼び出し元は保守的に []を返す
- LLM が不正な JSON を返す → None を返す
- LLM が未知の skill 名を返す → 静かに破棄（`name_set` で交集合を使用）
- `` ```` ```json ... ``` ```` markdown フェンスをサポート（`_extract_json` ロジックをインラインで再利用）

### `match_skills` / `load_and_format` に `use_llm=True` パラメータを追加

後方互換性：`match_skills_keyword` 公開 API を保持；既存の呼び出しは `match_skills(use_llm=True)` で新しい動作がデフォルトです。

### 古い `match_skills` キーワード版を `match_skills_keyword` に改名

公開エクスポート、以下として機能します：
- LLM 失敗時のフォールバックパス
- テストシナリオの純キーワードモード
- ユーザーが「完全に予測可能な」デプロイを望む場合に明示的に呼び出し可能

## 検証

### ユニットテスト（tests/unit/test_skills.py、新規 10 項目追加）

- `test_match_skills_keyword_function_still_works` — 古い API は公開
- `test_match_skills_keyword_hit_skips_llm` — キーワードヒット時、spy は LLM 呼び出し回数 = 0 を表示
- `test_match_skills_no_keyword_calls_llm` — fake LLM は review を選択、入力は "code review"（triggers にはない）→ LLM が候補リストを受け取ったことを検証
- `test_match_skills_llm_returns_empty` — LLM が該当なしと主体的に判定 → []
- `test_match_skills_llm_failure_falls_back_to_empty` — `boom()` がエラーを投げる → []（クラッシュしない）
- `test_match_skills_llm_invalid_json` — LLM が不正な JSON を出力 → []
- `test_match_skills_use_llm_false_keyword_only` — キーワード未ヒット + use_llm=False → []、spy が 0 回の LLM call を検証
- `test_match_skills_no_candidates_skips_llm` — mode フィルタ後に候補がない → すぐに []を返す、spy が 0 回の LLM call を検証
- `test_match_skills_llm_filters_unknown_names` — LLM が与えた名前が候補にない → 静かに破棄
- `test_match_skills_llm_with_markdown_codeblock` — `` ```` ```json ... ``` ```` フェンスを解析可能

12/12 ファイル全体をパス；test_skills.py 30/30。

### 統合検証（ICA Sonnet 4.6）

2 つの skill を記述：
- **api-design**：description "REST API / HTTP インターフェース設計レビュー"、triggers `["api", "endpoint", "接口"]`
- **perf-review**：description "パフォーマンスボトルネックレビュー"、triggers `["perf", "performance", "性能", "慢"]`

5 つのシナリオを実行：

| 入力 | キーワードヒット | LLM 決策 | 結果 |
|---|---|---|---|
| "見てください API **インターフェース**設計はどうですか" | ✅ "接口" が fast path にヒット | LLM を呼ばない | api-design ✅ |
| "見てください コード**効率**はどうですか" | ❌ perf/性能/慢 がない | LLM が perf-review を選択 | perf-review ✅ |
| "この **HTTP サービス**設計は合理的ですか" | ❌ api/接口/endpoint がない | LLM が api-design を選択 | api-design ✅ |
| "コードスタイルレビュー" | ❌ | LLM **強制的に選ばず** 空を返す | [] ✅ |
| "効率" + use_llm=False | ❌ | LLM を呼ばない | [] ✅（後方互換性） |

**重要な証拠**：
- LLM は「文字通りのキーワード」を超えてセマンティックマッチングができる —— 「効率→パフォーマンス」、「HTTP サービス→API 設計」
- LLM は強制的に選ばない —— 「コードスタイルレビュー」が完全に無関係な場合、空を返す（これは prompt の「無いよりマシ」指令が機能している証拠）
- キーワードヒットが fast path を走る —— ゼロ遅延ゼロコスト
- use_llm=False は古いバージョンのキーワードマッチングと完全に等価

## 評価

### 前回（キーワード版）との本質的な違い

キーワード版はユーザーが trigger 語を**明示的に**言うことを要求します；LLM 版はユーザーが**自然言語**を使うだけで命中できるようにします ——
これが「Prompt as a Service」の真に実用的な形式です。コミュニティが配布する skill ユーザーは triggers を覚える必要はなく、
LLM が description を見るだけで正しいものを選べます。

### Claude Code との距離を縮める

以前 ROADMAP では「yansh キーワードマッチング vs Claude Code LLM インテリジェントマッチング」が核心的な距離でした。今回
この距離を以下に縮めます ——
- 両者が LLM インテリジェントマッチング ✅
- 両者が明示的な trigger ヒントをサポート ✅（yansh の triggers フィールドは LLM への強いキーワードヒントに相当）

残りの距離：
- Claude Code は文脈を見る（会話履歴、プロジェクトファイル、最近の変更）；yansh は現在の一文の user_input だけを見る
- Claude Code は複数の skill を結合して呼び出せます；yansh もサポートしていますが、特に最適化されていない
- Claude Code には skill 間の依存関係があります；yansh はまだない

### コスト トレードオフ

毎回のタスクに 1 回の LLM 呼び出し追加（約 200 input + 50 output tokens × Haiku 価格 ≈ $0.0003）——
現在の cascade が Sonnet 4.6 を走らせる場合、約 $0.0015。
しかし**キーワードヒットが fast path を走る**ため、このコストは 0 に低下します —— 一般的なシナリオは無料です。
Haiku を強制ルーティングの最適化として残すのは次回のもの（llm_client に `model` パラメータパスを追加する必要があります）。

次回（このスコープ外）：
- 強制的に Haiku を走らせる（この種の軽量決定に最も安いモデルを専用）
- 文脈認識（会話履歴 / project 状態を見る）
- skill 間依存：A を選んだ時、A が依存する B を自動ロード
- skill 選択キャッシュ：同じ入力は短期間に LLM を重複呼び出ししない

## 重要ファイル

| ファイル | 変更 |
|---|---|
| `skills.py` | `match_skills` を階層的決定にリファクタリング；`match_skills_keyword` 公開 API；新しい `_llm_select_skills`；`load_and_format` に `use_llm` 追加 |
| `tests/unit/test_skills.py` | +10 ユニットテスト、fast path / LLM パス / 失敗フォールバック / エッジケースをカバー |
