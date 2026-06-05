# ABテスト最終報告書（2026-05-28～29）

**範囲**：yansh-code vs yscode、task1-5、合計7ラウンドのイテレーション（v3～v7 + yscode Z-15～Z-29）

---

## 最終データ（最適版）

| task | タイプ | yansh | yscode | cc（参照） |
|---|---|---|---|---|
| task1 探索 | 読み取り専用 | 394K/120s ✓ | 15K/17s ✓ | - |
| task2 trick | 実装済み検出 | 372K/154s ⚠️重複実装 | 30K/21s ✓ | - |
| task3 ドキュメント生成 | 読み取り専用+write doc | **303K/184s ✓** | 188K/133s ✓ | - |
| task4 버그 修正 | コード作成 | **196K/59s ✓** | 285K/136s ✓ | - |
| task5 大量バッチ | 65か所の変更 | **981K/769s ✓** | 1307K/320s ✓ | ~不明/180s ✓ |

yansh バージョン：`0468033`（compact thrashing 修正）
yscode バージョン：Z-28（streaming fallback）

---

## 成果

### 1. task5 から継続的失敗から安定合格へ

yansh task5 は5つのバージョンを経て初めて合格：

| バージョン | tokens | 時間 | 結果 | 根本原因 |
|---|---|---|---|---|
| v3 | 1402K | 574s | ❌ | test patch バインディングのずれ + baseline タイムアウト記録漏れ |
| v4 | 2519K | 1620s | ❌ | write_file モード22ラウンド重写副作用 |
| v5 | 2320K | 429s | ❌ | 同上（部分改善） |
| v6 | - | 351s | ❌ | compact thrashing（閾値30K太低） |
| **v7** | **981K** | **769s** | **✓** | thrashing 修正 |

最終 token は2519Kのピークから981Kに低下（**-61%**）。

### 2. yansh 新規 3つの重要メカニズム

| commit | 内容 | 解決された問題 |
|---|---|---|
| `e608b1b` | baseline タイムアウト30s→120s + タイムアウト時の収集済み出力保持 | baseline 記録漏れ → coder が関連のないテスト反復修正 |
| `53b7b37` | test_subagent.py patch バインディング修正 | 15個のテスト偽陰性（patch agent.call_llm が無効） |
| `0468033` | compact 閾値60K + 圧縮率判定 + _compact_disabled | thrashing 無限ループ → タスク強制終了 |

### 3. yscode 重要最適化経路（Z-15 → Z-28）

| バージョン | task5 tokens | 重要な変更 |
|---|---|---|
| v3（Z-15） | 2663K ❌ | explorer cap=3 + ドキュメントタスク検出 |
| Z-23 | 2703K ✓ | attempts 消費済みだが失敗なし → 成功と見なす |
| Z-24 | 2264K ✓ | baseline 非空で早期 break |
| **Z-28** | **1307K ✓** | streaming tool_use parse error → fallback stream=False |

---

## 経験

### E1：タスク種別が勝者を決定、万能アーキテクチャなし

| タスク種別 | yansh 優位性 | yscode 優位性 |
|---|---|---|
| 読み取り専用探索 | — | 直接回答、coder loop 非トリガー（15K vs 394K）|
| 実装済み検出 | — | architect が既存を識別、重複実装なし（30K vs 372K）|
| ドキュメント生成 | 直接 doc 作成、余計な探索なし | — |
| コード修正/大量変更 | token より効率的（plan/coder 分離精密）| 並行がより高速（subagent 並列）|

両システムは各々の得意領域があり、全面的な圧倒は存在しない。

### E2：ABテスト環境品質は結論信頼性に直接影響

本ラウンドでは複数の結論が環境問題に汚染：
- **workspace 汚染**：yscode task5 前回実行残存、coder が「完了」を検出32s で終了（偽 pass）
- **bug inject SKIP**：workspace 未削除、注入ステップが pattern 見つけられず静かにスキップ
- **baseline タイムアウト**：30s タイムアウトが pytest 出力を切断 → 15個の事前保存失敗記録漏れ → coder が関連のないテスト反復修正

**教訓**：実行前に必ず `git checkout -- . && git clean -fd`、runner 内部 reset に頼れない。

### E3：compact 閾値は低ければ低いほど良いわけではない

yansh は次を経験：80K（非トリガー）→ 30K（thrashing）→ 60K（安定）。

