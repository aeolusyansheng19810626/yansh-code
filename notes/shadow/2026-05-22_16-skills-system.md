# P2 #8 Skills 系统

承接 [_15](./2026-05-22_15-plan-mode-c.md)：P2 #7 Plan Mode 完成后做 #8。
按 ROADMAP "最小版"目标——目录约定 + frontmatter triggers + 关键词匹配注入 system prompt。

## 改了什么

### 1) skills.py 新文件

数据结构：
```python
@dataclass
class Skill:
    name: str
    description: str
    triggers: list      # 关键字（不区分大小写）
    modes: list         # 适用 mode；空=全部
    body: str           # markdown 正文
    source_path: str
```

API：
- `parse_skill_file(path)` 解析单个 .md（手写最简 frontmatter parser，不引 PyYAML）
- `discover_skills(workspace_dir)` 扫描项目级 + 全局
- `match_skills(input, skills, mode)` 关键字匹配
- `format_skills_prompt(matched)` 拼成 system prompt 片段
- `load_and_format(input, ws, mode)` 一站式入口

目录约定：
- `<workspace>/skills/*.md` 项目级（优先）
- `~/.yansh/skills/*.md` 全局（备选）

同名时项目级覆盖全局。

### 2) frontmatter 格式

```yaml
---
name: code-review
description: 代码审查工作流
triggers: ["审查", "code review", "review"]
modes: ["audit", "plan"]      # 可选；空=全 mode
---
（markdown 正文，作为 prompt 片段）
```

手写解析器只支持子集：标量 / `[list]` / `# 注释` / 单双引号字符串。够用，不依赖 PyYAML。

### 3) agent.py 注入

模块级 `_ACTIVE_SKILLS_PROMPT: str = ""` 持有当前激活的 skill 片段。

`_run()` 入口：
```python
prompt_frag, matched = skills.load_and_format(requirement, _get_workspace(), mode=mode)
_ACTIVE_SKILLS_PROMPT = prompt_frag
if matched: console.print(f"[skills] 命中 {len(matched)} 个：...")
```

注入点：
- `plan()` system prompt 末尾 += `_ACTIVE_SKILLS_PROMPT`
- `code()` 两条路径（已有文件 / 新建文件）末尾 += 同上
- `audit()` system prompt 末尾 += 同上
- `fix()` 两条路径（review_rejection / test_failure）末尾 += 同上
- `plan_chat()` 独立扫一次（mode='plan'），写自己的 system prompt（不复用 `_ACTIVE_SKILLS_PROMPT`，因为 plan_chat 不走 run）

### 4) main.py 命令

```
/skill list              列出全部 skill（项目级+全局）
/skill show <name>       显示某 skill 完整内容
```

加进 `_SLASH_COMMANDS` 自动补全列表。

## 验证

### 单测（tests/unit/test_skills.py，新增 20 条）

frontmatter：
- 标量 / list / 缺失 / 注释行 / 引号 / 大小写规整 / 缺 frontmatter
- modes 字段：列表 / 空 / `applies_to_mode` 行为

发现：
- 项目级扫描
- 没目录返回空
- **项目级覆盖全局级（同名优先）**——用 `monkeypatch.setattr(Path, "home", ...)` 验证
- 坏文件不崩主流程

匹配：
- 关键字（大小写无关）
- mode 过滤（modes=[audit] 时 code mode 不命中）
- 空输入 / None 安全

格式化：
- 空列表返回空字符串
- 单/多 skill 格式
- 端到端 load_and_format

agent 集成：
- `run()` 入口扫到 skill → `_ACTIVE_SKILLS_PROMPT` 含 skill body
- 不命中时 `_ACTIVE_SKILLS_PROMPT` 被清空（不留残留）

12/12 文件全过；新文件 20/20。

### 集成验证（ICA Sonnet 4.6）

workspace 准备：
- `calc.py`：极简 add / divide（divide 未处理除零）
- `skills/code-review.md`：审查清单 + **强制 markdown 表格输出格式** + 严重/中/低三档分级

跑 `python main.py "审查 calc.py" --mode audit`：

输出含 `[skills] 命中 1 个：code-review`，LLM 报告**严格按 skill 规定的格式**：

| 文件 | 行号 | 类型 | 描述 | 建议 |
|------|------|------|------|------|
| calc.py | 5–6 | **严重** | divide 未处理 b=0 | ... |
| calc.py | 1、5 | **中** | 两个函数均无 docstring | ... |
| calc.py | 1、5 | **低** | 缺少类型注解 | ... |
| — | — | **低** | 无任何测试文件 | ... |

**关键收益证据**：
- 表格列名（"文件 / 行号 / 类型 / 描述 / 建议"）跟 skill 一字不差
- 类型分级用 "严重 / 中 / 低"——**完全对应 skill 里规定的三档**，没用 LLM 默认的 "high / medium / low"
- 每条都给了具体行号——skill 里"必须给出具体行号"被遵守

匹配负面验证（不命中）：
- `mode=audit + "审查"` → 命中
- `mode=code + "审查"` → mode 过滤掉
- `mode=audit + "优化"` → 关键字不命中

## 评估

### 跟 .agent_rules 的本质区别

`.agent_rules` 是项目级**常量规则**，每次任务都注入；Skills 是**按需加载**——只在用户输入命中 trigger 时才注入。两者并存：rules 管"我这个项目要遵守什么"；skills 管"做这类任务时要遵循什么工作流"。

### 跟 Claude Code 的差距

Claude Code 的 Skills：
- **LLM 智能匹配**：不止关键字，还看上下文/历史
- **Skill 安全沙箱**：第三方 skill 能不能改 agent 行为有边界控制
- **Skill 间依赖**：skill A 触发后能加载 skill B

yansh 当前是"prompt 注入"最小版本：
- 只匹配关键字（不智能）
- 没安全沙箱（skill 内容能让 LLM 干任何事，包括恶意 prompt 注入）
- 不支持依赖

够用作"项目内私有工作流模板"——这是最常见的真实诉求。第三方分发场景再做也来得及。

### "Prompt as a Service"的工程意义

代码审查这种工作流以前要：
1. 用户每次手写 "审查 X，按 严重/中/低 分级，必给行号"
2. 输出格式每次都不一样

现在：
1. 一次写好 skill
2. 任何包含 "审查" 的输入自动加载
3. 输出格式高度一致——可写脚本机器解析

这是把"经验"沉淀进可复用单元的最小工程化。

下一波（不在这次范围）：
- LLM 智能匹配：用一次轻量 LLM call 判断 "这个输入需要哪些 skill"
- skill 优先级 / 互斥
- skill 触发 token 统计：知道 skill 注入加了多少 token
- skill 对 tool list 的影响：某些 skill 可能想限制工具白名单（比如 "code-review skill 只用 readonly tools"）
- 内置 skill 库：随项目自带几个常用模板（code-review / refactor / debug / api-design）

## 关键文件

| 文件 | 改动 |
|---|---|
| `skills.py` | 新文件：Skill dataclass + parse / discover / match / format / load_and_format |
| `agent.py` | 模块级 `_ACTIVE_SKILLS_PROMPT`；run() 入口加载；plan/code/audit/fix/plan_chat 拼接 |
| `main.py` | `/skill list` `/skill show <name>` |
| `tests/unit/test_skills.py` | 新文件 20 条单测 |
| `tests/run_unit.py` | 加进文件清单 |
