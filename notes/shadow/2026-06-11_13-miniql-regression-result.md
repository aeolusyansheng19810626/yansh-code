# miniQL 回归验证结果（gate churn fixplan 后）

> 承接 ./2026-06-11_12（gate churn Fix A-E 落地 + minire-2 验证）。
> 目的：确认 Fix A-E 未破坏 miniQL 高耦合路径。

## 结果

| 指标 | fixplan 基线（f2-f4）| regression1（修复后）|
|---|---|---|
| success | True | **True** ✅ |
| 黑盒 | 10/10 | **10/10** ✅ |
| cost_usd | $10.7-19.1 | $12.1 |
| duration | ~800-1466s | 1148s |
| tool_calls | — | 90 |
| 自测通过数 | 143 | 217 |

## 关键观察

- gate 正常回灌了 1 次真实失败（`1 failed, 22 passed` → agent 修复 → `23 passed`），Fix B/C 的收敛/止损逻辑未误伤正常红回灌路径。
- smoke 强制前移触发 1 次（agent 漏补 smoke），Fix A 不影响 smoke 判定。
- 217 passed 比基线 143 多（agent 这次多建了 executor 单元测试），功能覆盖更广。

## 结论

**无回归。Fix A-E 对 miniQL 高耦合路径透明。** gate churn fixplan 全量验证完毕，可 commit+push。