規則：閾値は「compact 後の最小メッセージ本体」（system prompt + 摘要 + 最近 N ラウンドツール呼び出し）より高くなければならない。task5 のような複数ファイル大量タスクの底線は約42K、30K 閾値トリガー後すぐ超過、無限ループ発生。

**正しい方法**：cc の rapid-refill 思想を参照――「compact 後まだ超過」ではなく「圧縮率 <15% のみ thrashing とカウント」を使用。

### E4：bedrock streaming に bug あり、大型 tool call は切断される

yscode task5 coder ステージで bedrock が `replace_in_file` の arguments JSON を切断：
```
arguments: '{"path": "tools.py"'  ← 第19文字で切断
```

context が大きすぎる問題ではなく（当時 history は25K）、bedrock streaming モード下での大型 tool call output の bug。**修正法**：parse 失敗時 fallback で stream=False リトライ（Z-28）。

### E5：テスト patch ターゲットと実装バインディング一致

yansh test_subagent.py 15個のテストが長期偽陰性、根本原因：`subagent.py` が `from llm_client import call_llm` 遅延読み込み、テスト patch `agent.call_llm` は subagent のローカルバインディングに影響しない。

**規則**：patch ターゲットは関数実行時に検索する名前空間である必要があり、呼び出し元の参照ではない。`monkeypatch.setattr(module, "name", fake)` 直接割り当てではなく使用。

---

## 教訓

### L1：最適化方向が正しくても、実装に副作用がある可能性

yansh Fix C（write_file 処理20+か所許可）の方向は正しいが、実装後 task5 token が1402Kから2519Kに暴騰。根本原因：write_file モード トリガー後、LLM は22ラウンド内で循環重写（write→pytest 失敗→再度書き込み）、1ラウンドあたり1500行ファイル read+write、22ラウンド replace_in_file より高コスト。

**教訓**：最適化完了後すぐに回帰実行、「方向が正しい = 結果が正しい」と仮定できない。

### L2：thrashing 保護は raise のみではなく、タスク続行させる必要

元の thrashing 保護（連続4回超過 → raise RuntimeError）は task5 でタスクを直接終了。cc の設計を参照：thrashing 時に自動 compact を停止するが、タスク実行は続行、「保護」を理由にユーザー タスク終了することはできない。

### L3：データが物語る、直感分析は間違うかもしれない

本ラウンドでは2回の診断エラー：
1. yscode token が高いのは subagent が完全な出力をメイン context に詰め込むせい → 実測 subagent は既に1000文字ハード cap、主因は coder 39ラウンド × 50K/ラウンド
2. compact 閾値60K→30Kで bedrock 切断確率低下できる → 実測 history は25K、compact 閾値変更は no-op

**教訓**：log データを確認し、結論を出す；「直感的に合理」で実測に代わるべきではない。

### L4：ICAの制限は AB テスト基準に書き込む必要

本ラウンドでは複数の結論が ICA 環境に影響：
- prompt cache が透過されない（yansh/yscode キャッシュなし、cc あり → 時間差異に ICA 要因）
- bedrock が tool call を切断（yscode 特有の問題、直接 Anthropic は発生しない）
- opus-4.8 ICA 未設定（モデルバージョン不一致）

AB テスト結論は「ICA 環境に基づく」と注記、cc 絶対値と直接比較できない。

---

## 最終結論

### token 効率

- **小型タスク（読み取り専用/実装済み検出）**：yscode 勝出、10-20x 差距
- **中型タスク（ドキュメント生成/バグ修正）**：接近、yscode task3 既に yansh 以下（188K vs 303K）
- **大型タスク（大量変更）**：yansh がより効率的（981K vs 1307K、-25%）

### スピード

- **全タスク**：yscode がより高速（subagent 並行）、task5 2.4倍高速（320s vs 769s）
- cc 参照値（~180s）は主に prompt cache から受益、ICA 環境は比較不可

### 信頼性

- task3/4 両側安定
- task5 yansh v7 安定合格；yscode Z-28 安定合格
- 両側に既知の制限：yansh task1/2 plan pre-flight 欠落、yscode streaming は fallback に依存

---

*データファイル：`C:\Users\ShengYan\Projects\AB-test\SUMMARY_v3.md` ~ `SUMMARY_v7.md`*
