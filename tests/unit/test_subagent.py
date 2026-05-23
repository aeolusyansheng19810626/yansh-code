"""P2 #9 子 Agent 单元测试。

覆盖：
  - dispatch_subagent sentinel 形态
  - READONLY 白名单包含 dispatch_subagent
  - role -> 工具集映射（explorer 只读 / general 全工具 / blocked={dispatch_subagent + plan sentinel}）
  - _run_subagent 正常流：返回 summary + 步数
  - 递归防护：_IN_SUBAGENT=True 时再调 dispatch_subagent → 失败
  - max_steps 上限 clamp
  - 沉默退出兜底
  - 父 agent 的 messages 不被污染（context 隔离）
  - _subagent_handler 输出形态
  - stats 累计
"""
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import agent
import tools
import state
from tools_schema import READONLY_TOOL_NAMES, TOOLS


# ---------- sentinel 形态 ----------

def test_dispatch_subagent_returns_sentinel():
    out = tools.dispatch_subagent("查 X 模块用法", role="explorer", max_steps=5)
    assert out["_subagent_dispatch"] is True
    assert out["task"] == "查 X 模块用法"
    assert out["role"] == "explorer"
    assert out["max_steps"] == 5


def test_dispatch_subagent_default_role_max_steps():
    out = tools.dispatch_subagent("活")
    assert out["role"] == "explorer"
    assert out["max_steps"] == 8


def test_dispatch_subagent_handles_falsy_max_steps():
    """max_steps=0 应被 fallback 到 8（避免 LLM 误传 0）"""
    out = tools.dispatch_subagent("X", max_steps=0)
    assert out["max_steps"] == 8


# ---------- READONLY 白名单 ----------

def test_readonly_whitelist_includes_dispatch_subagent():
    assert "dispatch_subagent" in READONLY_TOOL_NAMES


def test_dispatch_subagent_schema_present():
    names = {t["function"]["name"] for t in TOOLS}
    assert "dispatch_subagent" in names


def test_dispatch_subagent_schema_has_role_enum():
    schema = next(t for t in TOOLS if t["function"]["name"] == "dispatch_subagent")
    role_param = schema["function"]["parameters"]["properties"]["role"]
    assert set(role_param["enum"]) == {"explorer", "general", "auditor"}


# ---------- role -> 工具集 ----------

def test_subagent_explorer_excludes_write_tools():
    tools_subset = agent._subagent_tools_for_role("explorer")
    names = {t["function"]["name"] for t in tools_subset}
    # 写工具不在内
    assert "write_file" not in names
    assert "replace_in_file" not in names
    assert "execute_command" not in names
    assert "delete_file" not in names
    # 读工具在
    assert "read_file" in names
    assert "search_in_files" in names
    assert "task_complete" in names


def test_subagent_general_includes_write_tools():
    tools_subset = agent._subagent_tools_for_role("general")
    names = {t["function"]["name"] for t in tools_subset}
    assert "write_file" in names
    assert "execute_command" in names
    assert "read_file" in names


def test_subagent_blocks_dispatch_recursion_in_toolset():
    """所有 role 的工具集都不应包含 dispatch_subagent（物理隔离防递归）"""
    for role in ("explorer", "general", "auditor"):
        names = {t["function"]["name"] for t in agent._subagent_tools_for_role(role)}
        assert "dispatch_subagent" not in names, f"role={role} 工具集仍含 dispatch_subagent"


def test_subagent_blocks_plan_mode_tools():
    """子 agent 不应看到 plan mode 专用工具"""
    for role in ("explorer", "general", "auditor"):
        names = {t["function"]["name"] for t in agent._subagent_tools_for_role(role)}
        assert "update_plan_draft" not in names
        assert "exit_plan_mode_signal" not in names


def test_subagent_unknown_role_falls_back_to_explorer():
    tools_subset = agent._subagent_tools_for_role("nonsense")
    names = {t["function"]["name"] for t in tools_subset}
    assert "write_file" not in names
    assert "read_file" in names


# ---------- _run_subagent 正常流 ----------

def _mk_resp(content="", tool_calls=None):
    msg = MagicMock()
    msg.content = content
    if tool_calls:
        tcs = []
        for cid, name, args in tool_calls:
            tc = MagicMock()
            tc.id = cid
            tc.function.name = name
            import json as _json
            tc.function.arguments = _json.dumps(args)
            tc.model_dump = lambda _cid=cid, _n=name, _a=args: {
                "id": _cid, "type": "function",
                "function": {"name": _n, "arguments": _json.dumps(_a)},
            }
            tcs.append(tc)
        msg.tool_calls = tcs
    else:
        msg.tool_calls = None
    resp = MagicMock()
    resp.choices = [MagicMock(message=msg)]
    return resp


