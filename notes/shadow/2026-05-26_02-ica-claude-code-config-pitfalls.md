# ICA × Claude Code / 编码 Agent 配置陷阱

**日期**：2026-05-26
**起因**：本会话中 Claude Code 派 haiku subagent 反复撞 401，深挖到 ICA 平台的两条协议路径割裂、model ID 命名差异等系统性问题
**适用**：撞 401 / 404 / 模型不可用时按这清单查；接入 Cline / Continue / Aider / 任意编码 agent 时配置参考

---

## 总览：ICA 暴露两条协议路径，背后同一组模型

```
                       ICA Platform
                  └─ Bedrock claude-haiku-4-5  ✓
                       ↑                ↑
              ┌────────┴────┐    ┌──────┴──────────┐
              │ /ica/v1     │    │ /ica            │
              │ OpenAI 协议 │    │ Anthropic 协议  │
              │ 映射完整 ✓  │    │ 漏配 haiku ❌   │
              └─────────────┘    └─────────────────┘
                yansh/yscode      Claude Code
```

两条路径**协议不同 + model 映射表独立维护**，所以会出现"同一个模型 A 路径能用、B 路径不能用"。

---

## 1. 两个 endpoint，**协议不同**

| 用途 | Base URL | 协议格式 |
|---|---|---|
| 普通 ICA API（OpenAI 兼容 SDK 用 — yansh / yscode 走这条） | `https://api.nextgen-beta.ica.ibm.com/ica/v1` | OpenAI Chat Completions（`POST /v1/chat/completions`） |
| **Claude Code 专用** | `https://api.nextgen-beta.ica.ibm.com/ica` ← **不带 /v1** | Anthropic Messages API（`POST /v1/messages`） |

**注意**：
- yansh 代码内部自动补 `/v1`，yscode `.env` 直接写 `/v1`
- Claude Code 的 `ANTHROPIC_BASE_URL` **必须不带 /v1**
- 两条路径不能简单互换——协议不同，请求 body / response 字段都不一样

---

## 2. 两类 API Key（分开生成）

ICA Console → API Keys tab：

- **ICA API Key**：给普通 ICA API 用（yansh / yscode）
- **Coding Agent API Key**：专给 Claude Code / 编码 agent 用

各自只能存在一个，重新生成会让旧 key 失效。

> 实测：本次 AB test 时 yansh / yscode 都用同一份 ICA API Key（不是 Coding Agent Key）跑通了，所以普通 Key 已经够用。Coding Agent Key 跟 Claude Code 走 `/ica` 路径的关系待验证。

---

## 3. Model ID 命名不一致

| 来源 | haiku 4.5 ID |
|---|---|
| ICA Global Models（via `bedrock_converse`） | `claude-haiku-4-5` |
| Claude Code 内置默认请求 | `claude-haiku-4-5-20251001`（带日期后缀） |

ICA 用 AWS Bedrock 命名风格（不带日期）；Claude Code 用 Anthropic 直连风格（带日期）。

sonnet / opus 在两边命名一致（`claude-sonnet-4-6` / `claude-opus-4-7`），所以没问题；haiku 4.5 命名不一致 → 撞 ID。

---

## 4. Team Enabled toggle（在 admin 手里）

ICA Console → API Keys → Global Models tab，每行最右"Team Enabled" toggle：

- **Status: Active** = 平台层激活（ICA 全局可用）
- **Team Enabled: 开** = 你的 team 才能用

普通用户**不能切换**这个 toggle，要找 team admin。但本次 yansh probe 实测 haiku 通了，说明 team 实际是 enable 了 haiku 的——所以 toggle 状态可能是"已开但 UI 渲染灰色"。

---

## 5. ⭐ haiku 401 真因（深挖结论）

之前一度以为是 "team 不允许 haiku"。**不是**。证据：`scripts/probe_ica_models.py` 用 `/ica/v1` + `claude-haiku-4-5`（不带日期）实测，5 个模型全通：
- `claude-haiku-4-5` ✓
- `claude-sonnet-4-6` ✓
- `claude-opus-4-7` ✓
- `gemini-3-pro-preview` ✓
- `gpt-5.4-gus` ✓

**真实根因**：`/ica` 路径（Anthropic 协议代理）的 model 映射表**漏配了 haiku 4.5**。无论 Claude Code 发 `claude-haiku-4-5` 还是 `claude-haiku-4-5-20251001`，`/ica` 都找不到 → 401。

错误文案 "team can only access global-models, tried claude-haiku-4-5-20251001" 是**误导**——实际是 `model not found in /ica mapping`，但 ICA 的错误处理把它塞进了 "access denied" 桶里。

sonnet / opus 在 `/ica` 路径**碰巧也配了**（优先适配的常用模型），所以通；haiku 4.5 单单漏了。

**结论**：**这是 ICA 端的实现 bug / 漏配，不是 team 权限问题，也不是 Claude Code bug**。可反馈给 ICA 修。

---

## 6. ⭐ 通用配置：Cline / 任意 OpenAI 兼容编码 agent 走 ICA

