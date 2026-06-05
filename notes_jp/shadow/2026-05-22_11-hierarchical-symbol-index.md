# P0 #1 階層型シンボルインデックス

ROADMAP P0 #1 3つの未実装項目を一緒に完成させる。

## 変更内容

### 1) workspace_symbols を階層型対応に変更

`workspace_symbols(extensions=None, path=None, recursive=False)`

- **デフォルト top モード**（path=None, recursive=False）：トップレベルファイルシンボルのみ + サブディレクトリサマリー表示
  （py_files / total_symbols カウント）
- **path ドリルダウン**（path="sub/dir"）：該当ディレクトリのトップレベルファイルシンボル + サブディレクトリサマリーを返す
- **recursive=True**：従来のフルスキャン動作。大規模プロジェクトでは注意が必要

返却構造に `mode: "top" | "deep"` を追加して区別。新たに `subdirs` フィールドを追加。空のサブディレクトリ（.pyなし）は除外。
subdirs でノイズを減らす。

`_parse_symbols_cached`、`_AST_CACHE`、`_WORKSPACE_SYMBOLS_IGNORE` を再利用——変更なし。

新規内部ヘルパー `_dir_symbol_count(dirpath, exts)`：特定ディレクトリの .py ファイル数 + シンボル総数を再帰的に統計。

### 2) 新規ツール directory_summary(path=".") を追加

特定ディレクトリの全体概況を返す：

```python
{
    "path": "src",
    "file_count": 12,
    "subdir_count": 3,
    "by_extension": {".py": 8, ".md": 2, ".json": 2},
    "key_files": ["README.md", "pyproject.toml"],
    "subdirs": ["agents/", "tools/", "tests/"],
    "files_sample": ["main.py", "agent.py", "...", "..."],
}
```

非再帰的——直下の項目のみ対象。`key_files` の候補リストは一般的なマーカーをカバー（README/pyproject/setup.py/
Makefile/Cargo.toml/go.mod/package.json/CLAUDE.md/ROADMAP.md/.agent_rules など）。

エラー：パス越界 → permission；ディレクトリ不在 → not_found；ディレクトリではない → invalid_args。

### 3) audit() をトップレベル注入に変更

agent.py:1349-1367 を書き直し：デフォルトで `workspace_symbols()` でトップ構造を取得、トップレベルファイルシンボル +
サブディレクトリサマリー + 「path= で深掘」のワンラインヒントをレンダリング。

`_AUDITOR_ROLE` プロンプトに以下を追加：
> 注入されるのはトップレベル構造です。特定ディレクトリを詳しく調べるには `workspace_symbols(path="...")` または
> `directory_summary(path="...")` を使用してください。**一度に全ツリーを取得しないでください**（recursive=true は大規模プロジェクトで context を圧迫します）。

### 4) tools_schema.py を同期

- `workspace_symbols` の説明を階層型セマンティクスを反映するように変更；`path` / `recursive` パラメータスキーマを追加
- 新規 `directory_summary` スキーマを追加
- `READONLY_TOOL_NAMES` に `directory_summary` を追加

### 5) agent.py ディスパッチ登録

import + readonly_handlers に `directory_summary` を追加。

## 統合検証（yansh-code 自身）

| モード | 文字数 | ファイル数 | シンボル数 |
|---|---|---|---|
| top（新デフォルト） | **3,314** | 12 トップレベル + 2 サブディレクトリサマリー | 171 トップレベル |
| deep（従来のデフォルト） | 12,975 | 40 全ツリー | 448 |

**74.5% 削減**——これはわずか 40 ファイルの中規模プロジェクトです。3000 ファイルの大規模プロジェクトでは、比率推定で deep モードは
200K context ウィンドウを直接圧迫します。

## ユニットテスト

`tests/unit/test_audit.py` 19件すべてパス：
- 従来の 4件 deep 動作テストケース：`recursive=True` を追加してアサーション復元
- 新規 7件 workspace_symbols：top のみ / ネストされた再帰計数 / path ドリルダウン / 空サブディレクトリスキップ /
  パス越界 / 不在 / ディレクトリではない
- 新規 5件 directory_summary：基本形態 / path パラメータ / パス越界 / 不在 /
  ディレクトリではない / files_sample トランケーション

`python tests/run_unit.py`：10/10 ファイルパス。

## 重要な設計判断

**デフォルト動作を破壊する理由と `mode="top"|"deep"` パラメータを使わない理由**：
- ROADMAP の第 3 項が明確に「audit は全量サマリーを事前注入しない」ことを目指している——デフォルトを変更してはじめて audit が自動的に恩恵を受ける
- LLM が見るツールの説明はデフォルトを優先使用する——パラメータを追加してデフォルトが全量のままでは変更と同じ効果がない
- 従来の呼び出し元（test_audit.py）が明示的に `recursive=True` でマークすることは、「暗黙的に全量を継承」するより意図が明確

**なぜサブディレクトリサマリーは再帰的カウントするのか**：
- LLM への情報密度が十分：「tests/ は 23個の .py / 265個のシンボル」が 1 行で深掘するかどうか判断できる
- `_AST_CACHE` にほぼゼロコストでヒット。スキャン開業の心配不要