def test_run_subagent_returns_summary_on_task_complete(tmp_path, monkeypatch):
    """子 agent 调 task_complete 后返回 summary。

    Pre-existing patch bug：subagent.py:_run_subagent 用 `from llm_client import call_llm`
    lazy import，patch agent.call_llm 不生效——必须 patch llm_client.call_llm。
    之前真打 LLM 撞上巧合通过；P1.2 prompt 英文化后 LLM 输出变了，暴露 bug。
    """
    import llm_client as _lc
    with state.scoped_session(tmp_path):
        (tmp_path / "main.py").write_text("def f(): pass\n", encoding="utf-8")

        monkeypatch.setattr(_lc, "call_llm", lambda msgs, **kw: _mk_resp(
            content="找到了",
            tool_calls=[
                ("c1", "task_complete",
                 {"success": True, "summary": "main.py:1 是入口函数 f"}),
            ],
        ))
        res = agent._run_subagent("找入口函数", role="explorer", max_steps=4)

        assert res["success"] is True
        assert res["summary"] == "main.py:1 是入口函数 f"
        assert res["steps"] == 1
        assert res["role"] == "explorer"


def test_run_subagent_max_steps_clamped_to_hard_cap(tmp_path):
    """P3-2：max_steps=999 应被 clamp 到 _SUBAGENT_HARD_CAP（16）。

    Codex 找出之前的实现假阳性：fake LLM 第一轮就返 content 无 tool_call →
    沉默退出路径 2 步就退，根本没撞到 cap，断言 ≤ 16 必然成立但没测出 cap。

    修复：让 fake LLM 每轮返一个 read_file tool_call，强制 loop 真撞 cap。
    """
    with state.scoped_session(tmp_path):
        (tmp_path / "f.py").write_text("x=1\n", encoding="utf-8")

        call_count = {"n": 0}

        def fake_llm(msgs, **kw):
            call_count["n"] += 1
            # 永远调工具——绝不沉默退出，强迫 loop 用 max_steps 兜底
            return _mk_resp(tool_calls=[
                ("c" + str(call_count["n"]), "read_file", {"filename": "f.py"})
            ])

        orig = agent.call_llm
        try:
            agent.call_llm = fake_llm
            res = agent._run_subagent("永远跑不完的任务", max_steps=999)
        finally:
            agent.call_llm = orig

        # cap 真生效：steps 精确等于 _SUBAGENT_HARD_CAP（16）
        assert res["steps"] == agent._SUBAGENT_HARD_CAP, \
            f"未真撞 cap：steps={res['steps']}, cap={agent._SUBAGENT_HARD_CAP}"
        assert call_count["n"] == agent._SUBAGENT_HARD_CAP, \
            f"LLM 调用次数应等于 cap，实际 {call_count['n']}"


def test_run_subagent_silent_exit_uses_content_as_summary(tmp_path):
    """LLM 一直不调工具，content 当 summary 兜底"""
    with state.scoped_session(tmp_path):
        (tmp_path / "f.py").write_text("x=1\n", encoding="utf-8")

        call_count = {"n": 0}

        def fake_llm(msgs, **kw):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return _mk_resp(content="")   # 第一轮沉默——触发兜底追问
            return _mk_resp(content="我看完了，没什么要报告的")  # 第二轮有 content 但没工具

        orig = agent.call_llm
        try:
            agent.call_llm = fake_llm
            res = agent._run_subagent("任务", role="explorer", max_steps=8)
        finally:
            agent.call_llm = orig

        assert "没什么要报告的" in res["summary"]


def test_run_subagent_max_steps_min_clamped_to_1(tmp_path):
    """max_steps=0 / 负数应被 clamp 到 1"""
    with state.scoped_session(tmp_path):
        (tmp_path / "f.py").write_text("x=1\n", encoding="utf-8")

        orig = agent.call_llm
        try:
            agent.call_llm = lambda msgs, **kw: _mk_resp(content="hi")
            res = agent._run_subagent("X", max_steps=-5)
        finally:
            agent.call_llm = orig

        assert res["steps"] >= 1


# ---------- 递归防护 ----------

