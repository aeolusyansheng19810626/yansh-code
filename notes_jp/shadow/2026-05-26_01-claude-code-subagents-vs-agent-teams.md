# Claude Code マルチエージェントメカニズム：Subagents vs Agent Teams

**日付**：2026-05-26
**背景**：ABテスト完了後の議論「私（Claude Code）が子エージェントに使用するメカニズムは何か」、2つの混同しやすい概念を明確化
**重要な意図**：今後ユーザーが提供するタスクは**Agent Teams（方法二）の使用が必要になる可能性がある**、このノートは圧縮による損失を防ぐ

---

## 3つの関連メカニズムの比較

| | Claude Agent SDK | Subagents (Task tool) | Agent Teams |
|---|---|---|---|
| 呼び出し元 | 開発者が Python/TS コードを記述して SDK をインポート | Claude Code アプリケーション内蔵 Task / Agent tool | Claude Code で env var を有効化 |
| ステータス | 安定（独立 SDK 製品） | **安定** | **experimental**（デフォルト無効） |
| オーケストレーター | ユーザーが自分でコードを記述 | Claude Code メインエージェント | Claude Code メインエージェント（"team lead"） |
| 通信 | ユーザーコードが完全に制御 | 子エージェント実行完了後、メインエージェントに**単一メッセージ**を返す | チームメイト**相互**でメッセージ送信（mailbox） |
| タスクスケジューリング | ユーザーが完全に制御 | メインエージェントが一対多でタスク分配 | shared task list（チームメイトが自分で claim） |
| コンテキスト | ユーザーが完全に制御 | 子エージェント独立 context window | 各チームメイト独立 context window |
| 子エージェント間通信 | n/a | ❌ 不可 | ✅ 可能（直接メッセージ送信） |
| プロセス | ユーザー独立プロセス | 同一 Claude Code プロセス内で spawn | 複数の独立 Claude Code session |
| 有効化条件 | SDK をインストールしコードを記述 | デフォルト有効 | `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` |

---

## ABテストで使用したのは Subagents（Agent Teams ではない）

本 ABテスト（2026-05-25）は 5 つのタスク × 3 方法 = 15 回の dispatch、すべて Claude Code 内蔵 Task tool（`subagent_type=general-purpose, model=sonnet`）を使用 —— **Subagents メカニズム**、安定、env 設定は不要。

15 回の dispatch 期間中、**オーケストレーション層（Claude Code 自体）は問題が発生せず**、ターゲット CLI（yansh / yscode）自体のバグのみが暴露された。

---

## Agent Teams とは何か、いつ使用するか

### 有効化

```json
// settings.json
{
  "env": {
    "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"
  }
}
```

Claude Code v2.1.32+ が必要（`claude --version` で確認）。

### Subagents との核心的な違い — チームメイト同士で通信

```
Subagents:                       Agent Teams:
                                  
   メインエージェント               lead
   ↓ ↓ ↓                       ↙ ↕ ↘
   sub sub sub              team1 ⟷ team2 ⟷ team3
   ↓ ↓ ↓                       ↘ ↕ ↙
   結果返却                  shared task list + mailbox
```

Subagents では子エージェント間に**通信チャネルがない**——メインエージェントがすべての結果を受け取ってから統合。Agent Teams のチームメイトは**直接メッセージ送受信**でき、互いの見方に異議を唱えることができます（ディベートモード）。

### Agent Teams に適したシナリオ

ドキュメントで明確に列挙：
1. **Research and review**：複数のチームメイトが異なる角度から並行調査 → 相互共有 + チャレンジ
2. **新規モジュール/機能**：チームメイトが独立した部分をそれぞれ所有
3. **異なる仮説でのデバッグ**：チームメイトが異なる仮説をテスト → 科学的ディベートのように相互に反論、残された仮説がより正しい原因である可能性が高い
4. **クロスレイヤー調整**：フロントエンド/バックエンド/テストを異なるチームメイトが所有

> **重要な判断基準**：チームメイト間で「通信、ディベート、協調」が必要な場合に Teams を使用。「タスク分配 + 統合」だけで十分な場合は Subagents を使用。

### 有効化方法

```text
（メインセッションで直接入力）
Create an agent team to explore this from different angles: one
teammate on UX, one on technical architecture, one playing devil's advocate.
```

