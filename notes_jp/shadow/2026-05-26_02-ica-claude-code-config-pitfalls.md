# ICA × Claude Code / コーディング Agent 設定の落とし穴

**日付**：2026-05-26
**起因**：本セッション内で Claude Code が派遣した haiku subagent が繰り返し 401 エラーに遭遇し、ICA プラットフォームの 2 つのプロトコルパスの分離、model ID 命名の差異などのシステム的問題を深掘りした
**適用範囲**：401 / 404 / モデル利用不可の際にこのチェックリストで検査；Cline / Continue / Aider / 任意のコーディング agent 統合時の設定参考

---

## 概要：ICA は 2 つのプロトコルパスを暴露、背後は同じモデルセット

```
                       ICA Platform
                  └─ Bedrock claude-haiku-4-5  ✓
                       ↑                ↑
              ┌────────┴────┐    ┌──────┴──────────┐
              │ /ica/v1     │    │ /ica            │
              │ OpenAI プロトコル │    │ Anthropic プロトコル  │
              │ マッピング完全 ✓  │    │ haiku 設定漏れ ❌   │
              └─────────────┘    └─────────────────┘
                yansh/yscode      Claude Code
```

2 つのパスは**プロトコルが異なり、model マッピングテーブルは独立して保守されている**ため、「同じモデル A パスで使用可能、B パスでは使用不可」という状況が発生する。

---

## 1. 2 つのエンドポイント、**プロトコルが異なる**

| 用途 | Base URL | プロトコルフォーマット |
|---|---|---|
| 通常の ICA API（OpenAI 互換 SDK 用 — yansh / yscode はこのパスを使用） | `https://api.nextgen-beta.ica.ibm.com/ica/v1` | OpenAI Chat Completions（`POST /v1/chat/completions`） |
| **Claude Code 専用** | `https://api.nextgen-beta.ica.ibm.com/ica` ← **/v1 なし** | Anthropic Messages API（`POST /v1/messages`） |

**注意**：
- yansh コード内部は自動的に `/v1` を追加；yscode `.env` は直接 `/v1` を記述
- Claude Code の `ANTHROPIC_BASE_URL` は**/v1 を含めてはいけない**
- 2 つのパスは単純に相互交換できない——プロトコルが異なるため、リクエスト body / レスポンスフィールドも異なる

---

## 2. 2 種類の API キー（分別生成）

ICA Console → API Keys タブ：

- **ICA API Key**：通常の ICA API 用（yansh / yscode）
- **Coding Agent API Key**：Claude Code / コーディング agent 専用

各々は最大 1 つしか存在できず、再生成すると古いキーは無効になる。

> 実測：今回の AB テストでは yansh / yscode の両方で同一の ICA API Key（Coding Agent Key ではなく）を使用して通った。したがって通常の Key で十分。Coding Agent Key と Claude Code の `/ica` パス使用の関係は検証待ち。

---

## 3. Model ID 命名の不一致

| 出典 | haiku 4.5 ID |
|---|---|
| ICA Global Models（`bedrock_converse` 経由） | `claude-haiku-4-5` |
| Claude Code 内蔵デフォルトリクエスト | `claude-haiku-4-5-20251001`（日付サフィックス付き） |

ICA は AWS Bedrock 命名スタイル（日付なし）を使用；Claude Code は Anthropic 直結スタイル（日付付き）を使用する。

sonnet / opus は両側で命名が一致（`claude-sonnet-4-6` / `claude-opus-4-7`）するため問題なし；haiku 4.5 の命名不一致 → ID エラー発生。

---

## 4. Team Enabled トグル（admin 権限下）

ICA Console → API Keys → Global Models タブ、各行の右端「Team Enabled」トグル：

- **Status: Active** = プラットフォーム層で有効化（ICA グローバル利用可）
- **Team Enabled: オン** = あなたのチームが使用可能

通常ユーザーはこのトグルを切り替えることができません、チーム admin に連絡してください。ただし今回の yansh プローブ実測では haiku が通ったため、チームは実際に haiku を enable していることになります——したがってトグル状態は「既にオンだが UI がグレイアウト表示」の可能性があります。

---

## 5. ⭐ haiku 401 の真の原因（深掘り結論）