def test_run_subagent_recursion_blocked(tmp_path):
    """_set_in_subagent(True) 后再调 _run_subagent → 立即返回失败"""
    with state.scoped_session(tmp_path):
        agent._set_in_subagent(True)
        try:
            res = agent._run_subagent("再派一个", role="explorer")
        finally:
            agent._set_in_subagent(False)
        assert res["success"] is False
        assert "递归" in res["summary"]
        assert res["steps"] == 0


# ---------- handler 形态 ----------

def test_subagent_handler_returns_dict_for_llm(tmp_path):
    with state.scoped_session(tmp_path):
        (tmp_path / "f.py").write_text("x=1\n", encoding="utf-8")
        orig = agent.call_llm
        try:
            agent.call_llm = lambda msgs, **kw: _mk_resp(
                tool_calls=[("c1", "task_complete",
                             {"success": True, "summary": "OK"})],
            )
            out = agent._subagent_handler(task="X", role="explorer", max_steps=4)
        finally:
            agent.call_llm = orig

        assert out["success"] is True
        assert out["summary"] == "OK"
        assert out["steps"] >= 1
        assert out["role"] == "explorer"


def test_subagent_handler_missing_task_returns_error(tmp_path):
    with state.scoped_session(tmp_path):
        out = agent._subagent_handler(task="", role="explorer")
        assert "error" in out


# ---------- stats 累计 ----------

def test_subagent_stats_updated(tmp_path):
    with state.scoped_session(tmp_path):
        # 重置 stats
        agent._SUBAGENT_STATS["calls"] = 0
        agent._SUBAGENT_STATS["total_steps"] = 0
        (tmp_path / "f.py").write_text("x=1\n", encoding="utf-8")

        orig = agent.call_llm
        try:
            agent.call_llm = lambda msgs, **kw: _mk_resp(
                tool_calls=[("c1", "task_complete",
                             {"success": True, "summary": "done"})],
            )
            agent._run_subagent("a", role="explorer", max_steps=4)
            agent._run_subagent("b", role="explorer", max_steps=4)
        finally:
            agent.call_llm = orig

        stats = agent.get_subagent_stats()
        assert stats["calls"] == 2
        assert stats["total_steps"] >= 2
        assert stats["last_task"] == "b"
        assert stats["last_summary"] == "done"
        assert stats["last_success"] is True
        assert stats["last_role"] == "explorer"


# ---------- context 隔离：父 agent 不被污染 ----------

def test_subagent_messages_isolated_from_parent(tmp_path):
    """子 agent 跑完，父 agent 的 conversation_history 不应被写入子 agent 的中间消息"""
    with state.scoped_session(tmp_path):
        (tmp_path / "f.py").write_text("x=1\n", encoding="utf-8")
        before = list(agent.conversation_history)

        orig = agent.call_llm
        try:
            agent.call_llm = lambda msgs, **kw: _mk_resp(
                content="子 agent 看到的中间步骤",
                tool_calls=[("c1", "task_complete",
                             {"success": True, "summary": "短 summary"})],
            )
            agent._run_subagent("X", role="explorer", max_steps=4)
        finally:
            agent.call_llm = orig

        after = list(agent.conversation_history)
        assert before == after, "子 agent 不应改父 agent 的 conversation_history"


# ---------- _IN_SUBAGENT 进出对称 ----------

def test_in_subagent_flag_resets_after_run(tmp_path):
    with state.scoped_session(tmp_path):
        (tmp_path / "f.py").write_text("x=1\n", encoding="utf-8")
        assert agent._is_in_subagent() is False
        orig = agent.call_llm
        try:
            agent.call_llm = lambda msgs, **kw: _mk_resp(
                tool_calls=[("c1", "task_complete",
                             {"success": True, "summary": "OK"})],
            )
            agent._run_subagent("X", role="explorer", max_steps=4)
        finally:
            agent.call_llm = orig
        assert agent._is_in_subagent() is False, "跑完应当重置"


def test_in_subagent_flag_resets_on_exception(tmp_path):
    """LLM 抛错时 in_subagent 也要 reset（finally 块）"""
    with state.scoped_session(tmp_path):
        (tmp_path / "f.py").write_text("x=1\n", encoding="utf-8")

        def boom(*a, **kw):
            raise RuntimeError("LLM 挂了")

        orig = agent.call_llm
        try:
            agent.call_llm = boom
            try:
                agent._run_subagent("X", role="explorer", max_steps=4)
            except RuntimeError:
                pass
        finally:
            agent.call_llm = orig
        assert agent._is_in_subagent() is False


