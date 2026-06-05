# AB Test #4: bug 再現 / 修正タスク — yansh code mode vs CC サブエージェント

**bug 題目ソース**: `d95c87d fix: P1-2 memory.find_memory パス横断（slugify + resolve ダブルチェック）`

**再現方法**: `d95c87d` の `memory.py` 部分を reverse-apply（`tests/unit/test_memory.py` の 4 つの新テストは保持）。bug 状態下：

```
$ python -m pytest tests/unit/test_memory.py
1 failed, 34 passed
FAILED test_find_memory_slugify_consistent_with_save
```

1 つの `slugify_consistent` テストのみ失敗；3 つの `path_traversal_*` テストはワークスペースに `../../README.md` などの実ファイルがないため**偶然にもパス**——bug 自体のセキュリティ問題は何も直接 PoC で暴露されていない。

**プロンプト**（両側の共通的な要件）:
> tests/unit/test_memory.py にテスト失敗があります。bug を特定して修正してください

**モデル**: Claude Sonnet 4.6（両側メインモデル）；yansh サブエージェントは haiku を使用
**日付**: 2026-05-23
**タスクタイプ**: bug 特定 + 修正（失敗テスト + テスト名が示唆するセキュリティ意図を含む）

## データ比較

| 維度 | yansh (code mode) | Claude Code サブエージェント (general-purpose) |
|---|---|---|
| 所要時間 | **87.9s** | **31.3s** |
| ツール呼び出し | **24** | **6** |
| Token (in+out) | **249K** (sonnet 226K + haiku 23K) | **63K** |
| attempts | 1（test fix loop は一度でクリア） | 1 |
| ファイル変更 | `memory.py`（1 箇所に `slugify` を追加） | `memory.py`（1 箇所に `slugify` を追加） |
| テスト結果 | 35/35 pass | 35/35 pass |
| 修正の深さ | ⚠ slugify のみ追加（resolve ダブルチェック欠落） | ⚠ slugify のみ追加（resolve ダブルチェック欠落） |

**yansh ≈ 2.8× CC の所要時間、4× ツール、4× tokens、修正方法は完全に同じ** —— 今回は CC の圧勝（速度 + コスト優位、品質は互角）。

## 修正方法の比較 vs ベースライン

ベースライン `d95c87d` 修正方法（標準解答）:
```python
def find_memory(name, workspace_dir=None):
    """...P1 セキュリティ: name は先に _slugify... resolve() + is_relative_to で再度チェック..."""
    name = str(name).strip()
    if not name:
        return None
    slug = _slugify(name)                          # ← 追加 1: slugify
    for d, scope in (...):
        if d is None or not d.exists():
            continue
        f = d / f"{slug}.md"                       # ← slug を使用
        try:                                       # ← 追加 2: resolve ダブルチェック
            f_resolved = f.resolve()
            d_resolved = d.resolve()
            if not str(f_resolved).startswith(str(d_resolved)):
                continue
        except Exception:
            continue
        if f.exists():
            return parse_memory_file(...)
```

**両方のエージェントは第 1 層（slugify）のみ追加**、第 2 層（resolve ダブルチェック）は追加しなかった。両側の修正方法は字面で一致:

```python
# yansh と CC の両方が以下に変更:
+    slug = _slugify(name)
     for d, scope in (...):
         ...
-        f = d / f"{name}.md"
+        f = d / f"{slug}.md"
```

**なぜ両方とも resolve ダブルチェックを見落としたのか？**
- bug 状態ではテストが 1 つだけ失敗（`slugify_consistent`）、3 つの `path_traversal_*` はすべてパス
- slugify を追加した後、35 条のテストすべてがパス——LLM に続ける信号がない
- テスト名 `test_find_memory_path_traversal_blocked_dotdot` は「path traversal セキュリティ」を示唆しているが、テストが既にパスしているため、LLM は過度に修正しない

これは**テスト駆動開発の死角**を露呈している：テストカバレッジ < セキュリティ意図の場合、LLM はテストが満足するまでのみ修正し、defense-in-depth を主動的に追加しない。

## yansh の 24 個のツール呼び出し分布

| ツール | 回数 | 用途 |
|---|---|---|
| `execute_command` | 10 | pytest の実行 / テスト検証 / 複数回の再実行 |
| `get_symbol_definition` | 5 | `find_memory` / `save_memory` / `_slugify` などのシンボル定義を確認 |
| `read_file` | 3 | memory.py / test_memory.py のコンテキスト |
| `task_complete` | 3 | architect/coder/tester 各 1 回の sentinel |
| `dispatch_subagent` | 1 | haiku サブエージェントをディスパッチして関連関数を探索（23K haiku tokens） |
| `list_symbols` | 1 | memory.py のシンボル一覧 |
| `replace_in_file` | 1 | 実際の修正（1 回の edit） |

