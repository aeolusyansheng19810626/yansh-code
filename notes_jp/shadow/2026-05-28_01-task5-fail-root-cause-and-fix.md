# task #5 失敗根因分析与修復方案

**日付**：2026-05-28
**ソース**：AB テスト v3 yansh task5 失敗（test fail、16 errors）

---

## 現象

```
TypeError: _err() takes 2 positional arguments but 3 were given
16 failed, 72 passed
[警告] tools.py 已用尽 23 轮工具调用上限（expected_edits=60）
[Coder task_complete] 检测到 '无需修改' 信号 → 跳过剩余 2 个文件
```

## 根因（二層疊加）

### 1. sys_prompt と edit_strategy_hint の矛盾（主因）

`agent.py:2097`（existing file sys_prompt）：
```
For existing files you **must** use replace_in_file for precise replacement;
do not rewrite the whole file with write_file
```

`agent.py:2120`（edit_strategy_hint、expected_edits>=15）：
```
如果各 edit 点 old_str 各不相同，**强烈推荐用 write_file 一次重写整个文件**
```

2 つのルールが衝突し、sys_prompt の方が system role 権威が高いため、LLM は sys_prompt に従って replace_in_file を使用し、user message 内の hint を無視します。結果：tools.py の 60 箇所全てが replace_in_file で逐点修正されました。

### 2. budget 公式が不足

`max(5, ceil(60/3)+3) = 23` ラウンド、各ラウンドで実際に完成可能なのは ~1-2 回の replace_in_file（read_file オーバーヘッド含む）、23 ラウンドで最大 20-25 箇所を修正可能です。tools.py 60 箇所の _err 呼び出しポイントのうち約 1/3 のみ修正され、署名の変更が完成していません：
- `_err` 関数定義自体が修正されていない可能性（あるいは修正されていても一部の呼び出しポイントが同期されていない）
- agent.py の呼び出しは既に新しい署名で 3 つのパラメータを渡している → TypeError

## 修復方案

### Fix 1：sys_prompt の矛盾を解除（キーポイント）

`expected_edits >= 20` の場合、existing file sys_prompt 内の「must use replace_in_file」ルールを write_file の使用を許可するように変更します：

```python
# agent.py:2097 附近、existing file sys_prompt
if expected_edits >= 20:
    write_rule = "- For large batch changes (this file has {N} edit points): prefer write_file to rewrite the whole file in one shot — faster and less error-prone than {N} replace_in_file calls."
else:
    write_rule = "- For existing files use replace_in_file for precise edits, never rewrite a whole file."
```

### Fix 2：compact 閾値 80K → 30K

`agent.py:2155`：`int(_cfg("compact_threshold_tokens") or 80_000)` → `30_000` に変更

AB テストの実証：yscode 30K 閾値が 2 回トリガーされ（各回 28K を節約）、yansh 80K は 23 ラウンドで一度もトリガーされていません。

## 変更不要な項目

- budget 公式：Fix 1 修正後 LLM が write_file を使用し、1 ラウンドで整ファイルを処理完了し、23 ラウンド budget は十分です
- edits_per_round：同上
