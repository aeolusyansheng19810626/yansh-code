# 2026-05-21 shadow: 列出 yansh-code 的 LLM 工具

## 任务

> "yansh-code 项目目前有多少个 LLM 工具？列出名字。"

两边都用 **Claude Sonnet 4.6**，控制变量。

---

## Claude Code 的工具序列

| 步 | 工具 | 参数 | 说明 |
|---|---|---|---|
| 1（并行） | Read | `tools_schema.py:1-5` | 5 行确认是 TOOLS 列表 |
| 1（并行） | Grep | `pattern="\"name\":\\s*\""` | 一次抓全部 name 字段 |

**耗时 ~5s · 答案 19（准确）**

特点：
- 同轮发起两个无依赖工具
- 不读整文件，直接 grep 定点
- 只回答字面问题，不发挥

---

## yansh 的工具序列（改 prompt 前）

| 步 | 工具 | 参数 |
|---|---|---|
| 0（系统预注入） | workspace_symbols | 35 文件全量符号摘要 |
| 1 | read_file | `tools_schema.py`（整文件 988 行） |
| 2 | list_symbols | `tools.py` |

**耗时 27.8s · 答案 20（多 1，含 delete_file）**

特点：
- 整文件 read，没用 grep
- 多调一次 list_symbols 做交叉验证
- 输出套审计报告模板（总览/重要发现/总评/bonus）
- ✨ 但**意外发现真 bug**：delete_file 在 tools.py 有实现却未注册 schema

---

## yansh 的工具序列（改 prompt 后）

`_AUDITOR_ROLE` 加了"任务尺度感知"和"先定位再精读"两条原则。

| 步 | 工具 | 参数 |
|---|---|---|
| 0（系统预注入） | workspace_symbols | 同上 |
| 1 | read_file | `tools_schema.py`（整文件） |

**耗时 9.3s · 答案 19（准确）**

改进：
- ✅ 任务尺度感知生效——不再套审计报告模板，直接给清单
- ✅ 不再多余调 list_symbols
- ⚠️ 但还是**整文件 read**，没养成 grep 习惯

---

## 学到什么

### 1. Prompt 改动效果可量化

| 指标 | 改前 | 改后 | 变化 |
|---|---|---|---|
| 耗时 | 27.8s | 9.3s | **-66%** |
| 工具调用 | 2 | 1 | **-50%** |
| 输出长度 | 满模板 | 直接清单 | 显著简化 |
| 答案准确度 | 错（20） | 对（19） | 关键 |

**单条 prompt 规则的 ROI 极高**——加 3 行字让任务快 3 倍且答案变对。这印证了 ROADMAP P0 #2 的判断：调一周 prompt 的效果超过加 5 个工具。

### 2. "任务尺度感知"是个被低估的规则

LLM 默认**模式匹配最复杂的输出格式**——它看到自己角色叫"审计 Agent"就套审计模板，哪怕用户只问"几个"。

教它"简单问题给简单答"比想象中重要。**这是市面上 audit 工具普遍冗长的根本原因**。

### 3. "先 grep 再精读"这条没生效

prompt 里写了"用 search_in_files 锁定具体行号，不要整文件 read_file"——但 yansh 还是 read 了整个 tools_schema.py。

**为什么没生效**：因为 LLM 已经知道答案就在这一个文件里，直接 read 反而更"省事"。规则措辞需要再强：要么改成"超过 200 行的文件禁止整文件 read"，要么加 few-shot 例子。

→ ROADMAP P0 #2 后续：加 few-shot example 比加规则文字效果好。

### 4. yansh 的"过度发挥"是双刃剑

改前 yansh 多发现了 delete_file 漏注册——这是真 bug。改后 yansh 不发挥了，但也没发现这个 bug。

**深刻问题**：什么时候应该发挥、什么时候应该克制？  
现在的解：**任务措辞决定**。"列出 X" → 严格回答；"审一下 X" → 允许发挥。  
但用户的措辞经常含糊，需要 prompt 教会 LLM 在含糊时**先确认意图**。

→ 这又对应 ROADMAP P0 #2 的另一条："模糊任务先 1 句确认"。

### 5. workspace_symbols 预注入的副作用

audit mode 一进来就把 35 文件的全部符号塞 system prompt。**这让 LLM 倾向于"在已知信息里翻"，而不是用更精准的工具**——因为它"已经看过摘要"了。

→ 对应 ROADMAP P0 #1：分层符号索引比扁平摘要更好。

---

## 后续追加项（按本次发现往 ROADMAP 里塞）

- [ ] _AUDITOR_ROLE 加 few-shot example（"问几个 X" vs "审一下 Y"）
- [ ] 整文件 read 加硬阈值：>200 行强制要求 offset+limit
- [ ] workspace_symbols 默认输出改为顶层目录摘要（对应 P0 #1）
- [ ] _CODER_ROLE 也加 few-shot（这次没测到，不确定改动效果）

---

## Bonus 收益：替我们抓到了真 bug

- `delete_file` 实现存在但 schema 漏注册（`tools.py:258`）
- 已在本轮一并修复：tools_schema.py + agent.py 都补上
- LLM 替你审计自己的项目，能发现你没注意到的盲点——这是 audit mode 最有意思的应用场景
