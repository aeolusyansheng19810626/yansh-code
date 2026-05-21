# 2026-05-21 模板 4 + _TESTER_ROLE 加固：识别 pre-existing 失败

## 背景

上一轮 4 模板验证（[_05 笔记](./2026-05-21_05-four-templates-validation.md)）发现：
模板 4（范围克制）在 task A 里失败——yansh 看到 5 个 pre-existing 失败用例，
**误以为是自己引入的**，于是去改 `_DANGEROUS_PATTERNS` 和 `_validate_path` 错误文案
"修复"它们，污染了产品代码。

5 个 pre-existing 失败的根因都跟 max_depth 任务无关：
- `test_execute_command_timeout`：assert "超时"，但 `python -c` 已被列入 `_DANGEROUS_PATTERNS`，文案变成"安全拦截"
- 3 个 path-traversal 测试：assert "超出"/"exceeds workspace"，但 `_validate_path` 文案改成"路径越界"
- `test_build_diff_lines_exactly_50_no_truncation`：截断阈值定义可能调整过

## prompt 改动

两处加固：

**`_CODER_ROLE` 模板 4** 末尾加：
```
- 失败用例不一定是你引入的：跑测试看到红，先核对失败 assert 引用的函数/常量
  是不是本次 plan 列出文件里的符号——不沾边的（如本次改 list_files 但
  test_execute_command_timeout 失败）大概率是 pre-existing 失败，
  记录在报告里但不要碰产品代码"修复"它
```

**`_TESTER_ROLE`** 排查顺序最前面插一条「先识别归属」——
fix 流程用的就是 `_TESTER_ROLE`，这里是最关键的位置：
```
1. 先识别归属：失败 assert 引用的符号是否在本次 plan 列出的文件里？
   - 是 → 本次任务引入的失败，继续走流程
   - 否 → 大概率 pre-existing 失败，跳过不修，在最终报告里列出来让用户判断
```

## 设计要点

跟之前 [_04 yansh 反超 Claude Code 笔记](./2026-05-21_04-yansh-beats-claude-code.md) 同型：
**用具体形状打具体问题**——
不写"区分 pre-existing 失败"这种抽象原则，写"失败 assert 引用的符号是不是
本次 plan 列出文件里的符号"这种**机械可判定**的规则。

LLM 拿到这条规则后能 mechanically 执行：
1. 看失败 assert 引用了哪个 函数/常量名
2. 那个名字在不在 plan 的文件里
3. 不在 → 跳过

不需要 LLM"判断"它是不是 pre-existing；它只需要做字符串匹配。

## 验证

复跑 task A（list_files 加 max_depth）。

| 维度 | 这次（带新 prompt）| 上次（dbc25e2） |
|---|---|---|
| 触碰 `_DANGEROUS_PATTERNS` | ❌ 没碰 ✅ | ✅ 删了 `python -c` |
| 触碰 `_validate_path` 错误文案 | ❌ 没碰 ✅ | ✅ 改了文案 |
| 触碰 `_build_diff_lines` 截断逻辑 | ❌ 没碰 ✅ | — |
| max_depth 实现 | ✅ 先剪枝后枚举 | ✅ |
| 4 个 max_depth 测试 | ✅ 全过 | ✅ |
| pre-existing 5 failed | 5 个保持 failed（不修）| 仍然 fail |

**5 个 pre-existing 失败保持失败状态，但产品代码完全没动**——这是质变。

## 唯一瑕疵

fix loop 第 6 轮触发上限前，yansh 用 `replace_in_file` 把
`test_build_diff_lines_modify` 里的循环变量 `l` 改成 `line`（cosmetic 美化）。
违反"不改既有变量名"，但：
- 改的是 pre-existing 测试自身（不是它该修的产品代码）
- 不破坏任何东西（`l` 和 `line` 等价）
- 不影响 5 个 pre-existing 失败的状态

属于"想做点什么但找不到合理目标"的 fallback 行为。可以接受。

## 残留：dispatch 未改

这次 architect plan 阶段没把 `agent.py` 列进 plan，所以 dispatch
（`agent.py:866` 的 `list_files()`）没改成 `list_files(**args)`。

不过这是模板 1 / `_ARCHITECT_ROLE` 的问题，**不是这次加固的目标**。
而且 max_depth=None 默认值让现存 LLM 调用都还能正常工作（只是没法用 max_depth）。

下一轮的事。

## 一句话总结

**用一条机械可判定的反向警告（"失败 assert 引用的符号在不在 plan 文件里"），
把 yansh 在 fix loop 里碰 pre-existing 产品代码的次数从 N 降到 0**——
这是模板 4 第一次在 fix 阶段真正生效。

## commit

`34f22ce feat: 模板 4 + _TESTER_ROLE 加固——区分 pre-existing 失败 vs 本次任务引入的失败`
