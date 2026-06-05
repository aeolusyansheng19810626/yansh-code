# P2 #9b サブ Agent の並行実行

[_18](./2026-05-22_18-subagent.md) を引き継ぐ：ユーザーから良い質問が出た——"大規模プロジェクトでは主 agent が1つだけサブ agent を派遣するか、それとも量に応じて複数派遣するか？"

## 現状（_18 完成時）

- LLM は1回の response で複数の dispatch_subagent tool_call を返すことができる（OpenAI プロトコルで対応）
- しかし主 agent ループは **for tc in msg.tool_calls での直列処理**——前のものが完了してから次のものが実行
- N 個のサブ agent の総実行時間 = N × 単一の実行時間

Claude Code の Task ツール：並行実行が可能——そのシステムプロンプトで明確に記載されている
「send them in a single message with multiple tool uses so they run concurrently」。
yansh のこの波では、この機能を補う。

## 何が変わったか

### 1) グローバル flag `_IN_SUBAGENT` → `threading.local`

複数のサブ agent を並行実行するときは各スレッドが独立——`_subagent_state.in_subagent`：
```python
_subagent_state = threading.local()
def _is_in_subagent(): return bool(getattr(_subagent_state, "in_subagent", False))
def _set_in_subagent(v): _subagent_state.in_subagent = bool(v)
```

実際の再帰防止は「ツールセットの物理的フィルタリング dispatch_subagent」で行われる——thread-local flag は同一スレッド内の追加保険に過ぎない。

### 2) `_SUBAGENT_STATS` に `threading.Lock` を追加

並行カウンタ更新は原子的である必要がある：
```python
with _SUBAGENT_STATS_LOCK:
    _SUBAGENT_STATS["calls"] += 1
    ...
```

### 3) `_dispatch_tool_calls(tool_calls, *, ...)` ヘルパーを抽出

**コア戦略——dispatch_subagent についてのみ並行実行**：
- ローカルツール（read/grep/list_files）は数ミリ秒で、並行化のオーバーヘッドはメリットがない
- 書き込みツールは直列実行が必須（HIL/confirm の順序依存、コンソール出力の可読性）
- サブ agent は唯一の長実行時間（複数ラウンドの LLM 呼び出し）で、並行実行の効果が最大

実装：
```python
sub_indices = [i for i, tc in enumerate(tool_calls)
               if tc.function.name == "dispatch_subagent"]

if len(sub_indices) >= 2:
    with ThreadPoolExecutor(max_workers=min(len(sub_indices), 4)) as ex:
        # subagents を並行実行
        ...

# 残りを直列処理（単一 subagent とすべての非 subagent ツールを含む）
for i, tc in enumerate(tool_calls):
    if outs[i] is None:
        outs[i] = _dispatch_tool_call(tc, ...)

# 元の順序で messages に戻す（OpenAI プロトコルは tool_call と tool result の順序対応を要求）
for out in outs:
    _record_dispatch(out, messages)
```

`_SUBAGENT_CONCURRENCY_CAP = 4`——スレッドプールサイズの上限。

### 4) 4 か所の tool_calls ループを置き換え

`audit() / plan_chat() / _run_subagent() / fix()` / `code()` 内のループをヘルパーに変更。
sentinel 検出（task_complete / plan_draft_update / exit_plan_mode_signal）は「実行中に検出」
から「すべて実行後に outs をスキャン」に変更——並行実行後にすべての結果が揃った後で統一処理。

`_auto_generate_tests` は変更なし——独自の特殊ツール処理がある（_dispatch_tool_call を使用しない）。

### 5) dispatch_subagent スキーマの説明に並行化ヒントを追加

> 「**複数ブランチの並列調査**——1回の response で複数の dispatch_subagent tool_call を送ると、**並行実行**
> （最大4つを同時に）され、総実行時間≈max(単一) ではなく sum。例：3つのモジュール A/B/C の使用方法を分析する場合、
> 1回で3つの dispatch_subagent を送ると直列查より3倍高速。」

## 検証

### ユニットテスト（tests/unit/test_subagent.py、新規追加 7 件、合計 29 件すべてパス）

新規カバレッジ：
- `test_dispatch_tool_calls_helper_serial_for_non_subagent` — 非 subagent は直列処理 + outs の順序確認
- `test_dispatch_tool_calls_concurrent_subagents` — 3つの subagent、max active ≥2 + 総実行時間 < 0.8s（直列 0.9s）を検証
- `test_dispatch_tool_calls_single_subagent_serial` — 1つの subagent はスレッドプールを起動しない
- `test_dispatch_tool_calls_concurrency_capped` — 6つの subagent + cap=4、max active ≤ 4 を検証
- `test_dispatch_tool_calls_mixed_subagent_and_local_tools` — 混合ツール時に outs が厳密に元の順序
- `test_dispatch_tool_calls_subagent_exception_isolated` — 1つの subagent のエラーは他に影響しない
- `test_subagent_stats_lock_concurrent_increments` — 5つの並行実行後に stats.calls=5（ロック有効性を検証）

