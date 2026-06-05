# yansh P1 技術負債集中修復 plan

## Context

**起因**：2026-05-25 三方 AB-test（cc / yansh / yscode）が5つのtaskを実行し、yansh が7つのP1レベルの機制問題（#1-#7）が明らかになった。その中：
- #2 91万 token を消費して修正範囲外の task 変更（plan が coder の「修正不要」シグナルを消費しない）
- #4 サイレント失敗により bug 修正漏れ（baseline failure 識別が誤ってユーザーリクエストを吸収）
- #5 CLI crash（_err シグネチャ不一致 — task #5が公開した問題そのもの）

**目標**：半日～1日で7つの問題のうち6つ（#1は既に漂流、ダウングレード）を集中修復し、AB 回帰 task #2/#4/#5 の yansh をパスさせる。

**ハード制約**：
- Implementor は Sonnet 4.6 を使用（既に15回の dispatch で AB 検証済み安定）
- Reviewer は **独立した context を必須** — Agent tool で subagent をディスパッチし、prompt は結論を事前設定しない
- Reviewer は Opus 4.7 を使用（reasoning が強く、implementor の暗黙的な仮定に challenge できる）
- テストは LLM agent を使わず、直接 bash で pytest を実行

## 修復戦略

**直列 + 段階的 review + 独立 commit**：

| フェーズ | タスク | review | 単一テスト | 累計推定時間 |
|---|---|---|---|---|
| 準備 | #7 → #2 → #3 | 不派（機械的修正） | 修正完了後に実行 | ~2h |
| 漂流処理 | #1 を P3 監視にダウングレード | n/a | n/a | 5min |
| プロトコル層 | #6 → #4 → #5 | 各項目ごとに Opus reviewer をディスパッチ | 修正完了後に実行 | ~1.5日 |
| 総合検証 | AB-test 回帰 | 手動 git diff レビュー | pytest 完全スイート | ~30min |

**Commit 戦略**：各P1に対して独立した commit を1つ、message 形式 `fix(P1 #N): <subject>`、ロールバックと bisect の便宜のため。

## 詳細方案

### P1 #7 — _err シグネチャ不一致（30min）

**File**: `tools.py:25` + `agent.py:1236, 1244, 1248, 1310`

**改 `tools.py:25`** に `tool` 仮パラメータを追加：
```python
def _err(kind: str, msg: str, tool: str = None, **extra) -> dict:
    result = {"error": kind, "message": msg, **extra}
    if tool:
        result["tool"] = tool
    return result
```

agent.py の4つの callsite は変わらない（引き続き `_err("internal", "msg", name)`），name は正しく tool パラメータに割り当てられる。

**単一テスト**：LLM が `search_in_files` に不正な regex を渡すシナリオを構築（例：`missing )`），CLI が crash しないこと、エラー返却構造が `tool` フィールドを含むことをアサート。ファイル位置：`tests/unit/test_tools.py`（既に存在）。

### P1 #2 — ラウンド使い切り偽警告（< 1h）

**File**: `agent.py:1836-1840`

現在の条件 `attempts_left <= 0 and response_message.tool_calls`，**最後のラウンドが `task_complete` を呼び出したかどうかをチェックしていない**。

**改法**：警告前に判定を追加 — 前のラウンドの tool_calls に `task_complete` があれば警告しない。

**単一テスト**：「task_complete + ラウンド使い切り」パスをシミュレートし、警告分岐に入らないことをアサート。

### P1 #3 — Detector を NameError / AttributeError に展開（< 30min）

**File**: `agent.py:2374-2392`

現在の regex は `TypeError missing argument` のみをマッチ。追加：
```python
r"NameError:\s+name\s+'.+?'\s+is\s+not\s+defined"
r"AttributeError:\s+'.+?'\s+object\s+has\s+no\s+attribute"
```

**単一テスト**：NameError / AttributeError 出力を構築し、detector がマッチし fix 予算を追加することをアサート。

### P1 #1 — ダウングレード処理（5min）

Explore レポートで現在のコードが **`/workspace` リテラルパス仮定を持たない**ことを確認（`tools_schema.py:39` は汎化テキストを使用、`agent.py:1599` は実行時の `_get_workspace()` を注入）。memory の説明は task #4/#5 v3 の古い commit 状態に基づいている。

**アクション**：memory の `project_yansh_tech_debt.md` で #1 を `[既に漂流 / 解決済み]` としてマーク、P1 から除外、P3 監視項目に転換 — 次回 AB 実行時に fix ループがパス仮定エラーを持つかどうかに注視。

### P1 #6 — Baseline 識別がユーザーリクエストを誤吸収（半日）

**File**: `agent.py:2705-2709`（capture）+ `agent.py:2792-2821`（subset 比較）

**問題**：current vs baseline subset → 通過と見なす、「ユーザーが明示的に修正を要求した失敗」vs「無関係な pre-existing」を区別しない。

**改法（推奨 a 方案）**：subset 判定前に prompt キーワードフィルタリングを追加：
- ユーザー prompt が「修」/「テスト失敗」/「fix」/「failing test」/「bug」などのキーワードを含む場合，**baseline subset 比較を無効化**（完全な fix ループを強制）
- キーワードリストはハードコード + 大文字小文字を区別しない