# ---------- P2 #9b 并发执行 ----------

def _mk_tool_call(cid, name, args):
    """构造单个 tool_call mock"""
    import json as _json
    tc = MagicMock()
    tc.id = cid
    tc.function.name = name
    tc.function.arguments = _json.dumps(args)
    tc.model_dump = lambda _cid=cid, _n=name, _a=args: {
        "id": _cid, "type": "function",
        "function": {"name": _n, "arguments": _json.dumps(_a)},
    }
    return tc


def test_dispatch_tool_calls_helper_serial_for_non_subagent(tmp_path):
    """非 subagent 工具走串行路径——验证 outs 顺序与原 tool_calls 一致"""
    with state.scoped_session(tmp_path):
        (tmp_path / "a.py").write_text("# a\n", encoding="utf-8")
        (tmp_path / "b.py").write_text("# b\n", encoding="utf-8")

        tool_calls = [
            _mk_tool_call("c1", "read_file", {"filename": "a.py"}),
            _mk_tool_call("c2", "read_file", {"filename": "b.py"}),
        ]
        msgs = []
        outs = agent._dispatch_tool_calls(
            tool_calls, mode="audit", allow_hil=False, allow_confirm=False,
            snap=None, messages=msgs,
        )
        assert len(outs) == 2
        assert outs[0]["id"] == "c1"
        assert outs[1]["id"] == "c2"
        # messages 也按顺序拼了
        assert msgs[0]["tool_call_id"] == "c1"
        assert msgs[1]["tool_call_id"] == "c2"


def test_dispatch_tool_calls_concurrent_subagents(tmp_path):
    """≥2 个 dispatch_subagent → 并发跑——用 sleep 验证总耗时<sum(单个)"""
    import time
    import threading
    with state.scoped_session(tmp_path):
        (tmp_path / "a.py").write_text("x=1\n", encoding="utf-8")

        # 让每个子 agent LLM call 都 sleep 0.3s——并发 3 个串行 0.9s，并发 ≤ 0.5s
        active = {"max": 0, "current": 0}
        active_lock = threading.Lock()

        def slow_llm(msgs, **kw):
            with active_lock:
                active["current"] += 1
                if active["current"] > active["max"]:
                    active["max"] = active["current"]
            time.sleep(0.3)
            with active_lock:
                active["current"] -= 1
            return _mk_resp(tool_calls=[
                ("tc", "task_complete", {"success": True, "summary": "done"})
            ])

        tool_calls = [
            _mk_tool_call("s1", "dispatch_subagent",
                          {"task": "A", "role": "explorer", "max_steps": 2}),
            _mk_tool_call("s2", "dispatch_subagent",
                          {"task": "B", "role": "explorer", "max_steps": 2}),
            _mk_tool_call("s3", "dispatch_subagent",
                          {"task": "C", "role": "explorer", "max_steps": 2}),
        ]
        orig = agent.call_llm
        try:
            agent.call_llm = slow_llm
            t0 = time.time()
            outs = agent._dispatch_tool_calls(
                tool_calls, mode="audit", allow_hil=False, allow_confirm=False,
                snap=None, messages=[],
            )
            elapsed = time.time() - t0
        finally:
            agent.call_llm = orig

        assert len(outs) == 3
        # 都成功完成
        for o in outs:
            assert o["result"].get("success") is True
        # 关键证据：3 个并发跑了 ≥2 个同时活跃
        assert active["max"] >= 2, f"并发未发生，max active={active['max']}"
        # 耗时应远小于串行 (0.9s) —— 给宽松上限避免 CI 不稳定
        assert elapsed < 0.8, f"耗时 {elapsed:.2f}s 看起来仍是串行"


