# 2026-05-21 P0 #3 エラー回復ループ——インフラストラクチャの実装、task_complete prompt の強化待ち

## 背景

ROADMAP P0 #3「エラー回復ループ」は従来まったく未着手だった。現在の根本的な問題（[ROADMAP](../../ROADMAP.md) §P0_3）：

1. fix() 6ラウンド、audit() 8ラウンドのハードリミット——複雑なタスクが常に最後の一歩前で打ち切られる
2. token 予算保護がない
3. エラーレスポンスが単一の `{"error": str}` ——LLMが transient/permanent を区別できない
4. 「主動的な終了」チャネルがない：LLM が終了したければ「サイレント終了」しかできず、「できない」と明示する方法がない

## 実装

commit `7d1b399 feat: P0 #3 エラー回復ループ——task_complete + token 予算 + error_kind の標準化`

### インフラストラクチャ

- **`task_complete(success, summary)` ツール**：`{"_task_complete": True, ...}` sentinel を返す；
  fix/audit ループがこれを認識して終了する。`READONLY_TOOL_NAMES` にも含まれる（audit が使用する必要がある）
- **ソフトリミット + token 予算**：fix 6→12、audit 8→16；ループ開始時に token の開始点を記録し、
  60K(fix)/120K(audit) を超えたときに messages に system の通知を注入「できるだけ早く task_complete で終了してください」、
  警告は一度だけ
- **`error_kind` の完全な標準化**：`ERROR_KINDS = {invalid_args, not_found, permission,
  security, timeout, transient, internal}` + `_err(kind, msg)` ヘルパー、
  21 個のツールの全エラーリターンポイント（約 36 箇所）に適用。**後方互換性**：`error` キーを保持、
  `error_kind` フィールドのみ追加——古い呼び出し元が `result["error"]` を読むときも動作する
- **fix() に interrupt チェックを追加**：audit() はすでに持っていたが、fix() では漏れていた

### 検証

- 13 個の新単体テストがすべてパス（task_complete sentinel + 各 kind 分類テスト）
- `run_unit.py` 9/9 ファイルがパス
- 5 個の pre-existing 失敗は変わらず

## 統合実行で発見された問題

タスク A（list_files に max_depth を追加）を実行して統合検証：

| メカニズム | 結果 |
|---|---|
| token 予算警告 | ✅ トリガー（fix が 60530 token に達したとき正しく通知が注入された）|
| fix() に interrupt チェックを追加 | ✅ 追加されたがこの実行ではトリガーなし |
| ソフトリミット 12 | ⚠️ 有効だが LLM が満杯まで実行 |
| `task_complete` 主動呼び出し | ❌ Sonnet が主動的に呼ばなかった、fix が 12 ラウンド満杯で強制終了 |

### 副作用：誤導的な prompt が LLM にテスト期待値を修正させた

最初の `_TESTER_ROLE` に「error_kind に基づいて決定」セクションが追加された：
> `transient`/`timeout` は 1 回再試行可能；`invalid_args` はパラメータを変更して再呼び出し；
> `not_found` は最初にパスまたはシンボル名のスペルを確認；`permission`/`security` は回避しない、
> `task_complete(success=False, ...)` を呼び出す

**結果は逆効果**：Sonnet が pre-existing 失敗 `assert "超时" in result["error"]` を見たとき、
ツールが `error_kind="security"` を返す（python -c ブラックリスト）と、task_complete を呼ばず、
**テスト assert を error_kind に一致するように修正**：
```diff
-    assert "超时" in result["error"]
+    assert result.get("error_kind") == "security" or "超时" in result["error"]
```

これは [_05](./2026-05-21_05-four-templates-validation.md) テンプレート 4 スコープ制御と
[_06](./2026-05-21_06-pre-existing-failure-recognition.md) の pre-existing 認識に違反する。

### 修正