**単一テスト**：ユーザー prompt がキーワードを含む/含まない2つのシナリオを mock し、baseline 比較が有効かどうかをアサート。

**Reviewer (Opus，独立 context)**：subagent をディスパッチして git diff を確認、prompt に以下を提供：
- 元の問題説明（memory `project_yansh_tech_debt.md` の P1 #6 からコピー）
- 現在の diff
- 結論を事前設定しない；reviewer に独立的に「問題を解決したかどうか + 新しい問題を導入したかどうか（例：ユーザーがテスト修正を述べていないが実際にはテストに pre-existing failure がある場合に誤ってアクティベートされる fix ループ）」を判定させる

**回帰**：AB-test task #4 yansh を実行し、今回は正しく修正できるはず。

### P1 #4 — Plan が coder「修正不要」シグナルを受け入れない（半日）

**File**: `agent.py:1828`（coder task_complete 処理）+ `agent.py:2727-2737`（run() フロー）

**改法**：coder `success=True` かつ summary が「修正不要」/「既に実装」/「no changes needed」キーワードをマッチするとき，plan は主動的に残りの expected_edits サブタスクをスキップ，**直接 fix/test フェーズに入る**。

**単一テスト**：coder summary がキーワードを含む/含まないをmock し，plan が短絡するかどうかをアサート。

**Reviewer (Opus)**：独立 context で diff を確認，重点的に challenge：「キーワードフィルタリングが coder の実際の作業を誤吸収しないだろうか？例えば coder が1つのファイルを修正した後『残りの3つは修正不要』と述べた場合，全て修正不要と識別されるだろうか？」

**回帰**：AB-test task #2 yansh を実行，予想 token は 915K から大幅に低下。

### P1 #5 — Plan がドキュメント作成前に必ず explorer を実行（半日）

**File**: `agent.py:1599-1628`（plan system prompt）

**改法**：plan agent が「具体的な行番号」/「変更範囲」/「互換性分析」/「コード詳細」などのキーワードを含むタスク prompt を検出したとき，**まず explorer subagent（general-purpose, sonnet）をディスパッチして**関連ファイルをスキャンし，スキャン結果を plan context として，次にドキュメントを生成。

またはより簡単な方案：plan system prompt にルールを追加 — 「具体的なコード説明に関わる前に必ず read_file を最低1回関連ファイルで呼び出す」，plan フェーズで read_file ツール（現在は plan フェーズにツールがない）を開放。

**待 reviewer が2つの方案のどちらかが最適かを決定**。

**Reviewer (Review #4 とマージして1回)**：独立 context，「2つの修正が plan フロー内で相互に競合するかどうか」と問う。

**回帰**：AB-test task #3 yansh を実行，予想ドキュメント精度が 5/8 から ≥ 7/8 に向上。

## 検証

**各修正完了後**：
```cmd
pytest tests/unit/test_<関連>.py -v
```

**全修正完了後**：
```cmd
pytest                                  # 完全スイート
git log --oneline main..HEAD            # 6つの独立 commit を確認
git diff main..HEAD --stat              # 総合レビュー
```

**AB 回帰**：
```cmd
cd C:\Users\ShengYan\Projects\AB-test\yansh\yansh-code
yansh code <task-2 prompt>
yansh code <task-4 prompt>
yansh code <task-3 prompt>     # #5 を検証
```

予想：3つの task の yansh が今回すべてパス（以前は ❌/❌/⚠️）。

## Reviewer agent 呼び出しテンプレート

```python
Agent({
  subagent_type: "general-purpose",
  model: "opus",
  description: "Review P1 #N implementation",
  prompt: """
あなたのタスク：yansh-code の P1 #N 修復実装を独立的に review する。

**元の問題**：
<memory project_yansh_tech_debt.md の P1 #N 説明からコピー>

**現在の修復**：
<git diff 出力>

**Review 重点**：
1. 修復が元の問題を解決したか？
2. 新しい問題を導入したか（edge case / 誤吸収 / パフォーマンス低下）？
3. キーワードフィルタリング戦略は緩すぎるか厳しすぎるか？
4. カバーされていないトリガーシナリオはないか？

**しないこと**：結論を事前設定。実装が正しい場合，明確に「approve + 理由」と述べ；問題がある場合，具体的な行番号 + 修正提案を列挙。
"""
})
```

## 既知リスク

1. **agent.py の4つのP1をすべて修正**（#7 / #2 / #6 / #4），直列処理で競合を回避。
2. Reviewer は Opus 独立 context，単一コスト ~80K token，3回で約 240K — 可能な範囲。
3. AB 回帰は `AB-test/yansh/yansh-code` workspace で実行，主仓 main ブランチ状態に影響なし。
4. **#5 方案二選一**（キーワードトリガー explorer subagent vs plan フェーズで read_file ツール開放）は reviewer 評議後に決定，1回の反復が必要な可能性あり。

## データソース

- yansh 技術負債リスト：`memory/project_yansh_tech_debt.md`
- コード事実検証：本回 explore agent レポート（agentId: a327058e63f133fcb）
- ICA 設定落とし穴（reviewer が sonnet/opus を使い haiku を使わない理由）：`memory/reference_ica_claude_code_config.md`
