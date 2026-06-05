# 2026-05-21 yansh が特定の指標で初めて Claude Code subagent を上回った

## 背景

以前の三者公平比較（[fairness ノート](./2026-05-21_03-fairness-and-max-depth.md)）で発見：

- **dispatch 漏修は Anthropic Claude Code subagent も共通の盲点**
- Opus と Sonnet subagent ともに agent.py の dispatch テーブルを主動的に確認していない
- yansh は以前 review/fix ループのおかげで偶然ひっかかった

もし prompt が LLM agent に全ロードマップの確認を教えることができれば、理論的には yansh がこの項目で **Claude Code subagent を上回る**ことができる——これは反直感的な実験：アマチュアツールが針対的な prompt で成熟したツールを超える。

## 実験：「全ロードマップ意識」prompt を書く

`_ARCHITECT_ROLE` と `_CODER_ROLE` に追加、重要な設計ポイント：

1. **具体的な形状を直接指摘**：抽象的な原則「関数シグネチャを変更する際は全ロードマップを検討する」ではなく、
   「dispatch テーブル（agent.py の `if name == "X"` のブランチ）」と記述
2. **デフォルト仮定に反対**：ユーザーが列出したファイルリストは必ずしも完全ではないことを明示
3. **一般的な落とし穴を提供**：import ステートメント、ドキュメント例、dispatch の3種類の隠れた依存関係

## 結果

同じタスク（list_files に max_depth を追加）：

| 項目 | 以前の yansh | **今回の yansh（新 prompt）** | Sonnet subagent | Opus subagent |
|---|---|---|---|---|
| モデル | Sonnet | Sonnet | Sonnet | Opus |
| 耗時 | 242s | **175s** | 134s | 99s |
| ツール呼出 | 39 | 43 | 23 | 16 |
| **Plan フェーズが agent.py を含むか** | ❌ | **✅** | — | — |
| **dispatch 実際に修正** | ✅ fix loop 副産品 | **✅ plan→code 直接** | ❌ | ❌ |
| 実装が正しいか | ❌ | ❌（同じ off-by-one） | ✅ | ✅ |
| タスク判定 | 失敗 | 失敗 | 完了 | 完了 |

## 3つの証拠がこれが prompt の効果であることを明確に示す

**証拠 1：plan ファイルリストの変化**
- 以前：`['tests/unit/test_tools.py', 'tools.py']` (2ファイル)
- 現在：`['tools.py', 'tools_schema.py', 'agent.py', 'tests/unit/test_tools.py']` (4ファイル、主動的に agent.py を含む)

**証拠 2：plan フェーズで正しく変更され、fix loop の副作用ではない**
- 以前の yansh は単体テスト失敗 → review/fix 複数回 → 初めて dispatch に到達
- 現在の yansh は architect の出力 plan で直接 agent.py をリストアップし、code フェーズで一度に正しく修正

**証拠 3：subagent は同じタスクで依然漏らす**
- Anthropic の Claude Code subagent（Opus/Sonnet 両方試した）の内組 prompt には**この項目がない**
- yansh がこの prompt を追加 → この項目で上回った

## 歴史的意義

**これは yansh が特定の次元で Anthropic の成熟したツールを上回った初めてのケース**。

yansh 全体が優れているからではない——それでも遅く、依然として off-by-one、依然として関係のないコードを不用意に変更している。
しかし**針対的な prompt 改善**により「全ロードマップ意識」の項目で競争に勝った。

これは ROADMAP P0 #2 の核となる判断を検証した：

> 1週間 prompt を調整する効果は、5つのツールを追加するより勝る。

そして更に重要なことに：

> **独自のコードライブラリの具体的な形状に針対した prompt** は、小さなツールが大きなツールを特定の項目で打ち負かすことを可能にする。
> Claude Code の prompt は汎用的である必要がある——「agent.py の dispatch ブランチ」のような
> yansh コードライブラリと強く結合した表現を書くことはできない。これによって個人プロジェクトに本当の優位性が生まれる。

## しかし yansh は依然2つの古い問題で劣っている

1. **同じ off-by-one**：`dirs.clear() when current_depth >= max_depth` を実装
   - max_depth=1 の時、root はクリアをトリガーしない（0<1）、サブディレクトリのファイルが追加される
   - すでに2回失敗しているのに依然失敗中——prompt がこれは「再帰剪枝制御フロー」の能力問題を解決していないことを示す
2. **不用意に関係のないコードを変更**：`files.append(rel_path)` を
   `files.append(rel_path.replace("\\", "/"))` に変更し、`test_list_files` を破壊

## Few-shot 思考：具体的な形状で具体的な問題を打つ

今回の prompt 改善で最も記憶に値する一点：

**抽象的原則の勇気** vs **具体的名詞の有効性**——

「関数シグネチャを変更する際は全ロードマップを検討する」というのは抽象的な原則で、LLM がそれを聞いてもおそらく気に留めない。
「dispatch テーブル（agent.py の `if name == "X"` のブランチ）」というのは具体的な名詞で、
LLM はすぐに何を見るべきかのアンカーポイントを持つ。

これは [list-tools ノート](./2026-05-21_01-list-tools.md) における Claude Code の
「修正したばかりのファイルを再度読まない」という条項と同型——**1つの具体的な否定的警告**
は抽象的なガイドラインより10倍強い。

## 今後

- off-by-one を解決：_CODER_ROLE に few-shot example を追加して、
  max_depth のような剪枝制御フロー の正しい書き方を示す（「各ファイルについて path_parts
  数を計算し、max_depth を超えたら直接 continue」——シンプルで直接的、dirs.clear() のような巧妙な剪枝に頼らない）
- 「不用意に関係のないコードを変更」を解決：_CODER_ROLE に「diff は タスク説明の機能のみをカバーすべき；
  'while you're at it' リファクタリングは必ず先に停止してユーザーに質問する」を追加
- この2項目は両方とも P0 #2 のサブタスク

## 一文での要約

**yansh は30行の針対的な prompt を使用して、dispatch 隠れ依存関係チェックの項目で
Anthropic Claude Code の2段階の subagent を上回った**。全体的には依然遅れているが、
単点突破は ROADMAP P0 #2 のレバレッジを証明した。
