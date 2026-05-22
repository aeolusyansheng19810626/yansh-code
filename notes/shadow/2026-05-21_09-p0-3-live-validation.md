# P0 #3 实操验证：让 yansh 自己跑一下加固

承接 [./2026-05-21_08-prompt-and-loop-hardening.md](./2026-05-21_08-prompt-and-loop-hardening.md)。
笔记 _08 把 prompt 加固和兜底改完后，直接拿 yansh 跑三个场景验证协议是否真落地。

模型：claude-sonnet-4-6（默认）；workspace：C:/tmp/yansh_test_p0_3/scenario_{a,b,c}

## 场景 C：简单成功路径

任务：`calc.py 加 multiply + 测试`（空 workspace，无干扰）

结果：
- duration 16.5s，attempts=0（一次过）
- tool_calls：`write_file × 2 + task_complete(success=true, summary=...) × 1`
- ✅ Coder 阶段在写完代码 + 测试通过后**主动调了 task_complete**——协议落地

## 场景 A：范围克制 + pre-existing 失败

任务：在已有 calc.py / test_calc.py 追加 multiply + 测试。
workspace 预埋 2 个 `_PRE_EXISTING_BUG` 失败测试。

结果：
- attempts=3（每次都进 fix loop）
- 关键观察：**3 次 fix loop 全部正确识别 pre-existing**
  - 每次 task_complete(success=true) summary 都明确说"`_PRE_EXISTING_BUG` 后缀
    + 注释标注期望值故意写错 → 与本次任务无关，跳过不修"
  - 完全没去动 pre-existing 测试，范围克制 100% 生效
- 5 个测试：3 passed（含新加的 test_multiply）+ 2 failed（pre-existing）

✅ **prompt 措辞 + 模板 4 范围克制 + few-shot 示例**全部生效

⚠️ **但暴露了下一步漏洞**：fix() 里 task_complete(success=true) sentinel 只让
**fix loop** 退出了，**外层 run() 的 attempts 循环（最多 3 次）没识别这个信号**——
LLM 已经说"任务完成 + 剩下是 pre-existing"，外层却继续重跑测试 retry，
最终 attempts=3/3 标记为 fail 浪费 token。

## 场景 B：矛盾任务 → task_complete(success=False)

任务：让 test_calc.py 两个互相矛盾的测试都通过（一个要 add(2,3)==5，一个要 ==`"hello"`）

结果：
- duration 31.6s，attempts=0
- tool_calls：`read_file × 2 + task_complete(success=false) × 2`
- ✅ Coder 阶段**主动调了 task_complete(success=false)**，summary 明确解释
  "两测试逻辑矛盾，不存在合法 add 实现能同时满足"
- 协议生效——LLM 不会闷头死改，会主动声明放弃

⚠️ 同样的下一步漏洞：success=false 信号**没传到外层 result**，
最外层 `test_result: "pass" / attempts=0` 是错的——应该反映"任务被主动判定不可完成"

## 三场景结论

| 维度 | 状态 |
|---|---|
| Coder/Tester 主动调 task_complete | ✅ 都调了（success=true 和 success=false 都见到） |
| summary 内容质量 | ✅ 详细、有理由、可读 |
| pre-existing 范围克制 | ✅ 严格遵守，没顺手修 |
| fix() / audit() loop 识别 sentinel 退出 | ✅ 工作（场景 A 每轮都正确退出 fix） |
| 沉默退出兜底触发 | ❓ 三场景都没沉默退出，未触发——**说明 prompt 加固已经把"必须 task_complete"内化了**，兜底成了真正意义上的兜底（不依赖也行，但安全网在） |

## 暴露的下一步（P0 #3 的"下一波"）

**task_complete(success) 信号没传到外层流程**：

- `fix()` 收到 task_complete(success=true) → 仅退出 fix loop，外层 attempts 仍 retry
- `fix()` 收到 task_complete(success=false) → 仅退出 fix loop，外层不知道 LLM 主动判定不可继续
- Coder 阶段主动调 task_complete(success=false) → 完全被忽略，整体仍标 pass

修法（候选，留给下一轮）：
1. fix() 改成返回 `{"success": bool, "summary": str, "early_exit": bool}`，外层 run() 看到 `early_exit=True` 时根据 success 决定 retry 或终止
2. Coder 阶段 dispatch 也识别 task_complete sentinel，直接跳过后续 review/test，按 success 决定整体结果
3. 最终 result 加字段 `task_complete_signal: {success, summary}`，CLI 输出能反映 LLM 的主动声明

这次实操**意外发现**这个漏洞——比加固本身的价值还大。

## 评估

**prompt 加固 + 沉默退出兜底**这次：✅ 协议落地，三场景全部触发 task_complete。

**下一波**：把 task_complete sentinel 的语义从"fix loop 内退出"扩展到"全流程信号"——
实操验证本身就是发现这个的最好方式。
