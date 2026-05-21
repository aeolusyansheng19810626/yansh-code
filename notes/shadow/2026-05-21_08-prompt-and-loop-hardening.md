# P0 #3 prompt 加固 + loop 沉默退出兜底

承接 [./2026-05-21_07-p0-3-recovery-loop.md](./2026-05-21_07-p0-3-recovery-loop.md) 提到的待加固项。

## 这一轮做的

### 1. _TESTER_ROLE / _AUDITOR_ROLE prompt 措辞调整

**问题**：旧 prompt 把 "task_complete 显式收尾" 放在末尾，且写 "沉默退出 = 默认成功"——
LLM 实际行为里很容易直接沉默退出，错过显式协议带来的可观测性（success / summary）。

**做法**：
- 把【收尾要求 - 必读】块移到 prompt 顶部，加粗"必须"
- 加 2 个 few-shot 示例（成功 + 放弃）
- 删掉 "沉默退出 = 默认成功" 这种反向暗示
- tools_schema.py 里 task_complete 的 description 同步改

### 2. fix() / audit() 沉默退出兜底

**设计**：LLM 这一轮没调任何工具时——

- 旧行为：直接 return（视为完成）
- 新行为：如果 `silent_prompted == False`，注入一条 system 消息追问一次（"按协议必须 task_complete..."），`silent_prompted = True`，continue 循环
- 第二次再沉默 → 真退出（避免无限追问 spam）

只兜底一次，最坏情况多消耗一轮——比直接静默退出丢失收尾信号划算。

### 3. _CODER_ROLE 模板 4 范围克制：linter 归属

**问题**：模板 4 已经讲 "失败用例不一定是你引入的"，但只针对 pytest 失败。
ruff/flake8/pyright 报的 unused import (F401)、unused variable 等，LLM 会顺手清理——
但这些常常**不在本次 plan 文件范围内**，属于 pre-existing 噪音。

**做法**：模板 4 加一句：linter 报错若不在本次 plan 文件，按 pre-existing 处理，记录但不动。

## 验证

`python tests/run_unit.py` → 9/9 文件通过，无新增失败。

## 影响文件

- agent.py：_TESTER_ROLE / _AUDITOR_ROLE / _CODER_ROLE 模板 4 / fix() / audit()
- tools_schema.py：task_complete description

## 评估

prompt 措辞 + loop 兜底是互补的——单靠 prompt 改 LLM 还是会偶尔沉默；单靠兜底没显式协议
LLM 不会主动 task_complete。两个一起做才闭环。

linter 归属规则把范围克制原则从 "测试维度" 扩到 "lint 维度"，避免任务漂移到清理积技术债。