以前は「team が haiku を許可していない」と思われていました。**そうではありません**。証拠：`scripts/probe_ica_models.py` は `/ica/v1` + `claude-haiku-4-5`（日付なし）で実測し、5 つのモデルすべてが通りました：
- `claude-haiku-4-5` ✓
- `claude-sonnet-4-6` ✓
- `claude-opus-4-7` ✓
- `gemini-3-pro-preview` ✓
- `gpt-5.4-gus` ✓

**真の根本原因**：`/ica` パス（Anthropic プロトコルプロキシ）の model マッピングテーブルが**haiku 4.5 の設定を漏らしている**。Claude Code が `claude-haiku-4-5` でも `claude-haiku-4-5-20251001` でも送信しても、`/ica` は見つけられない → 401。

エラーメッセージ「team can only access global-models, tried claude-haiku-4-5-20251001」は**誤解を招く**——実際には `model not found in /ica mapping` ですが、ICA のエラー処理がそれを「access denied」バケットに入れてしまった。

sonnet / opus は `/ica` パス上でも**たまたま設定されている**（優先的に適応された一般的なモデル）ため、通；haiku 4.5 だけ漏れている。

**結論**：**これは ICA 側の実装バグ / 設定漏れであり、team 権限問題ではなく、Claude Code バグでもありません**。ICA に反馬できます。

---

## 6. ⭐ 一般的な設定：Cline / 任意の OpenAI 互換コーディング agent を ICA で実行

適用範囲：**Cline / Continue / Aider / 「OpenAI Compatible」プロバイダをサポートする任意のコーディングツール**

`/ica/v1`（OpenAI 互換パス）を使用することが一般的なソリューションであり、yansh / yscode と同じパスです：

| フィールド | 値 |
|---|---|
| **API Provider** | `OpenAI Compatible` |
| **Base URL** | `https://api.nextgen-beta.ica.ibm.com/ica/v1`（**/v1 を含める**） |
| **API Key** | 通常の ICA API Key（Coding Agent Key ではない） |
| **Model ID** | `claude-sonnet-4-6` / `claude-haiku-4-5` / `claude-opus-4-7` / `gpt-5.1-chat-gus`（任意選択、**日付サフィックスなし**） |

**Cline 具体操作**：VS Code に Cline をインストール → ⚙️ Settings → API Provider で `OpenAI Compatible` を選択 → 上記フィールドを入力 → **Verify** をクリックして接続テスト。

**⚠️ Cline 実測互換性（2026-05-26）**：

| Model ID | Cline 利用可 | 備考 |
|---|---|---|
| `claude-sonnet-4-6` | ✅ | probe + Cline の両方で通過 |
| `claude-haiku-4-5` | ✅ | probe 通過、Cline は専門的にテストしていない |
| `claude-opus-4-7` | ✅ | probe 通過、Cline は専門的にテストしていない |
| `gpt-5.1-chat-gus` | ✅ | **「chat」サフィックス = 標準 Chat Completions、Cline 互換** |
| `gpt-5.4-gus` | ❌ | reasoning モデル、Cline リクエスト内の特定フィールドが Azure 404 をトリガー |
| `gemini-3-pro-preview` | ❌ | Gemini 適応パスは Cline の OpenAI フォーマットリクエストと非互換 |

**根本原因**：Cline が送信するリクエストは probe スクリプトより複雑（`tools` / `temperature` を含む）、reasoning モデル（gpt-5.4）と Gemini の ICA 適応層がこれらのフィールドに非互換 → 404。probe の極小リクエスト（`model + messages + max_tokens` のみ）は全て通りますが、Cline は通りません。

**結論**：Cline + ICA は**Claude シリーズ**または**gpt-5.1-chat-gus**（「chat」で明示的にマークされた非 reasoning バージョン）のみを使用してください。

**利点**：
- yansh/yscode の検証済みパスと一致、安定性を確保
- モデルを切り替えるのは Model ID フィールドだけを変更

**欠点 / 注意**：
- Prompt cache は利用不可（ICA はどのみち透過しない — 既に `scripts/probe_ica_cache.py` で検証済み）
- Anthropic プロトコル固有機能（extended thinking reasoning フィールドフォーマット）は消失 — コーディングシーンで影響は少ない
- reasoning モデル（gpt-5.4 / gemini シリーズ）は利用不可

