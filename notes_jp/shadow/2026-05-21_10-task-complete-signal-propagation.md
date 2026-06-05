# P0 #3 第二波：task_complete シグナル全フロー貫通

[./2026-05-21_09-p0-3-live-validation.md](./2026-05-21_09-p0-3-live-validation.md) の実操に続き、発見された欠陥——LLM が主動的に task_complete を呼び出した後、シグナルが fix loop 内でのみ終了し、**外層の run() の attempts ループと最終結果に伝播していなかった**。このセッションではシグナルチェーンをトップレベルまで接続する。

## 何を変更したか

### fix() が dict を返す

```python
def fix(...) -> {"early_exit": bool, "success": bool, "summary": str}
```

3つのリターンポイント：
- LLM が task_complete を呼び出す → `{"early_exit": True, "success": ..., "summary": ...}`
- サイレント終了（ボトムアップで既に質問されても沉黙） → `{"early_exit": False, ...}`
- ソフト上限消費 → `{"early_exit": False, ...}`

### code() が Optional[dict] を返す

ファイルごとの inner loop で dispatch 後、`_task_complete` sentinel をチェック：
- `success=False` → 直ちに return（Coder が主動的にタスク全体を放棄、後続ファイルをスキップ）
- `success=True` → 本ファイルの inner loop から抜け出し、次ファイルに進み、最終的に signal を return
- sentinel に全く遭遇しない → None を return（旧呼び出しとの互換性を保持）

### run() が 2 経路のシグナルを受け取る

ステージ 2 後：`coder_signal.success=False` → review/test/fix をスキップして直接失敗とマーク。

ステージ 3 の attempts ループで fix() が fix_signal を返す：
- `early_exit=True, success=True` → "LLM が主動的にタスク完了を宣言（残りは pre-existing）"、success でマークして終了
- `early_exit=True, success=False` → "LLM が主動的に放棄"、fail でマークして終了
- `early_exit=False` → 通常の attempts += 1 で retry を続行

linter ステージの fix() 呼び出しも同様に success=False を認識して終了。

### report() に task_complete_signal フィールドを追加

オプショナルフィールド。None の場合は出力されず、歴史的な result スキーマの汚染を避ける。

### ついでにバグを 1 つ修正

Coder/linter の早期終了時 `report(False, None, ...)` が外層の main.py で
`res["test_result"].get("stderr")` をクラッシュさせていた。ダミー dict
`{returncode: -1, stdout: "", stderr: "<放棄理由>"}` に変更し、test_result インターフェイスを統一。

## 検証

### ユニットテスト（tests/unit/test_agent_loop.py を新規作成）

8 つのテストケースすべて合格：
- fix() task_complete(true/false) → early_exit dict 形式が正確
- fix() 2 回のサイレント終了 → early_exit=False
- code() Coder が放棄 → 直ちに early_exit signal を return
- code() Coder が完了 → signal を return；Coder がサイレント → None を return
- report() task_complete_signal フィールドあり/なしの両形式

### 統合検証（シナリオ A/B を再実行）

| シナリオ | 修正前 | 修正後 |
|---|---|---|
| A pre-existing | attempts=3 → fail、47s | **attempts=0 → success**、33s |
| B 矛盾タスク | attempts=0 → pass（誤り）、31s | **attempts=0 → fail**（正確）+ stderr に放棄理由を含む、2s |

シナリオ B で 31s → 2s に短縮——Coder が主動的に放棄すると、外層は直ちに終了し、無用なテスト + fix retry は実行されない。

### Pre-existing 失敗の保持

`python tests/run_unit.py`：10/10 ファイル通過（新しく追加された test_agent_loop.py を含む）。

## 評価

**このセッションは task_complete プロトコルを"内部ループ終了のプライベートシグナル"から"全フロー外部決策のパブリックコントラクト"にアップグレード**。

最小変更、最大レバレッジ：

- fix() / code() それぞれ ~10 行の return signal を追加
- run() に ~30 行の signal 認識ロジックを追加
- report() に 1 つのオプショナルフィールドを追加

**実装 + ユニットテスト + 統合検証の 3 層確認**：
- ユニットテストで各 return ポイントの形式をカバー
- 統合テストでエンドツーエンド動作の差異をカバー（シナリオ A/B の修正前後比較）
- 実際の LLM 呼び出しで Sonnet 4.6 の task_complete 呼び出し動作が予想通りであることを確認

次のセッション（今回の範囲外）：task_complete signal を `task_log` に永続化して、リプレイ/統計が LLM の主動宣言履歴を追跡できるようにする。
