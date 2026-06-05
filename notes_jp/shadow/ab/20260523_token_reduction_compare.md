# Token 削減検証：P1.0 + P1.2 + P1.3 + P2.1 + P2.2 実行後に task #2/#3 を再実行

**日付**：2026-05-23
**比較対象**：
- baseline = 同日 task #2 / #3 初回実行（commit 137b647 / 52971b5 前の yansh 状態）
- rerun = commit a6fad9c 以降（P1.0/P1.2/P1.3/P2.1/P2.2 を含む）

プロンプトと baseline は完全に一致。両回とも `--cwd .`、`--mode auto/audit`、sonnet 4.6 をメインモデルとして使用。

## 概要

| 項目 | task #2 baseline | task #2 rerun | Δ | task #3 baseline | task #3 rerun | Δ |
|---|---|---|---|---|---|---|
| 実行時間 (s) | 254 | **402** | +58% ❌ | 156 | **203** | +30% ⚠ |
| ツール呼び出し | 61 | **75** | +23% ❌ | 50 | **55** | +10% |
| 総 tokens (in+out) | 641K | **1722K** | **+169%** ❌ | 730K | **729K** | -0% |
| sonnet input | 627K | 1043K | +66% | 716K | **53K** | **-93%** ✓ |
| haiku input | 0 | 663K | (新規) | 0 | **658K** | (新規) |
| テスト結果 | pass | **fail** | ❌ | pass | pass | |
| fix attempts | 1 | **3 (最大)** | | n/a | n/a | |
| 推定コスト ($) | ~1.88 | ~3.79 | **+101%** ❌ | ~2.15 | **~0.82** | **-62%** ✓ |

コスト推定：sonnet $3/M-input、haiku $1/M-input（概算、output は含まず）。

## 結論

**task #3（読み取り専用の議論）は顕著な効果**：
- sonnet 使用量が 716K → 53K に削減。P2.2 により explorer/auditor サブエージェントが haiku に切り替わり、2 つのサブエージェントが大部分の探索を実行
- 総 token 数は減っていないが、**コストが 62% 削減**（haiku の単価は sonnet の 1/3）
- ツール呼び出しの品質は低下していない：baseline と同程度の議論構造を提供

**task #2（コード作成 + fix ループ）はむしろ悪化**：
- P1.3 のテストスコープが有効（`test_command: pytest tests/unit/test_tools.py`、正しい）
- しかし LLM は fix ループで **baseline のように早期終了していない** ——baseline では 1 回の attempt 後に LLM が「5 つの失敗は既存の問題で修正の対象外」と認識して task_complete を呼び出し；rerun は 3 attempts を最大まで実行し、最終的に `test_result: fail`
- 追加で消費された ~1M tokens は全て、fix ループ内でテストを反復実行し、コードを修正し、再度テストを実行するサイクルから発生
- P1.2 の英語プロンプトが「関連のない失敗を認識し、修正せず直接終了」という heuristic を弱めたと推測——baseline の中国語版 `_TESTER_ROLE` / fix() プロンプトはおそらく、より具体的な「関連のない失敗は直接 task_complete」のヒントを含んでいた

## モデル別 token 詳細

### task #2 rerun
| モデル | input | output |
|---|---|---|
| sonnet 4.6 | 1,042,683 | 13,259 |
| haiku 4.5 | 663,052 | 4,207 |

### task #3 rerun
| モデル | input | output |
|---|---|---|
| sonnet 4.6 | 52,641 | 5,548 |
| haiku 4.5 | 658,166 | 12,378 |

## 各段階の検証

| パッチ | 期待される効果 | 実際の効果 |
|---|---|---|
| P1.0 ICA cache_control 透過 | 検証：透過なら P1.1 へ進む | ❌ 透過されていない（cache_creation/read は両方 0 を測定）、P1.1 はスキップ |
| P1.2 英語化 system prompt | input を 30-40% 削減 | ⚠ 短期的には sonnet input が逆に増加（fix ループが制御不能）、独立検証不可 |
| P1.3 fix loop test scope | 全 pytest を実行しない | ✓ task #2 の実際のコマンド `pytest tests/unit/test_tools.py`（本タスクのみ関連） |
| P2.1 read_file ヒット検出 | 30-40% の再読を検出 | 個別に測定していない、task #2 の read_file 呼び出し数は依然多い |
| P2.2 subagent を haiku に切り替え | サブエージェント部分で約 70% のコスト削減 | ✓ 顕著：task #3 の sonnet 使用量が 93% 削減、サブエージェントは全て haiku を使用 |

## やることリスト

