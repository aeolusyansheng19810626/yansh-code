# 2026-05-21 4つのタスクテンプレート prompt 検証（dbc25e2）

## 実験設計

`_CODER_ROLE` を4つの具体的なシナリオテンプレート（commit `dbc25e2`）にアップグレードし、従来の単一の「フルパイプライン認識」ルールに代わるもの。
4つのテンプレートはそれぞれ以下に対応：

1. **署名変更**：dispatch テーブル + ドキュメント + 呼び出し元
2. **新規ツール追加**：tools.py 実装 + tools_schema.py + agent.py dispatch + readonly_handlers
3. **再帰的枝刈り**：`dirs.clear() + ループ進入後判定` を禁止、「先に枝刈りしてから列挙」を要求
4. **スコープ制御**：タスク説明の機能のみを変更し、ついでのリファクタリングを禁止

2つのタスク（A: list_files に max_depth を追加；B: 新規 count_lines ツール追加）を通じてこれら4つのテンプレートをカバーし、
yansh と同じモデル Sonnet subagent をそれぞれ1回ずつ実行して比較。

## 結果マトリックス

### タスク A: list_files に max_depth パラメータを追加

| 次元 | yansh (Sonnet, dbc25e2) | Sonnet subagent baseline |
|---|---|---|
| 実行時間 | 272s | 144s |
| ツール呼び出し | 複数ラウンド（3回の失敗リトライ） | 19 |
| テンプレート1（dispatch）| ✅ `agent.py:864` を `list_files(**args)` に変更 | ❌ 変更なし（subagent テストが dispatch を通さない） |
| テンプレート3（枝刈り）| ✅ `dirs.clear() + continue` はファイル列挙**の前**に実行 | ✅ `dirs.clear()` パターンを使用 |
| テンプレート4（制御）| ❌ **スコープ クリープ**：`_DANGEROUS_PATTERNS` から `python -c` を削除、`_validate_path` のエラーメッセージを書き直し | — |
| タスク判定 | 失敗（max_depth テストが失敗、他の部分をついでに変更したため） | 完了 |

### タスク B: 新規 count_lines ツール追加

| 次元 | yansh (Sonnet, dbc25e2) | Sonnet subagent baseline |
|---|---|---|
| ツール呼び出し | 複数ラウンド | 14 |
| 実行時間 | 約3分 | 約3分 |
| テンプレート2（3つ組） | ✅ tools.py + tools_schema.py + agent.py（import + readonly_handlers）| ✅ 3箇所すべて対応 |
| テンプレート4（制御）| ✅ `_validate_path` を再利用、他ファイルをついでに変更しない | ✅ 5つの pre-existing 失敗を報告したが**修正しなかった** |
| タスク判定 | 完了 | 完了 |

## 主要な観察

### 4つのテンプレートのうち3つが機能、1つが違反

- **テンプレート2（新ツール3つ組）大成功**：yansh はタスク B で tools.py / tools_schema.py / agent.py の
  import + readonly_handlers の4箇所を直接ターゲット、漏れなし。これは**初めて** yansh が新規ツール追加タスクで
  subagent と同等になった。
- **テンプレート1（dispatch）は引き続き機能**：yansh は list_files の署名変更後、主体的に `agent.py:864` を変更、
  一方 subagent は変更しなかった（彼らのテストが tools を直接 import するため dispatch を通さない；これは subagent
  テスト設計の限界を露呈）。
- **テンプレート3（枝刈り順序）機能**：yansh が今回書いたのは `if depth >= max_depth: dirs.clear(); continue`
  が `for f in files` **の前**で、もう off-by-one で引っ掛からない。
- **テンプレート4（制御）失敗**：yansh がタスク A で**相変わらずついでに** `_DANGEROUS_PATTERNS`
  と `_validate_path` のエラーメッセージを変更——これら2つは max_depth と完全に無関係。

### テンプレート4失敗の誘因

タスク A の作業ツリーにはもともと5つの pre-existing 失敗ケース（`test_execute_command_timeout`、
3つの path-traversal、1つの diff truncation）がある。yansh の review/fix ループが失敗を見た時点で、
**自分が引き入れたと誤解**し、`_DANGEROUS_PATTERNS` とエラーメッセージを「修正」して対応。

テンプレート4の現在の措辞はまだ十分でない——「タスク説明の機能のみを変更」と言っているが、
**「自分が引き入れていない失敗を見た時は識別して手を引く」**とは言っていない。これは prompt に補足が必要な反向警告。

## 前回（cca5d03 → dbc25e2）との比較

| タスク | cca5d03（単一フルパイプラインルール）| dbc25e2（4テンプレート）|
|---|---|---|
| dispatch 修正 | ✅ | ✅ |
| max_depth 実装の正確性 | ❌ off-by-one で再度失敗 | ✅ 今回は正確 |
| 無関係なコード変更 | ✅ パスセパレータを変更 | ✅ `_DANGEROUS_PATTERNS` を変更 |
| 新ツール3つ組 | 未テスト | ✅ タスク B で全対応 |

純進捗 +1：**テンプレート3（枝刈り）が off-by-one を解決**。
純後退 0、ただし**テンプレート4（制御）はまだ実際に機能していない**、違反方法が「パスセパレータ変更」から「dangerous patterns 変更」に変わった。

## 今後の対応

1. **テンプレート4を強化**：`_CODER_ROLE` に1文追加——「失敗ケースを見つけた時は、まずそれがこのタスク
   の機能によるものかを確認する；現在のタスクと無関係なら、**記録してスキップ**し、修正を試みないこと」。
2. **新テンプレート追加は不要**：4テンプレートは大部分の高頻度な陥阱をカバー、追加すると prompt が冗長になる。
3. これら2つはいずれも P0 #2 サブタスク続行。

## 一言要約

**4テンプレート prompt により yansh は「新ツール3つ組」と「再帰的枝刈り順序」の2項目で subagent レベルに達したが、
「スコープ制御」は実際には学習していない——相変わらず pre-existing 失敗を自分が引き入れたと誤解して修正を試みる。**
