# AB Test #1：探索タスク — `_dispatch_tool_calls` 並行条件

**プロンプト**（両側一致）：
> yansh-code プロジェクトで `_dispatch_tool_calls` という関数を見つけ、いつ並行実行し、いつ直列実行するかを教えてください——ファイルパス + 行番号 + トリガー条件を含めて。

**モデル**：Claude Sonnet 4.6（両側）
**日付**：2026-05-23
**タスクタイプ**：純粋探索 / 読み取りのみ

## データ比較

| 維度 | yansh (audit mode) | Claude Code サブエージェント (general-purpose) |
|---|---|---|
| 実行時間 | **24.54s** | **21.6s** |
| ツール呼び出し数 | **2** | **4** |
| ツール順序 | `get_symbol_definition` → `task_complete` | `Grep` × 3 → `Read` × 1 |
| 総 tokens | 未記録（`task_log` 未保存） | **20,365** |
| 正確性 | ✓ 正確 | ✓ 正確 |
| 完了度 | ✓ done | ✓ done |

両側の結論は一致：`_dispatch_tool_calls` は `agent.py:1115-1174` にあり、並行条件は「同一ラウンド ≥2個 dispatch_subagent」。

## 決定パス差異 ⭐ 最も興味深い発見

**yansh は「シンボルレベルの検索」パスを選択**：
```
get_symbol_definition(symbol_name="_dispatch_tool_calls")
  → 一発で命中：関数本体 + 行番号 + docstring を返す
  → task_complete
```

**CC サブエージェントは「テキストベース検索」パスを選択**：
```
Grep("_dispatch_tool_calls")          # 出現位置を検索
Grep("_dispatch_tool_calls", "agent.py")    # 範囲を絞る
Grep("_dispatch_tool_calls", "subagent.py") # subagent も試す（誤射）
Read("agent.py")                       # 全ファイル読み込み確認
```

**根本原因**：
- yansh のツールセットには **`get_symbol_definition`** がある——tree-sitter ベースで、「関数/クラス定義を見つける」タスクに直接定位
- CC サブエージェントの general-purpose ツールセットは **`Grep` + `Read`** のみ——テキスト検索後にファイルを読み込む必要があり、関数定義タスクで2ステップ多い

これは「どちらがより賢いか」の問題ではなく、**ドメイン固有ツール vs 汎用ツール**のトレードオフ：
- yansh は「コードエージェント」で、ツールセットに symbol-aware なツールを装備 → コード探索タスクのパスが短い
- CC サブエージェントは general-purpose（様々な知識作業）で、より基本的なプリミティブを使用 → パスは長いが汎用性が高い

## 出力スタイル差異

**yansh** には実際に **2層出力**がある：
1. **stderr**（rich console）：完全 markdown レポート——セクション分割、表、ソースコード引用
2. **stdout** JSON：簡潔 task_complete サマリー（1行）

**CC サブエージェント** は単層：
- コンパクト：4行の結論 + ファイルパス + JSON メタデータブロック（要件に従い添付）

「ユーザー画面に表示されたもの」だけを見ると——yansh はより詳細（stderr console 完全 markdown）を表示し、CC はコンパクトで実用的。

## 決定深度差異 ⭐

**yansh はソースコード docstring 原文を引用**（設計意図）：
```
ローカルツール 数ミリ秒、並行オーバーヘッドが見合わない → 常に直列
ツール作成は直列が必須（HIL/確認順序依存） → 常に直列
サブエージェントは唯一の長耗時操作 → ≥2 個のときに並行
```
このセクションは `agent.py:1118-1124` から読み込まれたもの——`get_symbol_definition` が一度に**関数本体 + docstring** を返すため、yansh は「どこにあるか」だけでなく「なぜこのように設計されたか」も知っている。

**CC サブエージェント は docstring を読まなかった**——grep で行番号を取得し、ファイルを読み込んだが、読み込んだのはファイル全体で、回答はコード論理のみで設計意図を含まない。

これはシンボルレベルツールのもう1つの隠れた価値：**docstring と一緒に返す → 回答が1階層上がる**（「コードが何をするか」から「なぜこのようにするか」へ）。

## LLM 実行ラウンド数

- **yansh: 2 ラウンド**（監査ラウンド 1 = get_symbol_definition；監査ラウンド 2 = task_complete）——初回で答え取得、2回目で終了
- **CC サブエージェント: 1 ラウンド LLM 含む 4 ツール呼び出し**（現在の Anthropic API は 1 回の LLM 応答で複数 tool_call が可能）

yansh の 24.54s には **2 回の** API ラウンドトリップ + 1 回の tree-sitter parse が含まれます；CC の 21.6s は **1 回の** ラウンドトリップ + 4 ツール計算。両側のウォールクロック時間はほぼ同じです。yansh のツール計算は安価（tree-sitter 単発 < 50ms）、CC の API ラウンドトリップは単発高額ですがツールは安価。

## cwd 実証差異

- **yansh** 本当に `/tmp/ab_test/yansh-clone/agent.py` を読み込み（`--cwd` で強制）
- **CC サブエージェント** 実際に `C:/Users/ShengYan/Projects/yansh-code/agent.py` を読み込み（**プロンプトの cwd ヒントを無視して、自分で main プロジェクトを選択**）

コード内容は同じなので結論は一致しますが、**隔離レベルが異なる**——これは task #2/#3 で修正が必要な場合に解決する必要がある問題です。

## データ収集 gap（パイプライン問題）

1. **yansh `--json` は token 数を保存しない**——`task_log.py:_current_task_log` に token フィールドがない。`task_log_signal` も同様。コミットを追加する必要：`llm_client._session_tokens_by_model` からデータを引き出して保存。
2. **CC サブエージェント は cwd を強制できない**——`/tmp/ab_test/yansh-clone` をプロンプトしましたが、実際に `C:/Users/ShengYan/Projects/yansh-code/`（元の main）を読み込み。このタスクでは両者のコードが同じなので影響なし、しかし task #2 / #3 で修正が必要な場合この隔離破洞は本当。後でCC サブエージェントが clone ディレクトリに実際に cd できるようにする。

## パイプラインキャリブレーション結論

✅ 両側とも実行可能、構造化結果取得可能、比較可能
⚠️ yansh の token データが漏れている、task #2 前に修正必要
⚠️ CC サブエージェント cwd 実際動作確認必要

## 後続提案

task #2（`tools.read_file` に `max_bytes` パラメータ追加）前に実施すべき：
1. `pyproject.toml` の 10 個のpy-modules を修正（`yansh` CLI が動作できるように fallback `python main.py` ではなく）
2. `task_log` に token フィールド追加（`llm_client` から引き出し）
3. CC サブエージェント プロンプトに明示的に `cd /tmp/ab_test/yansh-clone &&` コマンドスタイルの指導を含める

継続しますか？
