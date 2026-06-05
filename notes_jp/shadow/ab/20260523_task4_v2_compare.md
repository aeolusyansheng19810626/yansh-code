# Task #4 v2 検証：4 つの yansh 不具合の修正が有効

[`./20260523_task4_compare.md`](./20260523_task4_compare.md) に続く。task #4 の初回実行で 4 つの yansh 側の問題が見つかり、各々を修正してから同じ prompt + 同じ bug 状態で再実行して検証した。

## 4 つの修正方法

### #1（真の bug）`--json` モード stdout が汚染される

**原因**：`agent.set_batch_mode(json_output=True)` は `agent.py` 自身の `console` のみを再バインドしており、他の 7 つのモジュール（`snapshot.py / hil.py / monitor.py / task_log.py / subagent.py / llm_client.py / main.py`）は各々 `console = Console()` を持ち、再バインドされていないため、Rich レンダリングを stdout に書き込み続けていた。

**修正方法**：`console_shared.py` を抽出して `_ConsoleProxy` シングルトンを使用。すべてのモジュールで `from console_shared import console` とする。`set_json_mode(True)` で inner Console を `sys.stderr` に切り替える。`agent.set_batch_mode(json_output=True)` は 1 行で `_set_json_mode(True)` を呼び出し、すべてのモジュールで同時に有効になる。

**変更内容**：9 ファイル / 正味 +/-32 行（機械的な置換がメイン、agent.py の実質的な変更は `set_batch_mode`）+ 新しいファイル `console_shared.py` / `pyproject.toml` の `py-modules` に 1 行追加。

### #2（命名の混乱）`--mode` help テキストが説明を欠く

**修正方法**：`main.py:argparse` に各 mode の説明を追加——`auto=plan + 人工確認 + code + test + fix；code=auto と同じだが人工確認をスキップ（ただし plan は実行）；plan=計画のみ出力して実行しない；audit=読み取り専用分析`。

### #3（低頻度）plan フェーズで LLM が偶発的に空のコンテンツを返して JSON retry をトリガー

**修正方法**：`_call_with_json_retry` が空のコンテンツを検出した場合、空の assistant メッセージを retry に含めず、元の prompt を直接再送信する（token も節約でき、ICA の空 assistant 拒否も避けられる）。

### #4（動作）dispatch_subagent が早すぎるタイミングで派遣される

**原因**：`tools_schema.py` の dispatch_subagent description には既に「使用しない場合：単一ファイル読み込み / 1 回の grep のような簡単なタスクは下層ツールを直接呼び出す方が安い」と記載されていたが、task #4 v1 では LLM は依然として「3 つの関数 + 1 つのテストを読む」explorer subagent（23K haiku tokens）を派遣していた。schema description の重みが不足していた。

**修正方法**：`_CODER_ROLE` の Tool-call efficiency セクションに「小さなタスクには dispatch_subagent を使わない」という条項を追加し、❌ アンチパターンと ✓ 正しい使用例をそれぞれ 1 つ付ける。システム prompt は schema description よりもはるかに重みが高い。

## 再実行データ比較

| 項目 | v1 yansh | **v2 yansh (修正後)** | 改善 |
|---|---|---|---|
| duration | 87.9s | 89.4s | ~ |
| ツール呼び出し | 24 | **21** | -12% |
| 総 tokens | 249K | **233K** | -7% |
| sonnet input | 226K | 230K | +2%（独自探索でさらに読み込み） |
| haiku input | 23K | **0** | -100% ✓ #4 修正成功 |
| dispatch_subagent | 1 | **0** | ✓ #4 修正成功 |
| --json stdout | 汚染（`[スナップショット]` / `--- diff:` 等混在） | **純粋な JSON 単一行** | ✓ #1 修正成功 |
| `json.loads(stdout)` | ❌ 直接失敗 | **✓ 1 回で成功** | ✓ #1 修正成功 |
| JSON retry 回数 | 1（偶発） | 0 | （今回は非発生、#3 偶発は再現困難） |
| test_result | pass | pass | = |
| 修正の文字列 | slugify | slugify | =（共通の盲点：双重検証の resolve をスキップ） |

コスト：sonnet $3/M、haiku $1/M（粗推定）
- v1: 226K × 3 + 23K × 1 = $0.701
- v2: 230K × 3 + 0 = $0.690
- **正味約 2% 削減**——ただし**質的な変化**はツールチェーンの親和性向上（stdout がマシンで解析可能）

## v2 vs CC 子 agent

| 項目 | v2 yansh | CC 子 agent |
|---|---|---|
| 実行時間 | 89.4s | 31.3s |
| ツール | 21 | 6 |
| Token | 233K | 63K |

CC はまだ約 3.7 倍リード、だが yansh の差は「4× tokens + マシン解析不可」から「3.7× tokens + ツールチェーン利用可能」に縮まった。bug-fix タスクにおける CC の優位性は依然存在、なぜなら yansh の plan→code パイプラインは 1 ファイルの小規模変更に対して構造的なオーバーヘッドをもたらすためである。

## 副次的発見：5 ラウンドツール呼び出し上限の枯渇

v2 stderr に出現：
```
[警告] memory.py は 5 ラウンドツール呼び出し上限を使い果たしました
[警告] tests/unit/test_memory.py は 5 ラウンドツール呼び出し上限を使い果たしました
```

`agent.py:1800` は Coder フェーズの各 plan file に対する 5 ラウンドツール呼び出しを制限している。今回 LLM は Coder フェーズで反復的に pytest コマンドを試行——`cd /workspace && python -m pytest ...`——だが workspace の実際の位置は yansh-code ルートディレクトリであり、cd /workspace は存在しない `/workspace` ディレクトリに落ちるため、pytest は実行対象がない。LLM は反復的にバリエーション（`-v` / `--tb=short` / `2>/tmp/test_out.txt; cat` / `hexdump` / `strings` など）を試しても テスト出力を得られず、5 ラウンド枯渇。

最終的には LLM は正しく修正した（search_in_files で直接 memory.py を確認して bug を発見）が、**これは新たに発見された LLM の動作問題**：LLM の yansh の workspace パス認識に偏差があり、`/workspace` のような Docker スタイルのパスを想定する傾向がある。

推奨される修正方法（**未実装、参考までに記録**）：
- plan フェーズのシステム prompt で明示的に LLM に現在の workspace の絶対パスを告知（`/get_workspace()` は既に渡されたが tree_output のみ）
- または execute_command ツールの description で yansh が /workspace に chroot しないことを説明

## 状態

✓ #1 / #2 / #4 修正方法が有効  
⚠ #3 偶発問題、今回は非発生、後続の再実行で遭遇したら対応予定  
🆕 副次的発見 LLM の "/workspace パス" 仮定問題、バックログに記入

## データファイル

- `20260523_task4_v2_yansh.json` —— **現在はクリーンな単一行 JSON、直接 `json.loads` 可能**
- `20260523_task4_v2_yansh_stderr.log` —— stderr は完全な実行テストプロセス（5 ラウンドの警告を含む）
