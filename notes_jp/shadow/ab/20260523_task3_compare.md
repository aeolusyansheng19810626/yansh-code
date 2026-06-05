# AB テスト #3：アーキテクチャ論証 — `task_complete` sentinel → NL 信号の実現可能性

**プロンプト**（両側の一致したコア要件）：
> yansh-code プロジェクトが `task_complete` を sentinel ツールから LLM 自然言語信号に変更する実現可能性を評価します。
> (1) 変更範囲 (2) fix loop / dispatch_subagent / task_complete_signal との互換性 (3) リスク (4) 実施推奨の有無を提供します。
> markdown ドキュメントを出力、コード記述なし、リポジトリ変更なし、テスト実行なし。

**モデル**：Claude Sonnet 4.6（両側）
**日付**：2026-05-23
**タスク種別**：純粋なアーキテクチャ論証 / 読み取り専用分析
**期待値**：ドメイン知識 + テスト pipeline を含まないため、CC は yansh により近い —— 良好な対照グループである

## データ比較

| 次元 | yansh (audit mode) | Claude Code サブエージェント (general-purpose) |
|---|---|---|
| 実行時間 | **155.51s** | **116s** |
| ツール呼び出し | **50** | **12** |
| トークン (in+out) | **730,452** (in 715,956 + out 14,496) | **169,371** |
| ファイル変更 | **0**（audit が強制読み取り専用） | **0**（プロンプト制約） |
| 出力長 | 長形式の構造化ドキュメント（~5KB） | 中程度の構造化ドキュメント（~3.5KB） |
| 推奨結論 | 実施しない | 実施しない |
| 折衝案 | デュアルモード互換 + 厳密なプレフィックス行形式 | ツール + NL 並行（フォールバック） |

**yansh ≈ CC の 4 倍のトークンとツール呼び出し** —— task #2 の 25 倍差から大幅に縮小。**期待値が検証される**：純粋な論証タスクでは yansh の優位性が減弱。

## 完成品質の差異

### yansh の優位：正確な行番号 + 表形式の変更点

yansh は変更点を表にリストアップし、各行に行番号を付与：

| ファイル | 関数 / 位置 |
|---|---|
| `tools.py:35-47` | `task_complete()` |
| `tools_schema.py:~L404` | task_complete JSON Schema |
| `agent.py:1041-1044` | `_dispatch_tool_call()` |
| `agent.py:1616-1630` | `code()` inner loop |
| `agent.py:1736-1750` | `audit()` loop |
| `agent.py:2184-2191` | `fix()` loop |
| `subagent.py:221` | `_run_subagent()` |
| `task_log.py:85-89` | `finish_task_log()` |

**4 ファイル 8 箇所** —— 行番号はすべてリポジトリの現在の状態と一致（yansh は 34 回の read_file で確認）。

CC も 4 ファイルを提供していますが、行番号の粒度がより粗い（`L1042-1044`、`L1619-1629` のような範囲）で、`tools_schema.py` の正確な位置が不足しています。

### CC 独自の洞察（key_decisions 内）

CC の `key_decisions` では yansh が言及していなかった 2 つのトラップを明示的に列挙：

1. **`plan_chat` の `exit_plan_mode_signal` は同種の sentinel**
   > `task_complete` のみを変更し `exit_plan_mode_signal` を保持する場合、システム内に 2 つの終了メカニズムが共存し、設計の一貫性が低下します。

   yansh の方案はこれについて全く触れていません——これは真の問題で、リポジトリの `agent.py:plan_chat()` は同じ sentinel パターンを使用しており、1 つのみを変更すると不一致が残ります。

2. **system prompt の予算通知テキストに "task_complete" 文字列を含む**
   > L1714、L2161 はメッセージに通知テキスト（"task_complete(...) を呼び出す"）を注入し、NL パーサーが `role: system` を区別しない場合、誤りトリガーになります。

   yansh のリスク節に R1（LLM 誤トリガー）がありますが、これら 2 つの注入ポイントに具体的に位置付けられていません。CC はこの隠れたトラップをキャッチしました。

### yansh 独自の洞察

1. **R3：success/failure セマンティクスの喪失** —— ツールには明示的な bool フィールドがあり、NL は 2 次パースが必要；CC の key_decisions にも記載されていますが、表現がやや弱いです。
2. **R6：複数ツール呼び出しの並行シナリオ** —— `_dispatch_tool_calls` が複数ツールを並行処理する際、NL 信号は "call_llm リターンハンドリングレイヤーに前倒し" が必要。CC では触れていません。
3. **`agent.py:2361, 2438, 2477, 2491, 2506, 2513, 2528` の 7 つの出口すべてが task_complete_signal を読むことを監査** —— これは yansh が 50 回のツール呼び出しを行った見返り：grep 後に各 read_file を一つずつ検証し、参照粒度は非常に細かいです。

### 推奨結論が一致

両側とも ❌ 実施しない と判定、理由は収束：
- ツールプロトコル（構造化）が regex 解析（非構造化）より厳密で優れている
- 4 ファイル 8 箇所の変更、NLP 曖昧性のリスク導入、利益なし
- "task_complete を忘れる" シナリオに対して既に `silent_prompted` のフォールバック処理がある

折衝案も両側で類似：**ツール呼び出しを優先、NL をフォールバック**。yansh はさらに source フィールド（`"source": "nl_signal"`）追加による信頼度マーク付けを推奨——これは CC では提供されていません。

## 今回 yansh がなぜ多く費やしたか

50 個のツール呼び出し分布：