またはより具体的に：

```text
Spawn 5 agent teammates to investigate different hypotheses. Have them
talk to each other to try to disprove each other's theories, like a
scientific debate. Update the findings doc with whatever consensus emerges.
```

### 操作の制御

- **In-process モード**：`Shift+Down` で指定したチームメイトに切り替え、メッセージを直接送信；`Enter` でチームメイトセッションに進入、`Esc` で現在のターンを中断；`Ctrl+T` でタスクリストを切り替え
- **Split-pane モード**：各チームメイトは独立 pane（tmux または iTerm2 + `it2` CLI が必要）
- **In-process を強制**：`claude --teammate-mode in-process`

### デフォルトチームメイトモデル

チームメイトは lead の `/model` 選択を**継承しない**。`/config` で "Default teammate model" を設定、またはspawn プロンプトで指定：

```text
Use Sonnet for each teammate.
```

---

## Agent Teams の既知制限（experimental 状態）

ドキュメントの原文で ⚠️ Warning とマーク：

| 制限事項 | 影響 |
|---|---|
| in-process teammates の `/resume` または `/rewind` での復帰不可 | session 中断後 lead が存在しないチームメイトへのメッセージを送信する可能性 |
| Task ステータスがラグ| チームメイトが completed とマークしない → 依存 task ブロック |
| Shutdown が遅い可能性 | チームメイト は現在の request/tool 完了を待ってから終了 |
| 1 つの lead は同時に 1 つのチームのみ管理可能 | チーム切り替え前に cleanup が必要 |
| ネストが不可 | チームメイトが team / teammate をさらに spawn できない |
| Lead は固定 | session を作成したユーザーが lead、移譲は不可 |
| Permissions は spawn 時にロック | チームメイト起動後は単独で mode 変更可、spawn 時は team ごとに設定不可 |
| Split-pane は VS Code 内蔵ターミナル / Windows Terminal / Ghostty 未対応 | tmux または iTerm2 を使用 |

### Token コスト

> Agent teams add coordination overhead and use significantly more tokens than a single session.

各チームメイトは独立した Claude インスタンス、token コストは**線形増加**。3～5 個のチームメイトはドキュメント推奨の出発点、5～6 個のタスク / チームメイトが適切なタスク数。

---

## いつどちらを使用するか — 決定表

| シナリオ | 推奨 | 理由 |
|---|---|---|
| メインコンテキスト削減用の「タスク分配作業者」（コード検索、テスト実行、調査） | **Subagents** | 安定 + token 節約 + 通信が簡潔 |
| 5 つの独立タスクを並行実行してレポート完成 | **Subagents** | 相互通信不要 |
| 複数視点から PR レビュー（セキュリティ/パフォーマンス/テストカバレッジ） | **Agent Teams** | チームメイトが同じ PR を見るが各自焦点異なる、最後に lead が統合 |
| 仮説を相互に反論してデバッグ | **Agent Teams** | ディベート機制 |
| 新規モジュール独立部分をそれぞれ実装 | **Agent Teams** | 同一ファイル競合削減 |
| 大規模リファクタリング / クロスレイヤー改変 | **Agent Teams** | フロントエンド/バックエンド/テストを異なるチームメイトが所有 |
| 一回限りの自動化（スクリプト記述 API 呼び出し） | **Agent SDK** | Claude Code インタラクティブ session に進入しない |

---

## データソース

- Subagents ドキュメント：https://code.claude.com/docs/en/sub-agents
- Agent Teams ドキュメント：https://code.claude.com/docs/en/agent-teams
- ABテスト実測ノート：`./ab/2026-05-25_01-task1-3way.md` ~ `2026-05-25_06-summary-3way.md`

---

## 待機中

- 今後ユーザーが提供するタスク**が Agent Teams を要求する可能性がある**：その時は以下を忘れずに：
  1. まず `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` が設定されていることを確認
  2. team をネストしない（チームメイトが team をさらに起動できない）
  3. タスク設計をチームメイトが独立して作業でき、ファイル競合を避けるように設計
  4. Windows + split-pane を希望する場合、tmux をインストール（**Windows 互換性が悪い、in-process のみになる可能性が高い**）
