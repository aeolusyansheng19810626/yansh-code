# P2 #8 Skills システム

前回 [_15](./2026-05-22_15-plan-mode-c.md)：P2 #7 Plan Mode 完成後に #8 を実施。
ROADMAP「最小版」目標——ディレクトリ規約 + frontmatter triggers + キーワード照合による system prompt インジェクション。

## 何が変わったか

### 1) skills.py 新規ファイル

データ構造：
```python
@dataclass
class Skill:
    name: str
    description: str
    triggers: list      # キーワード（大文字小文字区別なし）
    modes: list         # 適用 mode；空=全部
    body: str           # markdown 正文
    source_path: str
```

API：
- `parse_skill_file(path)` 単一 .md を解析（手書きの最小限 frontmatter パーサー、PyYAML を使わない）
- `discover_skills(workspace_dir)` プロジェクトレベル + グローバルをスキャン
- `match_skills(input, skills, mode)` キーワード照合
- `format_skills_prompt(matched)` system prompt フラグメントに結合
- `load_and_format(input, ws, mode)` ワンストップエントリーポイント

ディレクトリ規約：
- `<workspace>/skills/*.md` プロジェクトレベル（優先）
- `~/.yansh/skills/*.md` グローバル（バックアップ）

同名時はプロジェクトレベルがグローバルをオーバーライド。

### 2) frontmatter 形式

```yaml
---
name: code-review
description: コードレビューワークフロー
triggers: ["審査", "code review", "review"]
modes: ["audit", "plan"]      # オプション；空=全 mode
---
（markdown 正文、prompt フラグメントとして使用）
```

手書きパーサーはサブセットのみサポート：スカラー / `[list]` / `# コメント` / シングル/ダブルクォート文字列。十分で PyYAML に依存しない。

### 3) agent.py インジェクション

モジュールレベル `_ACTIVE_SKILLS_PROMPT: str = ""` が現在アクティブな skill フラグメントを保持。

`_run()` エントリーポイント：
```python
prompt_frag, matched = skills.load_and_format(requirement, _get_workspace(), mode=mode)
_ACTIVE_SKILLS_PROMPT = prompt_frag
if matched: console.print(f"[skills] 命中 {len(matched)} 个：...")
```

インジェクションポイント：
- `plan()` system prompt 末尾 += `_ACTIVE_SKILLS_PROMPT`
- `code()` 2つのパス（既存ファイル / 新規作成）末尾 += 同上
- `audit()` system prompt 末尾 += 同上
- `fix()` 2つのパス（review_rejection / test_failure）末尾 += 同上
- `plan_chat()` 独立スキャン（mode='plan'）、自身の system prompt を書く（`_ACTIVE_SKILLS_PROMPT` を再利用しない。plan_chat は run を経由しないため）

### 4) main.py コマンド

```
/skill list              全 skill をリスト表示（プロジェクトレベル + グローバル）
/skill show <name>       特定 skill の完全な内容を表示
```

`_SLASH_COMMANDS` 自動補完リストに追加。

## 検証

### ユニットテスト（tests/unit/test_skills.py、新規 20 条）

frontmatter：
- スカラー / list / 欠落 / コメント行 / クォート / 大文字小文字の正規化 / frontmatter なし
- modes フィールド：リスト / 空 / `applies_to_mode` の動作

発見：
- プロジェクトレベルスキャン
- ディレクトリなし時は空を返す
- **プロジェクトレベルがグローバルレベルをオーバーライド（同名優先）**——`monkeypatch.setattr(Path, "home", ...)` で検証
- 不正なファイルはメインフローをクラッシュさせない

照合：
- キーワード（大文字小文字無関係）
- mode フィルタ（modes=[audit] の時 code mode は命中しない）
- 空入力 / None セーフ

フォーマット化：
- 空リスト時は空文字列を返す
- 単一/複数 skill フォーマット
- エンドツーエンド load_and_format

agent 統合：
- `run()` エントリーポイントが skill を検出 → `_ACTIVE_SKILLS_PROMPT` が skill body を含む
- 命中しない場合 `_ACTIVE_SKILLS_PROMPT` はクリア（残留物を残さない）

12/12 ファイル全パス；新規ファイル 20/20。

### 統合検証（ICA Sonnet 4.6）