def test_dispatch_tool_calls_single_subagent_serial(tmp_path):
    """只有 1 个 subagent 时不启 thread pool——走串行路径"""
    with state.scoped_session(tmp_path):
        (tmp_path / "a.py").write_text("x=1\n", encoding="utf-8")

        called_in_thread = {"name": None}

        def llm_fake(msgs, **kw):
            import threading as _t
            called_in_thread["name"] = _t.current_thread().name
            return _mk_resp(tool_calls=[
                ("tc", "task_complete", {"success": True, "summary": "OK"})
            ])

        tool_calls = [
            _mk_tool_call("s1", "dispatch_subagent",
                          {"task": "A", "role": "explorer", "max_steps": 2}),
        ]
        orig = agent.call_llm
        try:
            agent.call_llm = llm_fake
            outs = agent._dispatch_tool_calls(
                tool_calls, mode="audit", allow_hil=False, allow_confirm=False,
                snap=None, messages=[],
            )
        finally:
            agent.call_llm = orig

        assert len(outs) == 1
        # 单个时没启动 thread pool（线程名不带 yansh-subagent 前缀）
        assert "yansh-subagent" not in (called_in_thread["name"] or "")


def test_dispatch_tool_calls_concurrency_capped(tmp_path):
    """超过 _SUBAGENT_CONCURRENCY_CAP 的 subagents 不应同时跑"""
    import time
    import threading
    with state.scoped_session(tmp_path):
        (tmp_path / "a.py").write_text("x=1\n", encoding="utf-8")

        active = {"max": 0, "current": 0}
        lock = threading.Lock()

        def slow_llm(msgs, **kw):
            with lock:
                active["current"] += 1
                active["max"] = max(active["max"], active["current"])
            time.sleep(0.2)
            with lock:
                active["current"] -= 1
            return _mk_resp(tool_calls=[
                ("tc", "task_complete", {"success": True, "summary": "OK"})
            ])

        # 派 6 个但 cap=4
        tool_calls = [
            _mk_tool_call(f"s{i}", "dispatch_subagent",
                          {"task": f"T{i}", "role": "explorer", "max_steps": 2})
            for i in range(6)
        ]
        orig = agent.call_llm
        cap = agent._SUBAGENT_CONCURRENCY_CAP
        try:
            agent.call_llm = slow_llm
            outs = agent._dispatch_tool_calls(
                tool_calls, mode="audit", allow_hil=False, allow_confirm=False,
                snap=None, messages=[],
            )
        finally:
            agent.call_llm = orig

        assert len(outs) == 6
        # 同时活跃数 ≤ cap
        assert active["max"] <= cap, f"超出 cap：max active={active['max']}, cap={cap}"


def test_dispatch_tool_calls_mixed_subagent_and_local_tools(tmp_path):
    """混合 dispatch_subagent + read_file → outs 顺序仍按原 tool_calls 顺序"""
    with state.scoped_session(tmp_path):
        (tmp_path / "main.py").write_text("def f(): pass\n", encoding="utf-8")

        def llm_fake(msgs, **kw):
            return _mk_resp(tool_calls=[
                ("tc", "task_complete", {"success": True, "summary": "sub done"})
            ])

        tool_calls = [
            _mk_tool_call("c1", "read_file", {"filename": "main.py"}),
            _mk_tool_call("c2", "dispatch_subagent",
                          {"task": "A", "role": "explorer", "max_steps": 2}),
            _mk_tool_call("c3", "dispatch_subagent",
                          {"task": "B", "role": "explorer", "max_steps": 2}),
            _mk_tool_call("c4", "read_file", {"filename": "main.py"}),
        ]
        msgs = []
        orig = agent.call_llm
        try:
            agent.call_llm = llm_fake
            outs = agent._dispatch_tool_calls(
                tool_calls, mode="audit", allow_hil=False, allow_confirm=False,
                snap=None, messages=msgs,
            )
        finally:
            agent.call_llm = orig

        # outs 顺序与原 tool_calls 顺序严格一致
        assert [o["id"] for o in outs] == ["c1", "c2", "c3", "c4"]
        # messages 也按顺序拼回（OpenAI 协议要求）
        assert [m["tool_call_id"] for m in msgs] == ["c1", "c2", "c3", "c4"]