| ツール | 回数 | 用途 |
|---|---|---|
| `read_file` | 34 | agent.py / tools.py / subagent.py / task_log.py の異なるセクションを繰り返し読取 |
| `search_in_files` | 8 | grep `task_complete` / `task_complete_signal` / `TASK COMPLETE` / `sentinel` / `fix_loop` |
| `task_complete` | 4 | audit モード各サブタスクで 1 回実行 |
| `dispatch_subagent` | 3 | サブエージェントを派遣し "main.py 制御フロー読み込み" 等のサブタスクを実行 |
| `list_symbols` | 1 | main.py シンボル一覧 |

**興味深い点**：yansh は **3 つの dispatch_subagent** を派遣してサブタスクを自動分割——task #1 と #2 ではこの動作は見られません。audit モード + 大規模分析タスク下で yansh は "main.py 制御フロー"、"task_complete 実装詳細" 等の独立したサブ問題に自動的に並行サブエージェントを分割し、これが yansh の再帰的自己呼び出し能力です。

CC サブエージェントは 12 個のツールのみで方案を提供：CC の sonnet は主レスポンス内で直接統合し、サブタスクを自動分割しません（`Agent` ツールは利用可能ですが使用していません）。

## プロセスの差異

**yansh audit モード**：
- 強制読み取り専用（`READONLY_TOOL_NAMES` でツール集合を限定）
- 複数ラウンド read_file + grep 検証 → dispatch_subagent でサブタスク分割 → 統合
- 最後に `task_complete(success=True)` で明示的に終了、summary を task_log に永続化

**CC general-purpose サブエージェント**：
- 自由なツール集合、しかしプロンプトで厳密に制約（"コード記述なし、リポジトリ変更なし"）
- 単一ラウンド複数ツール呼び出し → 終了前に `key_decisions` 生成
- 主レスポンスで直接リターン、明示的な信号なし

## task_complete_signal の予期しない副産物

yansh が今回実行した task_log には `task_complete_signal.success=True` があり、summary が完全です。**このタスク自体が "task_complete を変更すべきか"を論証する** —— yansh 自体が "論証される対象" として task_complete で終了を迎えました。**自己ループ検証で現在の設計の可観測性の価値が実証**——ログで "yansh が主体的にこの監査の正常終了を宣言" しているのが見え、これが正に task_complete_signal フィールドが存在する価値です。論証結論の "実施しない" も対応：実行エージェントとしての yansh 自体がこのメカニズムの恩恵を受けています。

## 総括：シナリオごとの選択（更新）

| タスク種別 | 推奨 | 倍率 |
|---|---|---|
| 探索 / 情報検索（task #1）| **CC** | yansh ≈ 1.5× |
| 厳密に字面要件の小変更 + テスト追加（task #2）| **CC**（25× 安価、要件ぴったり） | yansh ≈ 25× |
| **完全な機能実装**（schema、ドキュメント、クリーンアップ含む）| **yansh** | 25× 追加費用だが意味のクローズド |
| **アーキテクチャ論証 / 純粋な読み取り専用分析**（task #3） | **深度要件次第** | yansh ≈ 4× |
| 不慣れなコードベース | **yansh**（plan/audit 強制読み取り専用、より安全） | — |

**task #3 新発見**：アーキテクチャ論証タスク下、**yansh 出力より詳細 + 行番号より正確 + サブエージェント自動分割** vs **CC 簡潔 + key_decisions でトラップをより正確にキャッチ（plan_chat アナロジー、system prompt 注入）**。両側とも 4 ファイル 8 箇所のコア変更点と "実施しない" の結論を得ました——判定は一致、プロセスは異なります。

**正確な変更リスト + 行番号参照** を必要とするコード変更開始時には yansh を選択；
**実施を要するかどうかの迅速判定 + 隠れたトラップ通知** を必要とする意思決定には CC を選択。

## データ収集総括

3 回の AB 実行で、CC サブエージェントの異なるタスク種別に対する表現の安定性：

| Task | yansh 優位性 | CC 優位性 |
|---|---|---|
| #1 探索 | docstring → 設計意図 | パスが短い |
| #2 コード記述 | schema クローズド + デッドコード削除 | 25× 安価 + key_decisions が monkeypatch トラップをキャッチ |
| #3 論証 | 正確な行番号 + dispatch_subagent + リスク点がより全体的 | key_decisions が plan_chat アナロジー + system prompt 注入トラップをキャッチ |

**yansh の "ドメイン知識" 優位性** は task #3 で 4× に縮小、仮説を検証——しかし **出力深度はまだ明確な差**、特に yansh が派遣したサブエージェント分割能力。CC の key_decisions フィールドは 3 回のタスク全体で **追加価値洞察** を貢献——これはプロンプト設計の功績で、モデル能力差ではありません。

## 添付原始データ

- `20260523_task3_yansh.json` — yansh バッチ JSON 出力（audit body markdown + metadata 含む）
- `20260523_task3_yansh_body.md` — 抽出した audit markdown body
- `20260523_task3_yansh_stderr.log` — yansh stderr コンソール（rich レンダリング完全方案ドキュメント）
- CC サブエージェント トランスクリプト：親対話内のみ、単独保存なし

## 次ステップ候補

- task #4：バグ再現 / 修正タスク（真のバグ再現ステップを提供、yansh の fix loop vs CC 単一循環デバッグ、誰がより高速に位置付け）
- task #5：クロスファイル リファクタリング（広範に使用される関数署名を変更 + 全リポジトリ適応、yansh の plan→code→fix パイプライン vs CC の "読み取り + 変更 + 検証" 循環）
- クローズアウト：3 回の AB のキー結論を統合し README に、以降の選択参照に便宜提供
