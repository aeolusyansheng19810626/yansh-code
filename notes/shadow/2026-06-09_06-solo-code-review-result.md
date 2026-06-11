gpt-5.5 第二轮review 结果：

• 任务 1 真值表

  success 最终值：

   agent 最终意图 \ gate 结果                       无测试命令，break        gate 绿    红到 8 轮上限
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━  ━━━━━━━━━━━━━━━━━━━━━━━━━━━  ━━━━━━━━━━━━━  ━━━━━━━━━━━━━━━
   task_complete(success=true)              True [错配: 未复核即成功]           True            False
  ───────────────────────────────────────  ───────────────────────────  ─────────────  ───────────────
   task_complete(success=false) 主动放弃                        False    True [错配]            False
  ───────────────────────────────────────  ───────────────────────────  ─────────────  ───────────────
   no_progress 熔断                                             False    True [错配]            False
  ───────────────────────────────────────  ───────────────────────────  ─────────────  ───────────────
   沉默退出                                                     False    True [错配]            False
  ───────────────────────────────────────  ───────────────────────────  ─────────────  ───────────────
   soft_limit 超限                                              False    True [错配]            False

  补充：gate 绿 只有在测试命令确实覆盖本次修改时才可信；若 scope 落空后回退全量且全量不覆盖本次改动，表中所有 True 都可
  能是假阳性。

  P0 正确性

  - [严重度 高] gate 缺少三态结果，导致“agent 意图”和“测试复核”互相覆盖
      - 位置：agent.py:4125-4168
      - 问题：当前只维护一个 signal["success"]。它同时表达 agent 自述完成、agent 放弃、gate 测试结果。结果是：agent 失败
        意图会被绿测覆盖；agent 成功但无测试命令会原样返回成功。

      - 建议：拆成 agent_status 与 gate_status：agent_status = completed/abandoned/fused/silent/limit，gate_status =
        passed/failed/skipped/no_command。最终成功条件至少应是 agent_status == completed && gate_status == passed；无测
        试时返回 success=false 或 success=unknown。

  - [严重度 高] “无测试命令”分支会把 agent 自称成功直接放行
      - 位置：agent.py:4138-4140, agent.py:4168
      - 问题：如果 _solo_drive 返回 success=true，随后 not test_cmd 直接 break，最终 success=True。这等于零外部复核就报
        成功。

      - 建议：not test_cmd 时不要保留 true；设置 gate_status="skipped_no_command"，最终成功降级为 false 或要求显式配置允
        许跳过。

  - [严重度 高] gate 绿会覆盖所有非成功退出
      - 位置：agent.py:4141-4145
      - 问题：主动放弃、熔断、沉默、超限后，只要旧测试/无关测试返回 0，就被 signal["success"] = True 改成成功。
      - 建议：只有 signal["early_exit"] and signal["success"] 时，gate 绿才可确认成功；否则 gate 绿只能记录为“测试环境当
        前绿”，不能改变 agent 失败状态。

  - [严重度 高] 回灌内容选择应合并通道并按失败标志截取
      - 位置：agent.py:4151-4158, tools.py:410-459
      - 问题：pytest 的 FAILURES、assert diff、short summary 通常在 stdout；Python 运行时 traceback 常在 stderr。两个线
        程分别收集，无法恢复精确交错顺序，stderr or stdout 不可靠。

      - 建议：构造确定性 payload：
          - 始终保留 returncode、test_cmd。
          - 分别标注 STDOUT / STDERR，不要二选一。
          - pytest 命令优先截取 stdout 中包含 FAILURES、ERRORS、FAILED 、short test summary info、Traceback、
            AssertionError 的窗口；stderr 非空时附 stderr tail。

          - 非 pytest 的 python -m 优先 stderr 的 Traceback 窗口，同时附 stdout tail。
          - 截断采用“关键窗口 + tail”，不要简单 raw[-4000:]；assert diff 很长时，保留失败头、具体 traceback、summary 三
            段。

  - [严重度 中] gate 内一次 _solo_drive 可烧光全部剩余 120 轮
      - 位置：agent.py:3966-4062, agent.py:4146-4163
      - 问题：主循环硬上限是共享的 total_rounds < 120，不会超过 120 个 solo 主 LLM 轮。但在某个 gate 失败后，只要
        total_rounds < 120，一次回灌 _solo_drive 可以从当前轮数一路跑到 120，gate 无法按“每次失败只给少量修复轮”限流。

      - 建议：gate 修复应传单独 per_gate_round_limit，例如每次失败最多 5-10 轮；总预算和每次回灌预算分开。

  - [严重度 中] total_rounds 耗尽后还会多跑一次测试
      - 位置：agent.py:4132-4148
      - 问题：total_rounds >= soft_limit 的检查在测试失败之后。若上一轮 _solo_drive 已耗尽 120 轮，下一次 while 仍会先执
        行一次 test(test_cmd)，失败后才 break。

      - 建议：每轮 gate 顶部先检查 total_rounds >= soft_limit，再跑测试。

  - [严重度 中] “有进展”按工具名判断，不看工具结果
      - 位置：agent.py:4019-4024, agent.py:1690-1729
      - 问题：失败的 replace_in_file、失败的 write_file、重复失败的 execute_command 都会清零 no_progress。agent 可以反复
        失败但不触发探索熔断。

      - 建议：productive 应基于 dispatch result：写工具必须 result 含 success；命令应去重并要求输出/状态产生新信息。

  - [严重度 中] 同一错误可重复回灌到 8 轮，没有收敛检测
      - 位置：agent.py:4132-4163
      - 问题：未记录 test_cmd + 错误摘要 hash + modified snapshot。如果 drive 没有有效修改，gate 会重复跑同一测试、回灌
        同一错误。

      - 建议：若三元组连续不变，提前失败退出；summary 写明“无有效修改后同错复现”。

  P1 scope / 数据流

  - [严重度 中] scope 落空不会 break，而是回退全量，可能跑无关测试后放行
      - 位置：agent.py:4133-4137, 08_scope_chain.py:_infer_test_scope, _detect_python_test_cmd
      - 问题：scope=[] 被 _detect_python_test_cmd 当作 full suite。若没有对应测试但全量旧测试绿，gate 会
        signal["success"]=True，无法证明本次修改被覆盖。

      - 建议：区分 scope_miss 和 full_suite_intended。scope miss 时即使全量绿，也应在结果里标记 coverage=unknown，不能自
        动确认成功。

  - [严重度 中] 源文件到测试文件映射漏掉 *_test.py
      - 位置：08_scope_chain.py:_infer_test_scope
      - 问题：预扫只收集 tests/**/test_*.py，源文件 foo.py 只找 test_foo.py；常见的 foo_test.py 不会被命中。若全量测试绿
        但未覆盖本次改动，会误确认。

      - 建议：预扫同时索引 test_*.py 和 *_test.py，源文件同时匹配 test_<stem> 与 <stem>_test。

  - [严重度 中] move_file 成功不记录 modified，gate scope 会缺失
      - 位置：agent.py:1732-1741, task_log.py:162-176
      - 问题：move_file 返回 success 后没有 record_file_modified。纯移动/重命名任务后，snapshot_files_modified() 为空，
        scope 回退全量；若全量无关测试绿，会误报成功。

      - 建议：move 成功后同时记录 src 和 dst，至少记录 dst。

  - [严重度 低] execute_command 造成的文件副作用不会进入 scope
      - 位置：agent.py:4019-4024, task_log.py:162-176
      - 问题：agent 可通过命令生成/修改文件，但 task log 只记录写工具成功。此类改动 gate 无法推断相关测试。
      - 建议：执行命令后可对 workspace 做轻量 diff/snapshot，或禁止把命令副作用作为主要写入路径。

  - [严重度 低] solo gate 只调用 Python 测试探测
      - 位置：agent.py:4135-4137, 08_scope_chain.py:_detect_node_test_cmd
      - 问题：补充源码里有 node 测试探测，但 solo gate 没用。非 Python 项目会落到 python -m pytest，导致无关失败或错误回
        灌。

      - 建议：按项目类型选择 test detector；没有可用 detector 时明确 gate_status=no_command，不要伪造 pytest。

  P1 token / 上界

  - [严重度 中] solo 主 LLM 轮数有 120 上限，但总 LLM 调用没有同等上限
      - 位置：agent.py:3973, agent.py:3991, agent.py:4002
      - 问题：主模型调用最多 120 轮；但 compact summarizer 也会调用 LLM，且 dispatch_subagent 作为工具可再触发子 agent
        LLM 循环，不计入 total_rounds。

      - 建议：预算统计应覆盖主循环、compact、subagent；gate 回灌阶段最好禁用或限制 subagent。

  任务 4 触发链结论

  按补充 08_scope_chain.py，严格的“scope 落空 -> not test_cmd break”链路不成立：_detect_python_test_cmd(scope=[]) 会回退
  全量，最后甚至返回 python -m pytest。真实系统性失效链更常见是：

  agent task_complete(success=true) -> snapshot_files_modified 缺失或 _infer_test_scope 为空/漏匹配 -> gate 回退全量或包
  装器全量 -> 无关旧测试绿 -> signal["success"]=True -> 最终成功，但本次改动没有被相关测试复核。

  这比单点覆盖 bug更系统：scope miss、移动文件未记录、测试命名不匹配、非 Python 项目检测错误都会汇入同一个“绿测即覆盖”的
  错误假设。
  
  
 ------------------------------------------------------------
 opus4.8 第二轮review结果：
 
 ● 读完补充的 4 个文件，证据充分。下面先逐条裁决第一部分，再深化第二、三部分。

  ---
  第一部分 — 复核上轮「待确认」项

  1. threading.Lock —— 确认（你的判断成立），但 (b) 需补一句、(c) 建议采纳

  (a) 跨进程？确认不是 bug。 10_parallel_orchestrator.py:51 实锤：每子任务 workspace =
  base_cwd/.yansh/worktrees/<name>，独立进程 main --cwd <wt>。agent_state.md 走各自 _get_workspace()/.yansh（09 证实
  _get_workspace 实时读 --cwd 后的 WORKSPACE_DIR），路径互不相交 → 不存在跨进程写同一文件。threading.Lock 不是跨进程
  bug，成立。

  (b) 同进程 subagent 并发？确认 threading.Lock 够用。 _STATE_FILE_LOCK 是 tools.py 模块级全局，ThreadPoolExecutor
  起的多个 subagent 线程共享同一把锁；_update_agent_state 的读-改-写临界区（05:48-72）整段在 with _STATE_FILE_LOCK 内 →
  线程间正确串行化。这里够用，无需文件锁。 关键前提是 subagent 是线程而非子进程；09 的 dispatch 确为同进程线程，成立。

  (c) 原子写？建议加（低危但廉价）。 残留风险不是并发，而是中途被杀：编排器超时会
  procutil.kill_tree(proc)（10:89）杀整个进程组；若被杀线程正持锁执行 write_text，文件被截断/写坏，下次 run 读进来又注入
  system prompt（污染 + 可能 markdown 破形）。os.replace 在 Windows 上也是原子的。
  - 建议：tmp = state_path.with_suffix(".md.tmp"); tmp.write_text(...); os.replace(tmp,
  state_path)，仍在锁内。低，合入后跟进即可。

  2. compact M1 边界 —— 确认（你的判断成立，无可达反例）

  07 的 _split_messages_into_pairs 决定了：只有在 current 为空时才会产生「零散单消息 pair」，而 current 只在 rest
  起始处为空（一旦遇到首个 assistant 就再不为空，直到下个 assistant 才 flush）。推论链：
  - 任何非 rest 起始的 pair 必然 assistant 打头（07:14-17），且每 pair 恰含一个 assistant（不会「单 pair 多
  assistant」）。
  - 起始的零散 tool/user pair 要进 recent_pairs，必须 len(pairs) <= keep_recent_pairs → 但那样 _compact_messages:33
  已提前 return，根本不压。
  - 故真正触发压缩时，recent_pairs 全是 assistant 打头 → M1 的 first_role in ("assistant","user") 立即
  break，是个正确但几乎永远命中不到的兜底。
  - 「old_pairs 推空后 recent 仍非法」：while 条件要求 old_pairs 非空，且如上 recent 起始不可能是 tool → 不可达。

  孤立 tool_result 不可能，M1 充分。确认。 （附带正面结论：mid-stream 注入的 system 预算/熔断提示会被 07:19 的 else:
  current.append 吸进前一个 assistant pair，不破坏配对——也是安全的。）

  3. 摘要丢 plan-anchor —— 确认，最小修法选 (b) pin，但实现要做对

  07 _SUMMARIZE_SYSTEM 的强制项是 ③「已改动文件名+函数」和 ⑥「逐字保 shell 命令」。开场规划里尚未落地的文件清单 /
  跨文件接口签名 / symbol_contract 不属于「已改动」，③ 覆盖不到，⑥ 只管命令 → 规划在首个 assistant pair 里被当普通
  old_pair 走 LLM 摘要，会被泛化。与 _SOLO_ROLE:13「This plan is your anchor」直接冲突。确认成立。

  选 (b) pin 首个 assistant 规划 pair 进 head。理由（三维度全胜）：
  - 更稳：(a) 是给 summarizer 加一条指令，仍是非确定性 LLM 行为，且每次 compact
  都是「摘要的再摘要」，签名会跨多次压缩逐代衰减——恰恰是要防的失效模式。(b) 让规划逐字不参与压缩，零漂移、确定性。
  - 更省：_SOLO_ROLE:9 要求规划写在「FIRST assistant turn 的 message body」（纯文本、无 tool_calls），体量是几百 token
  量级；(b) 每轮多发这固定小块，远小于 compact 本要省的多 KB 历史。(a) 反而每次 compact 都重述一遍签名进摘要，长任务多次
  compact 累计未必更省。
  - 更不易回退：(a) 一旦 summarizer 某次没听话就静默丢，难复现；(b) 是结构保证。

  实现警告（别踩）：不能只靠「位置」固定。首次 compact 后 messages 变 [sys, user, assistant_plan, sys_summary,
  recent...]，第二次 compact 的 head 检测（01:21-27）只抓 system+user，assistant_plan 会落进 rest 再被压。必须显式把首个
  assistant pair 并入 head 并在每次 compact 持久保留（例如 head 检测后追加 if msgs[head_count] is assistant: head_count
  += len(该 pair)，或单独存 _solo_plan_pair 每次重建 msgs 时强制置顶）。否则修了第一次、第二次又丢。

  ---
  第二部分 — 深化 test gate 超时

  [高] gate 用普通命令的 30s 超时 + judge 三类混淆，叠加放大空转

  (1) 独立可配置超时，不动普通命令 30s：
  - test()（04:226）加形参透传：def test(test_command, timeout_sec=None): ... return execute_command(test_command,
  _timeout_sec=timeout_sec or int(_cfg("test_gate_timeout_sec") or 300))
  - solo gate（04:187）改 test(test_cmd, timeout_sec=...)；agent 在 loop 内自己跑的 execute_command 仍走默认
  30s。隔离干净。

  (2) judge 混淆——这是放大器，确认成立。 judge（04:235）只看 returncode==0，把下面三类压成同一个「红」：
  - 超时被强杀：execute_command 超时分支（06:97）返回 error_kind="timeout"、returncode=-1，stderr 是被 kill
  时的半截输出；
  - 进程崩溃/收集失败：import error / collected 0 items → pytest rc=2，或段错误负 rc；
  - 真断言失败：pytest rc=1，输出含 FAILED/assert。

  回灌时 agent 只拿到 raw[-4000:]（04:197-198），没有任何标签区分这三类。后果正如你担心：agent 把「太慢被砍的半截
  run」误读成代码错，去改本来正确的逻辑，或在「修不动的超时」上烧满 8 轮 gate——30s 问题被 judge 的信息丢失二次放大。

  最小改动（让 agent 能区分三类）：
  - 加分类器（复用 06 已有的 error_kind 标准化）：
  def _classify_test_failure(tr):
      if tr.get("error_kind") == "timeout":
          return ("timeout", "测试被超时强杀（>N 秒）——可能用例太慢或死循环/死锁，"
                  "不要当成断言失败盲改业务逻辑；考虑缩小本轮 scope 或排查阻塞点。")
      out = (tr.get("stderr") or "") + (tr.get("stdout") or "")
      rc = tr.get("returncode")
      if rc == 2 or "ModuleNotFoundError" in out or "ImportError" in out or "collected 0 items" in out:
          return ("uncollectable", "测试无法收集/运行（导入或崩溃），先修可运行性与 import 链，再谈断言。")
      return ("assertion", "断言失败，正常定位修复。")
  - 把 (kind, hint) 拼进回灌消息头部（替换 04:200-206 的纯 stderr 回灌），让 agent 知道这轮属于哪类。
  - 早停止血：连续 2 轮 kind=="timeout" 且 err_excerpt 与上轮高度相同 → 不再回灌，直接判 gate 结束（成功与否按最后一次
  assertion-class 结果），避免在超时上烧光 8 轮。

  ---
  第三部分 — 新地带

  1. [中｜部分待确认] _CURRENT_SNAPSHOT 是被并发 subagent 共享改写的全局态

  solo() 一次性建空快照（04:164）并以 snap=_CURRENT_SNAPSHOT 贯穿所有 _solo_drive+gate（04:50）。两点：
  - 并发写一致性（待确认 snapshot 内部）：同进程 ThreadPoolExecutor 跑的多个 subagent，其内部写工具会对同一个
  _CURRENT_SNAPSHOT 做「按需增量备份」。agent_state.md 有 _STATE_FILE_LOCK
  护着，但快照的增量备份是否同样加锁未在审范围内。若 snapshot 的备份 dict/list 改写无锁，多线程首触不同文件时存在
  race（漏备份 → /revert 还原不全）。→ 待确认，但「共享可变全局 + ThreadPoolExecutor」这个形状本身就该核 snapshot
  模块有没有锁。
  - /revert 语义（待确认首触语义）：空快照贯穿全 run 是对的——/revert 应还原到 solo
  启动前。但前提是增量备份遵循「仅首次触碰才存原始内容」；若某文件第 1 轮写、第 5 轮又写，备份必须保留的是第 1
  轮前的原版。若实现是「每次写都覆盖备份」，则 /revert 会还原成第 4 轮版本而非原始 → 错。solo 多轮 + gate
  回灌下这条比逐文件模式更容易踩。请核 snapshot 增量备份是不是 first-touch-only。

  2. [高｜确认] solo gate 绕过 smoke test —— 与第一部分第 3 条叠加，正是「200 单测全绿 / python -m pkg 9/10
  崩」的复发路径

  链路核实：
  - smoke 保险丝只在 _apply_test_scope_override（08:73-75）里强制并入 tests/test_smoke.py，而该函数只走 plan 路径。
  - solo gate（04:179-183）直接 _infer_test_scope(modified) → _detect_python_test_cmd(scope=scope)，不经过
  _apply_test_scope_override，无 smoke 强并入。
  - 更糟的是落空时机：_infer_test_scope 把改过的 lexer.py/parser.py 映射成 test_lexer.py/test_parser.py，scope 非空 →
  _detect_python_test_cmd(scope=非空) 必返回 "pytest tests/test_lexer.py ..."（08:100,113），or
  _detect_python_test_cmd(ws) 全套兜底永不触发（前者恒真）。即agent 越认真写对应单测，scope 越满，smoke 越被排除；只有
  agent 一个对应单测都没写、scope 为空时，才回退全套跑到 smoke。矛盾确认成立。

  叠加放大（确认）：跨文件 CLI 调用链断裂（entrypoint / __main__ / import 链 / 参数装配坏了，但各模块单测各自 mock
  着过）这一经典失效，gate 层唯一信号就是 smoke test——solo 把它跳过了；loop 层信号是 _SOLO_ROLE
  的「必须跑真实入口」——而它在首次 compact 后随 plan-anchor 一起被摘要弱化（第一部分第 3 条）。两道防线同时失效 →
  正是你给的背景「200 单测全绿但 python -m pkg 9/10 崩」的复发条件。这是本轮最值得修的一条。

  最小修法（顺手消除 plan/solo 两路分叉）：把 smoke 强并入抽成独立 helper，两边都调：
  def _force_include_smoke(scope: list[str], ws: Path) -> list[str]:
      smoke = "tests/test_smoke.py"
      if (ws / "tests" / "test_smoke.py").is_file() and smoke not in scope:
          scope.append(smoke)
      return scope
  _apply_test_scope_override 内替换原 08:73-75；solo gate 在 04:180 后加 scope = _force_include_smoke(scope,
  _ws_path)。这样 smoke 在 scope 非空时也必跑，且消除「保险丝只挂在 plan 一侧」的隐藏耦合。（与第二部分配合：smoke
  跑出来若是崩溃类，分类器会标 uncollectable，引导 agent 先修可运行性。）

  ---
  总体判断 — 阻塞项相比上轮的收敛

  上轮三个待定项裁决后：

  ┌────────────────────┬─────────────────────────────────────────────────────┬──────────────────────────────────────┐
  │         项         │                        结果                         │              阻塞性变化              │
  ├────────────────────┼─────────────────────────────────────────────────────┼──────────────────────────────────────┤
  │ threading.Lock     │ 推翻为非 bug（每 worktree                           │ 移出阻塞清单，仅留「原子写」低危跟进 │
  │ 跨进程             │ 独立进程独立文件；同进程线程锁够用）                │                                      │
  ├────────────────────┼─────────────────────────────────────────────────────┼──────────────────────────────────────┤
  │ compact M1 边界    │ 确认安全（无可达反例）                              │ 本就非阻塞，关闭                     │
  ├────────────────────┼─────────────────────────────────────────────────────┼──────────────────────────────────────┤
  │ compact 丢         │ 确认，定修法 (b) pin                                │ 仍阻塞                               │
  │ plan-anchor        │                                                     │                                      │
  └────────────────────┴─────────────────────────────────────────────────────┴──────────────────────────────────────┘

  净阻塞清单从「3 待定 + 1 已确认（gate 超时）」收敛为 3 条相互咬合的必修项：

  1. gate 超时 + judge 三类混淆（第二部分，高）——独立超时 + 失败分类 + 超时早停；
  2. compact 丢 plan-anchor（第一部分 3，中-高）——(b) pin 首个规划 pair，注意跨多次 compact 的持久化实现；
  3. solo gate 绕过 smoke（第三部分 2，高）——_force_include_smoke 两路共用。

  这三条不是独立的——2 让 agent 看不见真实入口契约，3 让 gate 不跑真实入口，1 让 agent
  即便跑了也分不清失败类型，三者合起来恰好重建「单测全绿 /
  端到端崩」的失效。建议三条一并修后再合入长期使用；修完后这是阻塞项归零的状态。

  待确认仅剩 1 条（不阻塞、需核审范围外模块）：_CURRENT_SNAPSHOT 增量备份在并发 subagent 下是否加锁、是否
  first-touch-only 语义（第三部分 1）。其余（正则词边界、state 文件无界增长、\r 归一化）维持上轮低危跟进结论，不变。