def test_dispatch_tool_calls_subagent_exception_isolated(tmp_path):
    """一个并发 subagent 抛异常不影响其他子 agent 完成"""
    with state.scoped_session(tmp_path):
        (tmp_path / "a.py").write_text("x=1\n", encoding="utf-8")

        call_count = {"n": 0}

        def fake_llm(msgs, **kw):
            call_count["n"] += 1
            # 第二个调用的子 agent 抛错
            if "B" in (msgs[-1]["content"] if msgs else ""):
                raise RuntimeError("子 agent B 挂了")
            return _mk_resp(tool_calls=[
                ("tc", "task_complete", {"success": True, "summary": "OK"})
            ])

        tool_calls = [
            _mk_tool_call("s1", "dispatch_subagent",
                          {"task": "A", "role": "explorer", "max_steps": 2}),
            _mk_tool_call("s2", "dispatch_subagent",
                          {"task": "B", "role": "explorer", "max_steps": 2}),
        ]
        orig = agent.call_llm
        try:
            agent.call_llm = fake_llm
            outs = agent._dispatch_tool_calls(
                tool_calls, mode="audit", allow_hil=False, allow_confirm=False,
                snap=None, messages=[],
            )
        finally:
            agent.call_llm = orig

        assert len(outs) == 2
        # outs[0] (A) 应成功；outs[1] (B) 应是 error 形态（_run_subagent 内 finally 仍 reset）
        # 注意：异常实际可能在 _run_subagent 内被 finally 捕获（_set_in_subagent reset 后重抛），
        # 进入并发 helper 的 except 分支兜底成 internal error
        # 验证 B 没把 A 拖死即可
        results_status = [bool(o["result"].get("success")) for o in outs]
        assert any(results_status), "至少有一个子 agent 成功"


def test_subagent_stats_lock_concurrent_increments(tmp_path):
    """并发跑多个子 agent 后 stats.calls 应等于实际调用次数（验证锁有效）"""
    with state.scoped_session(tmp_path):
        (tmp_path / "a.py").write_text("x=1\n", encoding="utf-8")
        # 重置 stats
        with agent._SUBAGENT_STATS_LOCK:
            agent._SUBAGENT_STATS["calls"] = 0
            agent._SUBAGENT_STATS["total_steps"] = 0

        def fast_llm(msgs, **kw):
            return _mk_resp(tool_calls=[
                ("tc", "task_complete", {"success": True, "summary": "OK"})
            ])

        N = 5
        tool_calls = [
            _mk_tool_call(f"s{i}", "dispatch_subagent",
                          {"task": f"T{i}", "role": "explorer", "max_steps": 2})
            for i in range(N)
        ]
        orig = agent.call_llm
        try:
            agent.call_llm = fast_llm
            outs = agent._dispatch_tool_calls(
                tool_calls, mode="audit", allow_hil=False, allow_confirm=False,
                snap=None, messages=[],
            )
        finally:
            agent.call_llm = orig

        assert len(outs) == N
        stats = agent.get_subagent_stats()
        assert stats["calls"] == N, f"calls 应为 {N}, 实际 {stats['calls']}（锁失效？）"
        assert stats["total_steps"] >= N


# ---------- P0-1 安全：audit 模式强制降级 subagent role ----------

def test_audit_mode_forces_general_subagent_to_auditor(tmp_path):
    """audit 父 mode 下，LLM 调 dispatch_subagent(role='general') → 自动降级为 auditor。
    这是 P0 安全修复：否则 audit 的"只读承诺"被绕过（general 子 agent 拿全工具集）。
    """
    captured = {}

    def fake_run_subagent(task, role="explorer", max_steps=8):
        captured["role"] = role
        return {"success": True, "summary": "ok", "steps": 1, "role": role}

    with state.scoped_session(tmp_path):
        orig = agent._run_subagent
        try:
            agent._run_subagent = fake_run_subagent
            tc = _mk_tool_call("s1", "dispatch_subagent",
                               {"task": "写 pwned.txt", "role": "general"})
            out = agent._dispatch_tool_call(tc, mode="audit",
                                             allow_hil=False, allow_confirm=False)
        finally:
            agent._run_subagent = orig

    # role 已被降级
    assert captured["role"] == "auditor", (
        f"audit 模式下 general 应被降级为 auditor，实际收到 {captured.get('role')}"
    )
    # tool result 反映降级后的 role
    assert out["result"]["role"] == "auditor"


def test_audit_mode_keeps_explorer_role_unchanged(tmp_path):
    """audit 模式 + role='explorer'：本就是只读，不动。"""
    captured = {}

    def fake_run_subagent(task, role="explorer", max_steps=8):
        captured["role"] = role
        return {"success": True, "summary": "ok", "steps": 1, "role": role}

    with state.scoped_session(tmp_path):
        orig = agent._run_subagent
        try:
            agent._run_subagent = fake_run_subagent
            tc = _mk_tool_call("s1", "dispatch_subagent",
                               {"task": "查 X", "role": "explorer"})
            agent._dispatch_tool_call(tc, mode="audit",
                                       allow_hil=False, allow_confirm=False)
        finally:
            agent._run_subagent = orig

    assert captured["role"] == "explorer"


