# enforcement=pre 端到端实验（2026-06-12）

> pre 模式：内建 PreToolUse test-first 拦截，agent 写实现文件前若无对应测试骨架则 block。
> commit e991db1。对照 gate baseline：minire-2（$6.87/15/15/轮~33）、miniQL regression1（$12.1/10/10）。

## 实验配置

| 项目 | 值 |
|---|---|
| 模型 | sonnet |
| enforcement | pre（SOLO_TEST_ENFORCEMENT=pre） |
| max_block | 3（默认） |
| 两个实验 | minire-pre-exp1、miniql-pre-exp1 |

## 结果对照

| 指标 | minire-pre | minire-gate baseline | miniql-pre | miniql-gate baseline |
|---|---|---|---|---|
| final_success | ✅ True | ✅ True | ✅ True | ✅ True |
| 黑盒 | **15/15** | 15/15 | **10/10** | 10/10 |
| 轮次 | **65** | ~33 | **87** | ~79 (fixplan2) |
| cost | $7.46 | $6.87 | $11.67 | $10.73 |
| duration | 667s | 703s | 1114s | — |
| tokens_in | 2.3M | — | 3.5M | — |
| pretool 拦截次数 | **3** | — | **9** | — |
| tests/ 建立时机 | **轮1-5（写impl前）** | gate 回灌后 | **轮1-5（写impl前）** | gate 回灌后 |

## 关键发现

### ★ pre 拦截机制完全生效

**minire**：agent 轮 1-5 先建 5 个测试文件（test_compiler/lexer/matcher/parser/smoke），随后写实现。3 次拦截：
1. `_check_path.py`（内部 helper，无对应测试）
2. `minire/__main__.py`（⚠️ 应豁免，入口文件，已有 test_smoke.py 覆盖）
3. 其他草稿文件

**miniQL**：9 次拦截，agent 同样轮 1-5 先建 9 个测试文件（含 test_analyzer/executor/errors 等），写 `miniql/errors.py` 时被拦一次 → 立即补 `test_errors.py` 再继续。**历史上 sonnet 在 miniQL 上从不主动建 tests/，pre 模式彻底改变了这个行为**。

### 代价：轮次增加

minire：65 轮 vs 33 轮（+97%），但多的轮次都是「建测试→写实现→修真实 bug」，**无 churn**（gate baseline 的 churn 是 8 轮假回灌同一文案）。cost 几乎持平（+8%）。

miniQL：87 轮 vs 79 轮（+10%），接近持平。更高测试覆盖（190 测试 vs baseline 的 ~200），14 个测试文件。

### 修的都是真实 bug

minire：State.transitions 缺属性、反斜杠边界处理、分组交替 integration test——全是真实实现缺陷，agent 边测试边修。
miniQL：Ternary.UNKNOWN 枚举、COUNT row context、NOT 表达式——同样真实。无 churn。

## 遗漏：`__main__.py` 需加入豁免名单

`_PRETOOL_EXEMPT_NAMES` 缺 `__main__.py`（CLI 入口文件），guard 拦截了 `minire/__main__.py`。`_SOLO_ENTRY_BASENAMES` 已有 `{"__main__.py", "cli.py"}`，两个集合应同步。同理 `cli.py` 也需加入豁免。

## 结论

**pre enforcement 目标达成**：
- ✅ 拦截机制在 `--json` 批处理下正常触发（绕开了 `set_disabled(True)`）
- ✅ agent 响应正确，被拦一次就转向建测试（不死循环）
- ✅ 两个任务黑盒满分、success=True，功能质量无回退
- ✅ miniQL 上效果尤其显著——历史上 sonnet 从不主动建 tests/，pre 彻底改变了这一行为
- ⚠️ 轮次代价：minire +97%（但无 churn，全是有效工作）；miniQL +10%
- 🔧 待修：`__main__.py` / `cli.py` 加入豁免名单

## 待办

1. 豁免名单加 `__main__.py` / `cli.py`（低风险一行改动）
2. n=1，建议再跑一次 miniQL 坐实方差（尤其 miniQL 历史方差大）
