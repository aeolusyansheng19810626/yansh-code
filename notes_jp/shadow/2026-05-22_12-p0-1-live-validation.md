# P0 #1 実操検証：yansh が自動的に階層索引でスコアリング

[./2026-05-22_11-hierarchical-symbol-index.md](./2026-05-22_11-hierarchical-symbol-index.md) に続く。
ノート _11 で階層索引コードを完成させた後、yansh-code 自体を workspace として 2 回 audit を実行して検証：

- 新しいデフォルト（top モード）インジェクションが本当に prompt を圧倒しないか
- LLM が主動的に `directory_summary` / `workspace_symbols(path=...)` で深掘るか
- audit フローが依然としてスムーズか

モデル：claude-sonnet-4-6；workspace：yansh-code 自体；mode=audit。

## 実験 1：テストファイル一覧

**タスク**：「tests/unit/ ディレクトリに存在するテストファイルは何か？その中で test_audit.py はどのような機能をテストしているか？」

**LLM の選択**：

| ラウンド | ツール呼び出し |
|---|---|
| 1 | `glob_files(pattern="tests/unit/*.py")` + `list_symbols(file_path="tests/unit/test_audit.py")` |
| 2 | `task_complete(success=true, summary="...")` |

duration 17.67s、attempts=0。

**観察**：
- LLM は完全に `directory_summary` / `workspace_symbols(path="tests/unit")` を使わなかった——最も馴染みのある `glob_files` + `list_symbols` を選択
- これは実は**合理的**——トップレベルのインジェクションで `tests/ (23 py / 265 sym)` を既に伝えているため、glob_files で一覧を取得する方がより直接的
- レポートは完全：10 個のファイル + test_audit.py の 19 個のテスト関数 + 機能領域の分類

## 実験 2：ディレクトリ全体構造

**タスク**：「notes/shadow/ ディレクトリの全体構造を見せてほしい：ファイル数、拡張子分布、キーファイル（README など）、サブディレクトリの有無」

**LLM の選択**：

| ラウンド | ツール呼び出し |
|---|---|
| 1 | `directory_summary(path="notes/shadow")` |
| 2 | `task_complete(success=true, summary="...")` |

duration 11.14s、attempts=0。

**観察**：
- LLM は**1 回のツール呼び出しで `directory_summary`** を使用し、すべての必要な情報を取得（ファイル数 11 / 拡張子 .md×11 / サブディレクトリなし / キーファイルなし）
- タスクの説明と新しいツールの説明が**高度に一致**——LLM は自然とこれを選択
- 出力レポートにはテーブル + 完全なファイル一覧を含む

## 重要な結論

**新しいツールは LLM に強制するのではなく、タスク一致時に自然に選ばれる**：

- 実験 1：「一覧 + シンボル」が必要な場合、glob_files+list_symbols が最も直接的なパス——LLM は新ツールを使わなかった、**これは正しい**
- 実験 2：「ディレクトリ全体認識（ファイル数/拡張子分布/キーファイル/サブディレクトリ）」が必要な場合、directory_summary の説明と完全に一致——LLM は直接使用、1 ラウンドで解決

これは以下を意味する：

1. **トップレベルのインジェクションで十分**——LLM が `tests/ (23 py / 265 sym)` のようなサマリーを見た後、自主的に深掘りのパスを判定できる
2. **新しいツールは補完、代替ではない**——list_symbols / glob_files などの既存パスを保持しても競合しない
3. **prompt で無理にツールを推売する必要がない**——ツールの description に適用シーンを明確に書けば十分

## 全体的な収益

| 側面 | 以前 | 現在 |
|---|---|---|
| audit システム prompt インジェクション体量 | 12,975 chars（全 40 ファイル全シンボル） | 3,314 chars（トップレベル 12 ファイル + 2 サブディレクトリサマリー） |
| 削減 | — | **74.5%** |
| 大規模プロジェクトが実行可能か | 3000 ファイルで直接 200K を圧倒 | トップレベル + 必要に応じて深掘り、規模無関係 |
| LLM の行動 | シンボル表全体を一目見てから特定のファイルに潜入 | トップレベル → 適切なツールで深掘り |

## 評価

実操検証は P0 #1 の修正が**実施すべきこと**であることを示す——既存の利用可能なツール（glob_files/list_symbols は依然利用可能）を破壊しない一方で、「ディレクトリ全体認識」という以前は直接表現できなかった要求に対して効率的な通路を開いた（directory_summary で 1 ラウンド解決）。

prompt の強化と loop による対策は P0 #3 段階の方法論；この段階は**ツール層の情報密度最適化**——「全木をスキャンして prompt に詰め込む」という粗暴な方式を「必要に応じた取得 + サマリー優先」に変更。両段階を合わせることで yansh が大規模プロジェクトで本当に実用的になる。
