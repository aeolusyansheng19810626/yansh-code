# 2026-05-21 テンプレート4 + _TESTER_ROLEの強化：pre-existing失敗の識別

## 背景

前回のテンプレート4検証（[_05ノート](./2026-05-21_05-four-templates-validation.md)）で発見：
テンプレート4（スコープ抑制）がタスクAで失敗——yanshは5つのpre-existing失敗ケースを見て、
**自分が導入したと勘違いして**、`_DANGEROUS_PATTERNS` と `_validate_path` のエラーメッセージを
「修正」してしまい、プロダクトコードを汚してしまった。

5つのpre-existing失敗の根本原因はすべてmax_depthタスクと無関係：
- `test_execute_command_timeout`：assert「タイムアウト」しかし `python -c` は既に `_DANGEROUS_PATTERNS` に含まれており、メッセージが「セキュリティブロック」に変わった
- 3つのpath-traversal テスト：assert「超過」/「exceeds workspace」だが、`_validate_path` のメッセージが「パス越界」に変更された
- `test_build_diff_lines_exactly_50_no_truncation`：トランケーション閾値の定義が調整されている可能性

## prompt の改動

2つの強化：

**`_CODER_ROLE` テンプレート4** の末尾に追加：
```
- 失敗ケースは必ずしもあなたが導入したものではない：テスト実行で赤を見たら、
  まずその失敗assertが参照する関数/定数がこのplanで列挙されたファイル内の
  シンボルかどうかを確認する——関係ないもの（例えば今回はlist_filesを変更したのに
  test_execute_command_timeoutが失敗している）はおそらくpre-existing失敗であり、
  報告書には記録するがプロダクトコードを「修正」しようとするべきではない
```

**`_TESTER_ROLE`** の調査順序の最前に「まず所有関係を識別する」を挿入——
fixプロセスが使用する _TESTER_ROLE はここが最重要ポイント：
```
1. まず所有関係を識別する：失敗assertが参照するシンボルがこのplanで列挙されたファイル内にあるか？
   - はい → このタスク内で導入された失敗、プロセスを続ける
   - いいえ → おそらくpre-existing失敗、修正をスキップし、最終報告書で列挙してユーザーの判断に任せる
```

## 設計のポイント

以前の [_04 yanshがClaude Codeを超える ノート](./2026-05-21_04-yansh-beats-claude-code.md) と同じ型：
**具体的な形で具体的な問題に対処する**——
「pre-existing失敗を区分する」という抽象的な原則ではなく、「失敗assertが参照するシンボルが
このplan内で列挙されたファイルのシンボルかどうか」という**機械的に判定可能な** ルール。

LLMがこのルールを取得すれば、それを機械的に実行できる：
1. 失敗assertがどの関数/定数名を参照しているかを確認する
2. その名前がplanのファイル内にあるかどうかを確認する
3. ない → スキップ

LLMが「それはpre-existingであるかどうかを判断する」必要はなく、単に文字列マッチングを行うだけでよい。

## 検証

タスクA（list_filesにmax_depthを追加）を再実行。

| 次元 | 今回（新prompt） | 前回（dbc25e2） |
|---|---|---|
| `_DANGEROUS_PATTERNS` に触れた | ❌ 触れず ✅ | ✅ `python -c` を削除 |
| `_validate_path` エラーメッセージに触れた | ❌ 触れず ✅ | ✅ メッセージを変更 |
| `_build_diff_lines` トランケーション ロジックに触れた | ❌ 触れず ✅ | — |
| max_depth実装 | ✅ 枝刈り後に列挙 | ✅ |
| 4つのmax_depthテスト | ✅ 全て成功 | ✅ |
| pre-existing 5件の失敗 | 5件は失敗のまま（修正なし）| 相変わらずfail |

**5つのpre-existing失敗は失敗のままだが、プロダクトコードは全く触っていない**——これが質的転換。

## 唯一の瑕疵

fix ループの第6ラウンドでトリガー上限に達する前に、yanshが `replace_in_file` を使用して
`test_build_diff_lines_modify` 内のループ変数 `l` を `line` に変更（化粧直し）。
「既存の変数名を変更しない」という規則に違反したが：
- 変更したのはpre-existing テスト自体（修正すべきプロダクトコードではない）
- 何も破壊しない（`l` と `line` は等価）
- 5つのpre-existing失敗の状態に影響しない

「何かやりたいが合理的なターゲットが見つからない」というフォールバック動作。許容範囲内。

## 未処理：dispatch は変更なし

今回のarchitect plan フェーズで `agent.py` をplanに含めなかったため、
dispatch（`agent.py:866` の `list_files()`）が `list_files(**args)` に変わっていない。

しかし、これはテンプレート1 / `_ARCHITECT_ROLE` の問題であり、**この強化の対象ではない**。
また、max_depth=None のデフォルト値により、既存のLLM呼び出しはすべて正常に動作する
（max_depthは使用できないだけ）。

次のラウンドの課題。

## 一言でまとめると

**「失敗assertが参照するシンボルがplanファイル内にあるかどうか」という機械的に判定可能な
逆方向の警告1つで、fix ループ内でyamshがpre-existing プロダクトコードに触れる回数を
N回から0回に削減した**——これはテンプレート4がfix段階で初めて真の効果を発揮した。

## commit

`34f22ce feat: テンプレート4 + _TESTER_ROLEの強化——pre-existing失敗とこのタスクで導入された失敗を区分`