`_TESTER_ROLE` の「error_kind に基づいて決定」セクションを**削除**し、反対警告に置き換え：
> error_kind フィールドはエラーの**分類ラベル**に過ぎない（再試行するか放棄するか判断するため）、
> **テスト期待値を修正する根拠ではない**——pre-existing テストが "超时" を期待しているが
> ツールが security エラーを返す場合、帰属ルールに従ってこの失敗をスキップし、
> **テスト assert を error_kind に一致するように変更しない**。

この統合実行で yansh が追加したすべての変更をロールバック（max_depth 実装、
テスト期待値の修正、agent.py の未使用 imports 削除、`l → ln` の美化）、
自分の P0 #3 の変更のみを保持。

## 重要な観察

### 1. error_kind フィールドは LLM にとって両刃の剣

このフィールドを追加した本来の意図は、LLM が `transient` を見て再試行が必要なこと、
`permission` を見て放棄が必要なことを理解させること。**しかし LLM は「情報を利用する」のが大好きだ**——
新しいフィールドを与えると、それをコードに組み込もうとする、間違った場所も含めて
（「security を返したなら、テスト assert を security に変更しよう」）。

教訓：**LLM に新しいフィールドを追加しても、LLM の行動が改善するわけではない**。
いつ使うべきか、いつ使うべきでないかを明確に伝える必要がある。**反対警告**
（「X の根拠ではない」）は時として正向きのガイダンス（「X を使って Y する」）より重要。

### 2. task_complete は単なる prompt では不十分

追加した prompt は：
> 完了または継続不可の場合、**`task_complete(success, summary)` を呼び出して明示的に終了**。
> サイレント終了（このラウンドでツールを呼ばない）= デフォルト成功；success=False の明示的宣言は「できない」を表現するため。

Sonnet は意味は理解したが**使わなかった**——古いパターン（サイレント終了/ハードリミット満杯）に従い続けた。
考えられる理由：
- 「サイレント終了 = デフォルト成功」が Sonnet に必要ないと思わせた
- task_complete が訓練分布にない（ChatGPT の OpenAI function calling にないツール）
- 6→12 ラウンドのソフトリミットで「まだ余裕がある」と感じさせた

次ラウンドの prompt 強化方向：
- **「サイレント終了 = 成功」の説明を削除**——LLM に task_complete を呼ばないと完了しないと思わせる
- **task_complete に few-shot の例を追加**——1～2 個の具体的なシナリオの示範を与える
- **fix/audit prompt の冒頭で強調**「必ず task_complete で終了」、末尾ではなく
- または：fix ループが LLM のこのラウンドがツールを呼ばなかった（サイレント終了）を検出したとき、
  主動的に問い合わせ「完了しましたか？task_complete で確認してください」——1 度のチャンスを与える

### 3. yansh の agent.py unused imports 削除は正しいが、このタイミングではだめ

ruff が `threading / difflib / time as _time / from openai import OpenAI`
をすべて F401 unused として報告。yansh が ruff エラーを見て主動的に削除。**機能的には yansh が正しい**——
これらの imports は確かに使われていない；しかしタスク A は list_files の変更で、ruff エラーはそれとは無関係、
スコープ蔓延。

教訓：**linter エラーも pre-existing 失敗の一種**。テンプレート 4 は linter
失敗の「帰属判定」を明確に含めるべき。次ラウンドの prompt 強化時に一緒に処理。

## 一言でまとめると

**P0 #3 インフラストラクチャレイヤーの実装**——`task_complete` ツール、token 予算警告、
`error_kind` の標準化、ソフトリミット——しかし **Sonnet が統合実行で task_complete を主動的に呼ばなかった**。
次がすべきことは新しいツールを追加することではなく、**prompt を調整して既存ツールが本当に使われるようにする**こと。

## フォローアップ

- 次ラウンド prompt 強化で task_complete が本当に呼ばれるようにする（上文 §2 の方向参照）
- テンプレート 4 に linter 失敗の帰属判定を追加（上文 §3 参照）
- ROADMAP P0 #2 の prompt チューニング継続イテレーション内に保留