适用：**Cline / Continue / Aider / 任何支持 "OpenAI Compatible" provider 的编码工具**

走 `/ica/v1`（OpenAI 兼容路径）是普适方案，跟 yansh / yscode 一条路：

| 字段 | 值 |
|---|---|
| **API Provider** | `OpenAI Compatible` |
| **Base URL** | `https://api.nextgen-beta.ica.ibm.com/ica/v1`（**带 /v1**） |
| **API Key** | 普通 ICA API Key（不是 Coding Agent Key） |
| **Model ID** | `claude-sonnet-4-6` / `claude-haiku-4-5` / `claude-opus-4-7` / `gpt-5.4-gus` / `gemini-3-pro-preview`（任选，**不带日期后缀**） |

**Cline 具体操作**：VS Code 装 Cline → ⚙️ Settings → API Provider 选 `OpenAI Compatible` → 填上面字段 → 点 **Verify** 测连接。

**优势**：
- 5 个模型都可选（含 haiku 和跨 family 的 gpt5 / gemini）
- 跟 yansh/yscode 已验证路径一致，确保稳定
- 切模型只改 Model ID 字段

**劣势 / 注意**：
- Prompt cache 不可用（ICA 反正不透传 — 已 `scripts/probe_ica_cache.py` 验证）
- Anthropic 协议特有功能（extended thinking reasoning 字段格式）丢失 — 编码场景影响小
- 错误信息可能没 Anthropic 直连那么详细

---

## 7. Claude Code 能不能也切 /ica/v1？

**直接不能**：

| | Claude Code 发的请求 | /ica/v1 期望 |
|---|---|---|
| 协议 | Anthropic Messages API（`POST /v1/messages`） | OpenAI Chat Completions（`POST /v1/chat/completions`） |
| Body schema | `{messages, max_tokens, ...}` 含 `content: [{type, text}]` | `{messages, ...}` content 是 plain string |
| Response | `content: [{type: "text", text}]` | `choices: [{message: {content}}]` |

请求路径都不一样 → 直接换 base URL 会 404。

**加 proxy 可以**：起本地代理对外 Anthropic、对内调 OpenAI，顺便做 model 名映射：

```
Claude Code  ──Anthropic 协议──>  本地 proxy  ──OpenAI 协议──>  ICA /ica/v1
              (任意 model alias)              (映射成 ICA 命名)
```

设 `ANTHROPIC_BASE_URL=http://localhost:<port>`。现成方案：
- **LiteLLM Proxy**（最成熟，OpenAI ↔ Anthropic 双向）
- **claude-code-proxy / anthropic-proxy**（专为 Claude Code 轻量代理）

**Trade-off**：
- ✅ 解锁 haiku + 跨 family 模型
- ❌ 多一个组件维护 + 调试链变长 + 错误排查更难
- ⚠️ Anthropic 独有功能（prompt cache / extended thinking）通过 proxy 可能丢，但 ICA 反正不透传

**当前判断**：sonnet 4.6 默认就够稳（本次 AB test 15 次 dispatch 验证），暂时不折腾 proxy。

---

## 8. 实操：撞 401 / haiku 不可用怎么办

按门槛由低到高：

| 方案 | 操作 | 备注 |
|---|---|---|
| **C** | 派 subagent 时显式指定 `model=sonnet`，避开默认 haiku | **本会话已在用**；AB test 15 次 dispatch 全 sonnet 没 401 |
| D | 反馈 ICA 修 `/ica` 路径漏配 haiku 4.5 | 根因解决，但要走流程 |
| A | 联系 team admin 确认 Team Enabled toggle 状态（可能本来就开） | 大概率不是真因 |
| B | 改 Claude Code 默认 subagent model | **没有这个设置**（已查文档），每个 subagent type 的 model 在 .md 定义里，内置 agent 改不了 |
| E | 上 LiteLLM proxy 把 Claude Code 切到 /ica/v1 | 终极方案，多一个组件成本 |

## 9. 内置 subagent 默认 haiku 的有

- `claude-code-guide`（撞过 401 两次）
- 其他 fast model 类型（待确认）

## 10. 本会话验证的最稳姿势

```
Agent({
  subagent_type: "general-purpose",
  model: "sonnet",   // 必加，不依赖 default
  prompt: "..."
})
```

`general-purpose` + 显式 `model=sonnet` —— 本次 AB test 5 个 task × 3 方 = 15 次 dispatch 全过。

---

## 数据来源

- IBM ICA Console 截图（2026-05-26）：API Keys tab + Global Models tab
- 401 错误信息：`team can only access global-models, tried claude-haiku-4-5-20251001`
- AB test 15 次 dispatch 实测（task #1-#5 × 3 方）：`./ab/2026-05-25_06-summary-3way.md`
- probe_ica_models.py 5 个模型实测全通（用 /ica/v1 + 不带日期 ID）
- ICA prompt cache 探测：`scripts/probe_ica_cache.py`（未透传）
- Cline 配置文档：https://docs.cline.bot/provider-config/openai-compatible
- Claude Code subagent 文档：https://code.claude.com/docs/en/sub-agents