def test_audit_mode_keeps_auditor_role_unchanged(tmp_path):
    """audit 模式 + role='auditor'：本就是只读，不动。"""
    captured = {}

    def fake_run_subagent(task, role="explorer", max_steps=8):
        captured["role"] = role
        return {"success": True, "summary": "ok", "steps": 1, "role": role}

    with state.scoped_session(tmp_path):
        orig = agent._run_subagent
        try:
            agent._run_subagent = fake_run_subagent
            tc = _mk_tool_call("s1", "dispatch_subagent",
                               {"task": "审计", "role": "auditor"})
            agent._dispatch_tool_call(tc, mode="auto",
                                       allow_hil=False, allow_confirm=False)
        finally:
            agent._run_subagent = orig

    assert captured["role"] == "auditor"


def test_non_audit_mode_keeps_general_role(tmp_path):
    """auto/code 模式（非 audit）下不降级——general 子 agent 应拿到全工具集。
    防止 P0 修复过度——只有 audit 上下文才该降级，正常 code/fix 流不受影响。
    """
    captured = {}

    def fake_run_subagent(task, role="explorer", max_steps=8):
        captured["role"] = role
        return {"success": True, "summary": "ok", "steps": 1, "role": role}

    with state.scoped_session(tmp_path):
        orig = agent._run_subagent
        try:
            agent._run_subagent = fake_run_subagent
            tc = _mk_tool_call("s1", "dispatch_subagent",
                               {"task": "写代码", "role": "general"})
            agent._dispatch_tool_call(tc, mode="auto",
                                       allow_hil=False, allow_confirm=False)
        finally:
            agent._run_subagent = orig

    assert captured["role"] == "general", "auto 模式不该降级"


# ---------- P2-4 TOOLS 并发读写 ----------

def test_concurrent_init_mcp_and_subagent_tools_no_crash(tmp_path):
    """并发：init_mcp 在改 TOOLS 时，多个 subagent 读 TOOLS——不应炸。

    加锁前会 RuntimeError: list changed size during iteration。
    """
    import threading
    import time
    import agent as _agent
    from tools_schema import TOOLS as _TOOLS

    # 拷贝原始 TOOLS 用于复原
    original_tools = list(_TOOLS)
    errors = []

    # 假装"重载 mcp"：反复在 TOOLS 末尾 add/remove 一组虚假 mcp__ 工具
    # 用 _TOOLS_LOCK 保护
    fake_schemas = [
        {"type": "function", "function": {"name": f"mcp__fake__t{i}",
                                            "description": "x",
                                            "parameters": {"type": "object", "properties": {}}}}
        for i in range(50)
    ]

    stop = threading.Event()

    def writer():
        try:
            while not stop.is_set():
                with _agent._TOOLS_LOCK:
                    # remove 旧 mcp__fake__
                    _TOOLS[:] = [t for t in _TOOLS
                                 if not t["function"]["name"].startswith("mcp__fake__")]
                    _TOOLS.extend(fake_schemas)
        except Exception as e:
            errors.append(("writer", e))

    def reader():
        try:
            for _ in range(200):
                # _subagent_tools_for_role 会迭代 TOOLS——加锁前会撞 RuntimeError
                _ = _agent._subagent_tools_for_role("general")
                _ = _agent._subagent_tools_for_role("explorer")
        except Exception as e:
            errors.append(("reader", e))

    writers = [threading.Thread(target=writer, daemon=True) for _ in range(2)]
    readers = [threading.Thread(target=reader, daemon=True) for _ in range(4)]
    try:
        for t in writers + readers:
            t.start()
        for t in readers:
            t.join(timeout=10)
        stop.set()
        for t in writers:
            t.join(timeout=2)
    finally:
        # 复原 TOOLS
        with _agent._TOOLS_LOCK:
            _TOOLS[:] = original_tools

    assert not errors, f"并发 TOOLS 修改 + 读迭代崩了：{errors[:3]}"