旧テスト2件を改修：`agent._IN_SUBAGENT = True` → `agent._set_in_subagent(True)`；
`agent._IN_SUBAGENT is False` → `agent._is_in_subagent() is False`。

13/13 ファイルすべてパス。

### 統合ベンチマーク（ICA Sonnet 4.6、3つの完全独立タスク prompt cache を回避）

```
3つの subagent の直列実行: 33.3s
3つの subagent の並行実行: 13.9s
高速化比: 2.40×
```

理論上限は3×（3つの独立 LLM 呼び出しが同時実行）、実際の2.4× は以下の理由：
- prompt cache miss ペナルティ（最初の直列実行の cache 未命中が最初の直列に分散）
- ICA ゲートウェイには token レート制限がある——3つの並行実行の token スループットが制限される
- LLM 応答時間自体には分散がある（最速が最遅を遅延させる）

**統合 audit の実行結果**（主 agent が dispatch_subagent で3つを派遣して3つのファイルを確認）：
- 主 agent が1回の response で3つの dispatch_subagent tool_call を返す ✅
- コンソール出力 `[audit ラウンド N] [subagent 並行] 3つのサブ agent が同時に起動` ✅
- stats.calls=3, total_steps=6（各2ステップ）✅
- 主 agent が3つのサマリーに基づいて集計表を提供 ✅

## 評価

### Claude Code にどの程度近づいたか

| 側面 | Claude Code Task | yansh dispatch_subagent |
|---|---|---|
| context 隔離 | ✅ | ✅（_18） |
| role 切り替えツールセット | ✅ | ✅（_18） |
| 入れ子再帰防止 | ✅ | ✅（_18） |
| **1回で複数送ると並行実行** | ✅ | ✅ **本波** |
| バックグラウンド実行 (run_in_background) | ✅ | ❌ |
| 完全なサブ agent トランスクリプト参照可能 (TaskOutput) | ✅ | ❌（last_summary のみ 500 文字切り詰め） |
| サブ agent の主動キャンセル (TaskStop) | ✅ | ❌ |

ギャップは「observability + バックグラウンド実行」に縮小され、もはやコアアーキテクチャの違いではない。

### 工学的意義

並行実行の真の価値は「3倍高速」ではない——**LLM にタスク分割を学習させること**。
スキーマに「3つのモジュール A/B/C の使用方法を分析するには1回で3つの dispatch_subagent を送ると良い」というヒントを追加した後、
Sonnet 4.6 は自由な audit タスクで**主動的に並行実行する**——人間の指示は不要。
これこそが「並列思考」を agent の行動パターンに内化させることだ。

### エッジケース（検証済み OK）

- 1つの並行 subagent がエラー → 他に影響なし；該当 subagent の result は internal error
- 単一 dispatch_subagent → スレッドプールを起動しない（不要なスレッド作成オーバーヘッドを回避）
- dispatch_subagent + read_file の混合 → outs/messages は厳密に元の順序
- 5つの並行実行での stats 同時更新で計数損失なし（ロック有効）
- 同期スレッド内で `_set_in_subagent(True)` 後に `_run_subagent` を呼び出し → 仍然再帰遮断

## しないこと（後続に委譲）

- サブ agent のバックグラウンド実行：派遣後に親 agent をブロックしない、イベントコールバック（Claude Code の `run_in_background` を参照）
- サブ agent の途中キャンセル：親 agent が B が不要だと判断、該当スレッドを主動的に stop
- サブ agent の完全なトランスクリプト回査：現在の last_summary は 500 文字切り詰めのみ；完全な messages には
  ディスク書き込み（または in-memory ring buffer 追加）が必要
- スレッドプール再利用：現在は毎回 helper 呼び出し時に新しい ThreadPoolExecutor を作成、N 回の並行実行で N 個の
  pool を作成。pool 再利用は thread-local 状態リークに注意が必要

## キーファイル

| ファイル | 変更 |
|---|---|
| `agent.py` | `_IN_SUBAGENT` → `threading.local`；`_SUBAGENT_STATS_LOCK`；`_SUBAGENT_CONCURRENCY_CAP=4`；`_is_in_subagent` / `_set_in_subagent`；`_dispatch_tool_calls` ヘルパー；4か所のループ置き替え |
| `tools_schema.py` | dispatch_subagent description に並行化ヒントを追加 |
| `tests/unit/test_subagent.py` | +7つの並行テスト、旧2つは新 API を使用 |