**主要な観察**: yansh は `code mode` でも計画ステップを走らせていた（`[Agent: Architect]`、軽量な bug タスクだが完全なパイプラインを走破）。`P1.3 fix loop scope` の動作は正常——`pytest tests/unit/test_memory.py` で関連テストに命中、linter attempt 1 は早期終了（218 個の ruff エラーを pre-existing と認識）、test attempt 1 修正後直接 35 pass、**タスク #2 のような弱化アサーション行動は出現しなかった**。修正方法は安定。

## CC の 6 個のツール呼び出し

CC が報告した: Read 1 + Grep 1 + Edit 1 + Bash 3。

CC のパスは極めて短い: grep `find_memory` と `save_memory` → save が slugify を使っているが find が使っていないことに気付く → Edit → pytest を実行して検証。**シングルスレッド直列、plan なし / subagent なし**。

## 完了品質の差異: 今回は基本的に差がない

| 維度 | yansh | CC |
|---|---|---|
| テスト通過 | ✓ | ✓ |
| 修正方法の字面 | 一致 | 一致 |
| docstring | 元の docstring の「P1 セキュリティ」説明を削除 | 同様に削除 |
| resolve ダブルチェックに言及 | ✗ | ✗ |
| `_slugify` 関数自体に言及 | ✗（ASCII セーフ動作を維持） | ✗ |
| 追加の変更 | 0 | 0 |

**今回は task #2 のような「yansh がより深い閉環」という差異がない**——タスクが余りに局所的で、LLM は 1 つの失敗 → 1 行追加 → パス → 終了という流れ。両側のパスはほぼ同構。

## 今回のタスクの特殊性

タスク #1-3 と異なる:

- **task #1**（探索）: CC のパスが短く勝利、yansh の出力が深く勝利
- **task #2**（コード記述 + テスト追加）: yansh の深い閉環 + 付随的なクリーンアップ vs CC は 25× 安価
- **task #3**（アーキテクチャ論証）: yansh がサブエージェントをディスパッチして分解 + 行番号正確 vs CC が隠れた罠を捕捉
- **task #4**（bug 修正）: **両側の修正方法は字面で同じ、CC は 4× 安価**

**bug 修正タスク（失敗テストを含む）は逆に yansh の価値が最も薄いシナリオ**:
- 失敗テストは既に bug の位置を極小にまで圧縮
- 「小さな変更 + テスト駆動検証」は CC の強項
- yansh の plan→code→fix パイプラインはこのような小変更では overhead

## 共通の盲点: defense-in-depth

両方とも resolve ダブルチェックを見落とした。これはモデル能力の問題ではなく、**テスト駆動信号の限界**:

- 失敗テストは 1 つだけ（`slugify_consistent`）
- 通過テストは 34 個。そのうち 3 個は `path_traversal_blocked` という名前を持ちながら実際には触発しない case を含む
- LLM は「1 fail → fix → 35 pass」という明確な閉環を見て停止

LLM に resolve 防護を追加させるには、prompt が以下を必要とする:
- (a) 明示的に「defense-in-depth セキュリティ防護を追加」と指示
- (b) または path_traversal テストを真の PoC に変更（例えば `os.symlink` で クロスディレクトリを作成、resolve 後は落盤外へ）

ベースライン d95c87d は人間の + Codex review の産物で、明確に「防御層を 1 つ保つのは損ではない」とコメントしている——このような「テスト要件を超えるエンジニアリングセンス」は現時点では LLM の弱項。

## まとめ: 何をどのシナリオで選ぶ（更新）

| タスクタイプ | 推奨 | 倍率 |
|---|---|---|
| 探索 / 情報検索（task #1）| **CC** | yansh ≈ 1.5× |
| コード記述 + テスト追加（task #2）| **CC**（25× 安価） | yansh ≈ 25× |
| 完全な機能実装 + ドキュメント整理 | **yansh** | 追加費用あるが意味的な閉環 |
| アーキテクチャ論証 / 純粋読み取り分析（task #3） | **深さ要件を見る** | yansh ≈ 4× |
| **bug 修正（失敗テストを含む、task #4）** | **CC**（4× 安価、修正方法同じ） | **yansh ≈ 4×** |
| 不慣れなコードベース | **yansh**（plan/audit 強制読み取り、より安全） | — |

**新しい結論**: bug 修正（特に失敗テストで信号を示すもの）は CC を選択——yansh の plan パイプラインは無駄。yansh の相対的な優位性は**明確な fail-signal がなく、全 repo 探索 / サブエージェント派遣 / 複数ファイル配套修正を必要とするタスク**で発生。

## データファイル

- `20260523_task4_yansh.json` — yansh batch JSON（ベースライン diff prelude を含む + 終了時 task_log JSON 行）
- `20260523_task4_yansh_stderr.log` — yansh stderr 完全な実行テストプロセス
- CC サブエージェント トランスクリプト: 親対話内、個別保存なし

## 次のステップ

- task #5 クロスファイルリファクタリング（広く使用される関数署名を変更 + 全 repo 適応、yansh の plan→code→fix vs CC の「読 + 改 + 検証」ループを確認）—— **yansh が反発すると予測**、複数ファイル配套は yansh の強項だから
- 4 回の AB を統合して README に