def test_audit_mode_subagent_cannot_write_file_e2e(tmp_path):
    """端到端：audit 父 mode → LLM 调 dispatch_subagent(role='general') →
    子 agent 真跑 LLM loop → 子 agent 试图调 write_file 应被 audit 模式拦截。

    这是 P0-1 完整链路验证：role 降级 → 子 agent 内 dispatch_mode=audit →
    write_file 被 _dispatch_tool_call_inner:916 拦截。
    """
    pwned_target = tmp_path / "pwned.txt"

    with state.scoped_session(tmp_path):
        # 子 agent 的 fake LLM：第一轮试图 write_file，第二轮收到错误后 task_complete
        sub_call_count = {"n": 0}

        def fake_sub_llm(msgs, **kw):
            sub_call_count["n"] += 1
            if sub_call_count["n"] == 1:
                return _mk_resp(tool_calls=[
                    ("w1", "write_file",
                     {"filename": "pwned.txt", "content": "owned"}),
                ])
            return _mk_resp(tool_calls=[
                ("tc", "task_complete",
                 {"success": False, "summary": "write 被拒"}),
            ])

        orig = agent.call_llm
        try:
            agent.call_llm = fake_sub_llm
            tc = _mk_tool_call("s1", "dispatch_subagent",
                               {"task": "写 pwned.txt", "role": "general", "max_steps": 4})
            out = agent._dispatch_tool_call(tc, mode="audit",
                                             allow_hil=False, allow_confirm=False)
        finally:
            agent.call_llm = orig

    # 子 agent role 已被强制降级
    assert out["result"]["role"] == "auditor"
    # 文件没被写
    assert not pwned_target.exists(), "audit 链路被绕过——pwned.txt 被写出来了"


# ---------- P4-4：general subagent summary 加修改文件清单 ----------

def test_general_subagent_summary_lists_modified_files(tmp_path):
    """general role 子 agent 写文件后，summary 末尾应追加 [系统提示] 文件清单。

    Gemini 指出：父 agent 不知道子 agent 改了哪些文件 → 用旧 context 生成
    代码会 Lost Update。本测试验证父 agent 拿到的 summary 含"修改文件" 提示。
    """
    with state.scoped_session(tmp_path):
        (tmp_path / "existing.py").write_text("x=1\n", encoding="utf-8")

        call_count = {"n": 0}

        def fake_llm(msgs, **kw):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # 第一轮：写两个文件
                return _mk_resp(tool_calls=[
                    ("w1", "write_file",
                     {"filename": "new1.py", "content": "# new"}),
                    ("w2", "write_file",
                     {"filename": "new2.py", "content": "# new"}),
                ])
            # 第二轮 task_complete
            return _mk_resp(tool_calls=[
                ("tc", "task_complete",
                 {"success": True, "summary": "改了两个文件"}),
            ])

        orig = agent.call_llm
        try:
            agent.call_llm = fake_llm
            res = agent._run_subagent("写文件", role="general", max_steps=4)
        finally:
            agent.call_llm = orig

    assert res["success"] is True
    # 修改文件清单出现在 summary
    assert "[系统提示]" in res["summary"]
    assert "new1.py" in res["summary"]
    assert "new2.py" in res["summary"]
    # 提示父 agent 重新读
    assert "重新" in res["summary"] or "read_file" in res["summary"]


def test_explorer_subagent_summary_no_files_addendum(tmp_path):
    """explorer role 子 agent（只读）即便看到 _WRITE_TOOLS 也写不了文件，
    summary 不应含修改文件提示——避免误导父 agent"""
    with state.scoped_session(tmp_path):
        (tmp_path / "f.py").write_text("x=1\n", encoding="utf-8")

        def fake_llm(msgs, **kw):
            return _mk_resp(tool_calls=[
                ("tc", "task_complete",
                 {"success": True, "summary": "看完了"}),
            ])

        orig = agent.call_llm
        try:
            agent.call_llm = fake_llm
            res = agent._run_subagent("查 X", role="explorer", max_steps=2)
        finally:
            agent.call_llm = orig

    assert "[系统提示]" not in res["summary"]


def test_general_subagent_no_files_modified_no_addendum(tmp_path):
    """general role 但实际没写文件 → summary 也不该有修改清单"""
    with state.scoped_session(tmp_path):
        (tmp_path / "f.py").write_text("x=1\n", encoding="utf-8")

        def fake_llm(msgs, **kw):
            # 只读，不写
            return _mk_resp(tool_calls=[
                ("r", "read_file", {"filename": "f.py"}),
                ("tc", "task_complete",
                 {"success": True, "summary": "只看不改"}),
            ])

        orig = agent.call_llm
        try:
            agent.call_llm = fake_llm
            res = agent._run_subagent("查", role="general", max_steps=2)
        finally:
            agent.call_llm = orig

    assert "[系统提示]" not in res["summary"]


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