1. **P1.2 をロールバックまたは微調整**：fix ループの早期終了が機能していないのは実際の問題。2 つの選択肢：
   - a) fix ループ user message に明示的なヒントを追加：「失敗が明らかに今回の plan と無関係の場合（既知の既存の失敗など）、`task_complete(success=true, summary='...既存の失敗は修正不要')` で直接終了してください」
   - b) `_TESTER_ROLE` / fix prompt を部分的に中国語にロールバック（他の role は英語のまま）
   - a を推奨：英語化による token 削減の利益を保持し、具体的なルールを 1 行追加するだけ。
2. **P2.1 read cache ヒット率の測定**：キャッシュ ヒット率をログに記録する機能を追加し、次回 task #3 で実際の read 節減量を確認。
3. **task #2 再実行**：P1.2 の fix ループ退化を修正した後、再度実行。期待値 ~250K（baseline の約半分で、sonnet→haiku ディスカウントが適用）。

## データファイル

- `20260523_task2_rerun_yansh.json` / `_stderr.log` (v1, 失敗)
- `20260523_task2_rerun_v2_yansh.json` / `_stderr.log` (v2, 修正後)
- `20260523_task3_rerun_yansh.json` / `_stderr.log`
- baseline：`20260523_task2_yansh.jsonl` / `20260523_task3_yansh.json`

## v2 検証（cce571a + 174df32 でプロンプト修正後に task #2 を再実行）

修正の中核：
- `_TESTER_ROLE` Example 3 に反例を追加（アサーションの弱体化を禁止）
- `fix()` user message で plan ファイルをリストアップして LLM が「このタスクの範囲」を明確に把握でき、Investigation order の第 1 項目に基づいて帰属判定を行う；`notes/shadow/` のような yansh-self-codebase の偶然の産物には依存しない
- コード内のバグを修正：plan は `files` キーを含む辞書だが、最初のバージョンでは list として反復し、plan_files が常に空になってしまった（LLM は誤読して「すべてが範囲外」と判断し、たまたま早期終了；rerun 174df32 後は本当に帰属判定に基づいて実行）

### task #2 v2 データ

| 項目 | baseline | v1 (失敗) | **v2 (修正後)** |
|---|---|---|---|
| 期間 (s) | 254 | 402 | **219** ✓ |
| ツール呼び出し | 61 | 75 | **28** ✓ |
| 総 tokens | 641K | 1722K | **754K** |
| sonnet input | 627K | 1043K | **747K** |
| haiku input | 0 | 663K | 0 |
| 推定コスト ($) | ~1.88 | ~3.79 | **~2.24** |
| test_result | pass | fail | **pass** ✓ |
| linter fix attempts | 1 (早期終了) | 1 (作成失敗) | **1 (早期終了)** ✓ |
| test fix attempts | 1 | 3 (最大) | **1 (早期終了)** ✓ |
| アサーション弱体化？ | ❌ なし | ⚠ 5 ヶ所 | ❌ **なし** ✓ |
| 付随する変更品質 | 未使用の import 3 個を削除 | （+ 5 ヶ所のアサーション弱体化、悪い）| **`_read_cache_key` で max_bytes のキャッシュミスヒットバグを修正** ✓ |

### 解釈

- **動作は正確**：linter attempt 1 は早期終了（218 個の ruff エラーが plan ファイル範囲外であることを正しく認識）；test attempt 2 は早期終了（5 個の既存の失敗が範囲外であることを正しく認識）。両段階とも修正を試みていない
- **token は baseline より若干高い (+18% sonnet)**：新しい anti-pattern few-shot + 明示的な plan_files ヒントはシステムプロンプトの常時インクリメント。しかし**品質**は baseline より顕著に向上——v2 は偶然にも真のバグを発見して修正（`_read_cache_key` で max_bytes をキーとして含めていなかったため、異なる max_bytes の read_file 呼び出しが誤ってキャッシュをヒット）
- **vs v1**：tokens を 56% 削減 (1.72M → 754K)、ツール呼び出しを 63% 削減 (75 → 28)、fail → pass に改善

### 3 つのタスク統合

| タスク | baseline tokens | v2 tokens | Δ tokens | v2 コスト vs baseline |
|---|---|---|---|---|
| #2 (コード作成 + fix ループ) | 641K | 754K | +18% | +19% |
| #3 (アーキテクチャ議論 + subagent) | 730K | 729K | 0% | **-62%** ✓ |

P2.2（subagent を haiku に切り替え）は #3 タイプのタスクで大きな効果がある；P1.2/P1.3/prompt 修正は #2 タイプで動作の退化を回避しているが、token 数自体は顕著に削減されていない。**結論**：コスト削減はアーキテクチャ上は P2.2 に依存し、品質の安定性はプロンプト反例 + 帰属ルールの明示化に依存。