---

## 7. Claude Code も `/ica/v1` に切り替え可能か？

**直接的には不可**：

| | Claude Code が送信するリクエスト | /ica/v1 期待値 |
|---|---|---|
| プロトコル | Anthropic Messages API（`POST /v1/messages`） | OpenAI Chat Completions（`POST /v1/chat/completions`） |
| Body スキーマ | `{messages, max_tokens, ...}` `content: [{type, text}]` を含む | `{messages, ...}` content は平文 string |
| レスポンス | `content: [{type: "text", text}]` | `choices: [{message: {content}}]` |

リクエストパス自体が異なる → base URL を直接変更すると 404。

**プロキシを追加すれば可能**：ローカルプロキシを起動して外部で Anthropic、内部で OpenAI を呼び出し、ついでに model 名をマッピング：

```
Claude Code  ──Anthropic プロトコル──>  ローカル proxy  ──OpenAI プロトコル──>  ICA /ica/v1
              (任意の model エイリアス)              (ICA 命名にマッピング)
```

`ANTHROPIC_BASE_URL=http://localhost:<port>` を設定。既製ソリューション：
- **LiteLLM Proxy**（最も成熟、OpenAI ↔ Anthropic 双方向）
- **claude-code-proxy / anthropic-proxy**（Claude Code 専用軽量プロキシ）

**トレードオフ**：
- ✅ haiku + クロスファミリモデルをアンロック
- ❌ 追加コンポーネント保守 + デバッグチェーン延長 + エラー検査がより難しい
- ⚠️ Anthropic 固有機能（prompt cache / extended thinking）はプロキシ経由で消失の可能性、ただし ICA はどのみち透過しない

**現在の判断**：sonnet 4.6 はデフォルトで十分安定（本 AB テスト 15 回 dispatch 検証済み）、当面はプロキシを手がかりにしない。

---

## 8. 実運用：401 / haiku が利用不可の場合

敷居の低い順から高い順に：

| ソリューション | 操作 | 備考 |
|---|---|---|
| **C** | subagent をディスパッチする時に明示的に `model=sonnet` を指定、デフォルト haiku を回避 | **本セッションで既に使用中**；AB テスト 15 回 dispatch 全て sonnet で 401 なし |
| D | ICA に `/ica` パスの haiku 4.5 設定漏れを修正するよう反馬 | 根本原因解決だがプロセスを走る必要がある |
| A | チーム admin に連絡して Team Enabled トグルの状態を確認（元々開かもしれない） | 真の原因でない可能性が高い |
| B | Claude Code デフォルト subagent model を変更 | **このような設定は存在しない**（ドキュメント確認済み）、各 subagent タイプの model は .md 定義に含まれ、内蔵 agent は変更不可 |
| E | LiteLLM proxy を用いて Claude Code を `/ica/v1` に切り替え | 究極のソリューション、追加コンポーネントコスト |

## 9. デフォルトで haiku を使用する内蔵 subagent

- `claude-code-guide`（401 エラーに 2 回遭遇）
- その他 fast model タイプ（確認待ち）

## 10. 本セッションで検証された最も安定した方法

```
Agent({
  subagent_type: "general-purpose",
  model: "sonnet",   // 必須追加、デフォルトに依存しない
  prompt: "..."
})
```

`general-purpose` + 明示的な `model=sonnet` —— 本 AB テスト 5 個タスク × 3 方 = 15 回 dispatch 全て通過。

---

## データソース

- IBM ICA Console スクリーンショット（2026-05-26）：API Keys タブ + Global Models タブ
- 401 エラーメッセージ：`team can only access global-models, tried claude-haiku-4-5-20251001`
- AB テスト 15 回 dispatch 実測（task #1-#5 × 3 方）：`./ab/2026-05-25_06-summary-3way.md`
- probe_ica_models.py 5 個モデル実測全て通過（`/ica/v1` + 日付なし ID 使用）
- ICA prompt cache 探査：`scripts/probe_ica_cache.py`（透過なし）
- Cline 設定ドキュメント：https://docs.cline.bot/provider-config/openai-compatible
- Claude Code subagent ドキュメント：https://code.claude.com/docs/en/sub-agents