workspace 準備：
- `calc.py`：最小限の add / divide（divide は除算ゼロを未処理）
- `skills/code-review.md`：レビューチェックリスト + **markdown テーブル出力形式を強制** + 深刻/中/低の3段階分級

`python main.py "審査 calc.py" --mode audit` を実行：

出力は `[skills] 命中 1 个：code-review` を含み、LLM は **skill が規定した形式に厳密に従って**レポート：

| ファイル | 行号 | タイプ | 説明 | 提案 |
|------|------|------|------|------|
| calc.py | 5–6 | **深刻** | divide が b=0 を未処理 | ... |
| calc.py | 1、5 | **中** | 両関数とも docstring なし | ... |
| calc.py | 1、5 | **低** | 型注釈欠落 | ... |
| — | — | **低** | テストファイルなし | ... |

**重要な効果の証拠**：
- テーブル列名（「ファイル / 行号 / タイプ / 説明 / 提案」）は skill の記述と完全に一致
- タイプ分級は「深刻 / 中 / 低」を使用——**skill で規定した3段階に完全に対応**、LLM デフォルトの「high / medium / low」は使用していない
- 各項目に具体的な行号を記載——skill の「具体的な行号を必ず記載すること」が守られている

負の側面検証（命中しない）：
- `mode=audit + "審査"` → 命中
- `mode=code + "審査"` → mode フィルタで除外
- `mode=audit + "最適化"` → キーワード不命中

## 評価

### .agent_rules との本質的な違い

`.agent_rules` はプロジェクトレベルの**定数ルール**、毎回のタスクで注入；Skills は**オンデマンド読み込み**——ユーザーの入力が trigger に命中したときだけ注入。両者は並存：rules は「このプロジェクトが守るべきこと」を管理；skills は「このようなタスクを実行する際に従うべきワークフロー」を管理。

### Claude Code との差異

Claude Code の Skills：
- **LLM インテリジェント照合**：キーワードだけでなく、文脈/履歴も参考
- **Skill セキュリティサンドボックス**：第三者 skill が agent 動作を変更できる範囲に境界コントロール
- **Skill 間依存**：skill A トリガー後に skill B を読み込み可能

yansh 現在は「prompt インジェクション」最小版：
- キーワード照合のみ（知的でない）
- セキュリティサンドボックスなし（skill コンテンツが LLM に任何を実行させることが可能、悪意ある prompt インジェクションを含む）
- 依存をサポートしない

「プロジェクト内プライベートワークフローテンプレート」として十分——これは最も一般的な実際のニーズ。第三者配布シナリオは後でやってもまだ間に合う。

### 「Prompt as a Service」の工学的意義

コードレビューのようなワークフローは以前は：
1. ユーザーが毎回手書きで「X を審査、深刻/中/低 で分級、行号を必ず記載」
2. 出力形式は毎回異なる

現在は：
1. skill を一度書く
2. 「審査」を含む任意の入力が自動的に読み込む
3. 出力形式は高度に一貫——スクリプトで機械解析可能

これは「経験」を再利用可能なユニットに沈澱させる最小工学化。

次のウェーブ（今回の範囲外）：
- LLM インテリジェント照合：軽量 LLM call を一度使って「この入力にはどの skill が必要か」を判断
- skill 優先順位 / 相互排他
- skill トリガー token 統計：skill インジェクションが何 token 追加したかを知る
- skill が tool リストに及ぼす影響：ある skill は工具ホワイトリストを制限したいかもしれない（例：「code-review skill は readonly tools のみ使用」）
- 組み込み skill ライブラリ：プロジェクトに数個の常用テンプレートが付属（code-review / refactor / debug / api-design）

## キーファイル

| ファイル | 変更 |
|---|---|
| `skills.py` | 新規ファイル：Skill dataclass + parse / discover / match / format / load_and_format |
| `agent.py` | モジュールレベル `_ACTIVE_SKILLS_PROMPT`；run() エントリーポイントで読み込み；plan/code/audit/fix/plan_chat で結合 |
| `main.py` | `/skill list` `/skill show <name>` |
| `tests/unit/test_skills.py` | 新規ファイル 20 条ユニットテスト |
| `tests/run_unit.py` | ファイルリストに追加 |
