# P0 #3 実施検証：yansh を実行して強化を検証

[./2026-05-21_08-prompt-and-loop-hardening.md](./2026-05-21_08-prompt-and-loop-hardening.md) に続く。
ノート _08 で prompt の強化と フォールバック を完了した後、yansh を 3 つのシナリオで直接実行してプロトコルが実装されているかを検証。

モデル：claude-sonnet-4-6（デフォルト）；workspace：C:/tmp/yansh_test_p0_3/scenario_{a,b,c}

## シナリオ C：シンプル成功パス

タスク：`calc.py に multiply + テスト追加`（空の workspace、干渉なし）

結果：
- duration 16.5s、attempts=0（一度で成功）
- tool_calls：`write_file × 2 + task_complete(success=true, summary=...) × 1`
- ✅ Coder フェーズが、コード作成とテスト成功後に**主動的に task_complete を呼び出した**——プロトコル実装成功

## シナリオ A：スコープ抑制 + pre-existing 失敗

タスク：既存の calc.py / test_calc.py に multiply + テストを追加。
workspace に 2 つの `_PRE_EXISTING_BUG` 失敗テストが事前に埋め込まれている。

結果：
- attempts=3（毎回 fix loop に進行）
- 重要な観察：**3 回の fix loop すべてが pre-existing を正しく識別**
  - 毎回 task_complete(success=true) の summary は明確に「`_PRE_EXISTING_BUG` 接尾辞
    + コメント注記で期待値が意図的に誤っている → 本タスクとは無関係、修正スキップ」と説明
  - pre-existing テストには全く触れず、スコープ抑制 100% 成功
- 5 つのテスト：3 成功（新規の test_multiply 含む）+ 2 失敗（pre-existing）

✅ **prompt の表現 + テンプレート 4 スコープ抑制 + few-shot サンプル**が全て機能

⚠️ **ただし、次のステップの脆弱性を露出**：fix() 内の task_complete(success=true) sentinel は
**fix loop** のみ終了させたが、**外層の run() の attempts ループ（最大 3 回）はこの信号を認識しない**——
LLM が既に「タスク完了 + 残りは pre-existing」と述べているのに、外層は引き続きテストの retry を実行し、
最終的に attempts=3/3 で fail としてマークし、token を浪費。

## シナリオ B：矛盾するタスク → task_complete(success=False)

タスク：test_calc.py の 2 つの互いに矛盾するテストの両方を成功させる（1 つは add(2,3)==5、もう 1 つは ==`"hello"`）

結果：
- duration 31.6s、attempts=0
- tool_calls：`read_file × 2 + task_complete(success=false) × 2`
- ✅ Coder フェーズが**主動的に task_complete(success=false) を呼び出し**、summary は明確に
  「2 つのテストは論理的に矛盾しており、両方を満たす合法的な add 実装は存在しない」と説明
- プロトコル機能——LLM は無思考に修正を繰り返さず、主動的に放棄を宣言

⚠️ 同様の次のステップの脆弱性：success=false 信号が**外層の result に伝わらず**、
最外層の `test_result: "pass" / attempts=0` が誤っている——「タスクが主動的に不可能と判定された」ことを反映する必要あり

## 3 シナリオの結論

| 観点 | 状態 |
|---|---|
| Coder/Tester が主動的に task_complete を呼び出し | ✅ 全て呼び出し（success=true と success=false の両方を確認） |
| summary の内容品質 | ✅ 詳細、理由あり、読みやすい |
| pre-existing スコープ抑制 | ✅ 厳格に遵守、余計な修正なし |
| fix() / audit() loop の sentinel 終了を認識 | ✅ 機能（シナリオ A のロ毎回正しく終了） |
| 沈黙終了フォールバック トリガー | ❓ 3 シナリオ全て沈黙終了なし、未トリガー——**prompt 強化により「必ず task_complete」が内在化されたことを示唆**、フォールバックが真の意味でのフォールバック（なくても動く、ただし安全網あり） |

## 次のステップで露出した課題（P0 #3 の「次の波」）

**task_complete(success) 信号が外層フロー に伝わらない**：

- `fix()` が task_complete(success=true) を受け取る → fix loop のみ終了、外層 attempts はまだ retry
- `fix()` が task_complete(success=false) を受け取る → fix loop のみ終了、外層は LLM の主動判定「継続不可」を認識しない
- Coder フェーズが task_complete(success=false) を主動的に呼び出し → 完全に無視され、全体は pass と表示

修正案（候補、次回に留保）：
1. fix() を `{"success": bool, "summary": str, "early_exit": bool}` を返すように変更、外層 run() が `early_exit=True` を見て success に応じて retry または終了を決定
2. Coder フェーズ dispatch も task_complete sentinel を認識、後続の review/test をスキップ、success に応じて全体結果を決定
3. 最終 result に `task_complete_signal: {success, summary}` フィールド追加、CLI 出力が LLM の主動宣言を反映

今回の実施検証で**予期しないこの脆弱性を発見**——強化自体より価値がある。

## 評価

**prompt 強化 + 沈黙終了フォールバック**今回：✅ プロトコル実装、3 シナリオ全て task_complete をトリガー。

**次の波**：task_complete sentinel のセマンティクスを「fix loop 内終了」から「全フロー信号」に拡張——
実施検証自体がこれを発見する最良の方法。
