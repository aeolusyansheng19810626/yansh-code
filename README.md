# yansh-code

基于 LLM（DeepSeek）的自动化代码生成与测试 CLI 工具。

通过 ReAct 循环自动完成：需求分析 → 生成代码 → 执行测试 → 错误修复。

## ✨ 功能特性

- 🤖 **智能规划**：LLM 自动拆解需求，生成分步实施计划。
- 📝 **增量修改**：优先使用 `replace_in_file` 进行局部代码替换，拒绝无脑重写整文件。
- 🧪 **闭环测试**：集成 `pytest` 自动运行单元测试。
- 🔄 **自动修复**：测试失败时，LLM 根据错误日志自动定位并修复漏洞。
- 💬 **对话记忆**：维护会话历史，闲聊时 LLM 能回答上下文相关问题（最多保留20轮）。
- 🗜️ **自动压缩**：历史超过 6000 字符时自动压缩旧轮，保留最近 3 轮原文。
- ⌨️ **交互命令**：支持 `/mode`、`/compress`、`/context`、`/clear` 等内置命令。
- ⏹️ **ESC 中断**：任务执行中按 ESC 可立即中断，100ms 内响应。
- 📦 **安全沙箱**：所有操作均在独立的 `workspace` 目录下执行。


## 快速开始

```bash
# 1. 进入虚拟环境
.\venv\Scripts\activate

# 2. 安装依赖
pip install -r requirements.txt

# 3. 配置密钥（通过 OpenRouter 调用 DeepSeek）
copy .env.example .env
# 编辑 .env 填入你的 OPENROUTER_API_KEY

# 4. 运行
python main.py
```

## 工作流程

输入需求后，程序自动执行 3 个阶段：

```
📋 阶段1：制定计划  →  LLM 分析需求，生成文件列表和测试命令
✍️ 阶段2：生成代码  →  多轮工具调用，写入或增量修改代码文件
🧪 阶段3：测试与修复 →  运行单元测试 → 失败则自动修复（最多3次）
```

任务完成后进入对话循环，支持：
- 继续提出新任务
- 闲聊（LLM 能记住最近5轮对话）

## 内置命令

在主循环中可直接输入以下命令：

| 命令 | 说明 |
|------|------|
| `/mode plan` | 仅输出计划，不生成代码 |
| `/mode code` | 跳过确认，直接生成代码 |
| `/mode auto` | 默认交互模式（默认） |
| `/compress` | 手动压缩上下文历史 |
| `/context` | 查看当前上下文大小（轮数 / 字符数） |
| `/clear` | 清空全部对话历史 |
| `exit` / `quit` | 退出程序 |

任务执行中按 **ESC** 可立即中断。

## 配置

通过 `config.py` 可调整：

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `DEEPSEEK_MODEL` | 模型名称 | `deepseek/deepseek-v4-flash` |
| `MAX_ATTEMPTS` | 最大测试修复次数 | 3 |
| `WORKSPACE_DIR` | 生成代码存放目录 | `workspace` |
| `QUALITY_CASCADE` | 降级模型列表 | 5 层（当前统一使用 DeepSeek） |

## 项目结构

```
yansh-code/
├── main.py           # CLI 交互入口（主循环、命令分发）
├── agent.py          # Agent 核心逻辑（ReAct 循环、质量级联、上下文压缩）
├── tools.py          # 增强工具集（读/写/删/查/精确替换/执行）
├── config.py         # 模型级联与环境配置
├── interrupt.py      # ESC 中断检测（跨平台后台线程）
├── tests/            # 单元测试
├── requirements.txt  # Python 依赖
├── .env              # API 密钥
└── workspace/        # 生成代码的输出目录
```

## 依赖

- `openai` — OpenRouter API 调用
- `python-dotenv` — 环境变量管理
- `rich` — 终端彩色输出