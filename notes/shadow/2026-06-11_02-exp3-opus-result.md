# 实验3 结果：opus vs sonnet 行为遵从梯度（2026-06-11）

> opus（claude-opus-4-8）+ solo + **off 模式**（纯自然行为，无 enforcement）。
> 干净 ws longrun-miniql-exp3-opus，单次。对照 sonnet off baseline（R19-21 / gatev2）。

## 对比表（同任务、同 off 模式、同 solo 框架）

| 维度 | sonnet (off) | **opus (off)** |
|---|---|---|
| 轮次 | 烧满 120，**从不 task_complete** | **51 轮干净 task_complete** |
| tests/ | 空/不建，只写根目录 _test_*.py 草稿 | **全套 5 文件（lexer/parser/analyzer/executor + test_smoke）** |
| 端到端 smoke | 无 | **有，subprocess 调真实入口，58 测试全过** |
| gate 裁定 | no_smoke / no_command | **passed** |
| final_success | **false** | **true** |
| 黑盒 | 9~10/10（功能其实基本对） | **10/10** |
| 耗时 | 758s | 613s（更快） |
| input token | 3.75M | **2.2M（更少）** |
| 成本 | $11.98 | $36.60 |

## 核心洞察

1. **opus 自觉做了 sonnet 靠硬卡都逼不出来的事**：动手前规划、写实现后用真实入口跑各种 SQL 验证、
   主动建全套 tests/ 含端到端 smoke、删掉 _scratch.py 清理、显式 task_complete 干净收尾。
   全程 off 模式，没有任何 enforcement。
2. **opus 更高效，不是更慢**：用更少 input token（2.2M vs 3.75M）、更短时间（613 vs 758s）完成。
   $36.6 vs $12 的差价**纯粹是 opus 单价 5×**，不是低效——恰恰相反，它不瞎转烧轮次。
3. **印证并补全实验1 的结论**：
   - enforcement（role/gate）机制对 opus **多余**（它自觉建测试）、对 sonnet **无效**
     （它烧满轮次不收尾，gate 硬卡够不到——见 notes/shadow/2026-06-11_01）。
   - 模型的「行为遵从」梯度真实且巨大：解题正确性之外，**主动建测试 / 不漂移 / 干净收尾**
     这些「工程素养」行为，opus ≫ sonnet。
4. **对 Anthropic「hook 才确定」观点的补充**：hook/enforcement 是为了让**能力不足的模型**可靠；
   能力足够的模型靠提示词（role）就自然遵从。所以「上 hook 还是换模型」是成本权衡：
   - opus：$36.6 一把过、success=true、10/10、零工程成本。
   - sonnet：$12 但 success=false、需额外工程化 hook（且 hook 还得设在不依赖收尾的时机）。
   对 miniQL 这种强耦合任务，**换 opus 比给 sonnet 加 hook 更省心**（单次成本高但确定性高、无返工）。

## 方向启示

- yansh solo 框架本身没问题——opus 跑出干净 success=true 证明框架能端到端工作。
  之前 R1-R21 的「假阴性 / 烧满轮次」主要是 **sonnet 行为短板**撞上框架，不是框架 bug。
- 若目标是「可靠产出」：强耦合任务直接上 opus。若目标是「让弱模型也可靠」（更有研究价值）：
  回到实验1 终局——把 smoke/tests 检查做成不依赖收尾的 hook（PostToolUse / 主 drive 内）。

## 数据点

- opus solo off：51 轮、$36.6、613s、10/10、success=true、2.2M in / 46.5K out。
- 首个 solo 框架下 final_success=true 的端到端满分跑（此前 sonnet 全因行为短板报 false）。
