"""P2 #11 Hooks 系统单元测试。

覆盖：
  - 配置加载（缺/优先级/坏 JSON）
  - matcher 匹配规则（精确/星号/None/不匹配）
  - 单个 hook 跑：allow / block / modify / system_message / 错误兜底
  - run_hook_event 聚合：多 hook 决策合并、modify 链式累积、block 早退
  - 集成 _dispatch_tool_call：PreToolUse 阻止 / 修改 tool_input；PostToolUse 修改 tool_output
  - 跳过场景：_is_in_subagent / 模块禁用
  - list_configured 给 /hooks 命令用
"""
import os
import sys
import json
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import hooks


# ---------- 配置加载 ----------

def test_load_config_missing_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "fake_home")
    cfg = hooks.load_config(workspace_dir=str(tmp_path))
    assert cfg == {}


def test_load_config_workspace_priority(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".yansh").mkdir(parents=True)
    (home / ".yansh" / "hooks.json").write_text(
        json.dumps({"hooks": {"PreToolUse": [{"matcher": "global"}]}}),
        encoding="utf-8",
    )
    ws = tmp_path / "ws"
    (ws / ".yansh").mkdir(parents=True)
    (ws / ".yansh" / "hooks.json").write_text(
        json.dumps({"hooks": {"PreToolUse": [{"matcher": "project"}]}}),
        encoding="utf-8",
    )
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    cfg = hooks.load_config(workspace_dir=str(ws))
    assert cfg["hooks"]["PreToolUse"][0]["matcher"] == "project"


def test_load_config_corrupt_returns_error(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".yansh").mkdir(parents=True)
    (home / ".yansh" / "hooks.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    cfg = hooks.load_config()
    assert "_error" in cfg


# ---------- matcher 规则 ----------

def test_matches_exact():
    assert hooks._matches("write_file", "write_file") is True
    assert hooks._matches("write_file", "read_file") is False


def test_matches_wildcard_or_empty():
    assert hooks._matches("*", "anything") is True
    assert hooks._matches("", "anything") is True
    assert hooks._matches(None, "anything") is True


def test_matches_target_none_with_wildcard():
    """UserPromptSubmit/Stop 调用时不传 target——应仍命中 *"""
    assert hooks._matches("*", None) is True
    assert hooks._matches(None, None) is True


def test_find_matching_hooks_filters_by_matcher():
    cfg = {"hooks": {"PreToolUse": [
        {"matcher": "write_file",
         "hooks": [{"type": "command", "command": "echo write"}]},
        {"matcher": "read_file",
         "hooks": [{"type": "command", "command": "echo read"}]},
        {"matcher": "*",
         "hooks": [{"type": "command", "command": "echo all"}]},
    ]}}
    matched = hooks._find_matching_hooks(cfg, "PreToolUse", "write_file")
    cmds = [m["command"] for m in matched]
    assert "echo write" in cmds
    assert "echo all" in cmds
    assert "echo read" not in cmds


def test_find_matching_hooks_skips_non_command_type():
    cfg = {"hooks": {"PreToolUse": [
        {"matcher": "*", "hooks": [{"type": "python", "command": "x"}]},
        {"matcher": "*", "hooks": [{"type": "command", "command": "ok"}]},
    ]}}
    matched = hooks._find_matching_hooks(cfg, "PreToolUse", "anything")
    assert len(matched) == 1
    assert matched[0]["command"] == "ok"


# ---------- 单 hook 跑（用真 subprocess） ----------

def _make_python_hook(body: str):
    """构造一个跨平台的 hook 命令：用 sys.executable -c '...'"""
    # 注意：shell=True 下 Windows / Linux 引号差异——用 base64 避免引号
    import base64
    encoded = base64.b64encode(body.encode("utf-8")).decode("ascii")
    return (
        f'{sys.executable} -c "import base64,sys; '
        f'exec(base64.b64decode(\'{encoded}\').decode())"'
    )


def test_run_one_hook_allow_empty_output():
    hook = {"command": _make_python_hook("pass"), "timeout": 5}
    result = hooks._run_one_hook(hook, {"event": "test"})
    assert result == {}


def test_run_one_hook_block():
    body = (
        "import sys, json\n"
        "print(json.dumps({'decision': 'block', 'reason': '禁止写敏感文件'}))\n"
    )
    hook = {"command": _make_python_hook(body), "timeout": 5}
    result = hooks._run_one_hook(hook, {"event": "PreToolUse"})
    assert result["decision"] == "block"
    assert "敏感" in result["reason"]


def test_run_one_hook_modify_tool_input():
    body = (
        "import sys, json\n"
        "data = json.loads(sys.stdin.read())\n"
        "ti = dict(data.get('tool_input', {}))\n"
        "ti['filename'] = 'safe_' + ti.get('filename', '')\n"
        "print(json.dumps({'modify': {'tool_input': ti}}))\n"
    )
    hook = {"command": _make_python_hook(body), "timeout": 5}
    result = hooks._run_one_hook(hook,
                                  {"event": "PreToolUse",
                                   "tool_input": {"filename": "x.py"}})
    assert result["modify"]["tool_input"]["filename"] == "safe_x.py"


def test_run_one_hook_invalid_json_returns_error():
    body = "print('not json output')"
    hook = {"command": _make_python_hook(body), "timeout": 5}
    result = hooks._run_one_hook(hook, {"event": "test"})
    assert "_hook_error" in result


def test_run_one_hook_command_not_found():
    hook = {"command": "this_command_definitely_does_not_exist_xyz_12345", "timeout": 2}
    result = hooks._run_one_hook(hook, {"event": "test"})
    # shell=True 下找不到命令仍不抛——shell 报"not found"，stdout 为空 → 返回空 dict（allow）
    # 这是预期行为：对未配置正确的 hook 保守 allow（不卡死主流程）
    assert "_hook_error" in result or result == {}


def test_run_one_hook_timeout():
    body = "import time; time.sleep(10)"
    hook = {"command": _make_python_hook(body), "timeout": 1}
    t0 = time.time()
    result = hooks._run_one_hook(hook, {"event": "test"})
    elapsed = time.time() - t0
    assert "_hook_error" in result
    assert "超时" in result["_hook_error"]
    assert elapsed < 5, f"超时应 ≤ 1s + buffer，实际 {elapsed:.1f}s"


# ---------- run_hook_event 聚合 ----------

def _write_hooks_json(tmp_path, monkeypatch, hooks_dict):
    """在 tmp_path/.yansh/hooks.json 写配置，并把 home 也指过去"""
    cfg_dir = tmp_path / ".yansh"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "hooks.json").write_text(json.dumps(hooks_dict), encoding="utf-8")
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)


def test_run_hook_event_no_match_returns_allow(tmp_path, monkeypatch):
    _write_hooks_json(tmp_path, monkeypatch, {
        "hooks": {"PreToolUse": [
            {"matcher": "write_file",
             "hooks": [{"type": "command", "command": "echo x"}]},
        ]}
    })
    result = hooks.run_hook_event("PreToolUse", {"event": "PreToolUse"},
                                   match_target="read_file",
                                   workspace_dir=str(tmp_path))
    assert result["decision"] == "allow"
    assert result["ran"] == 0


def test_run_hook_event_block_short_circuits(tmp_path, monkeypatch):
    """第一个 hook block，第二个不应再跑"""
    block_body = (
        "import json\n"
        "print(json.dumps({'decision': 'block', 'reason': '不许'}))\n"
    )
    sentinel_file = tmp_path / "second_ran.txt"
    second_body = (
        f"open(r'{sentinel_file}', 'w').write('1')\n"
    )
    _write_hooks_json(tmp_path, monkeypatch, {
        "hooks": {"PreToolUse": [
            {"matcher": "*",
             "hooks": [
                 {"type": "command", "command": _make_python_hook(block_body)},
                 {"type": "command", "command": _make_python_hook(second_body)},
             ]},
        ]}
    })
    result = hooks.run_hook_event("PreToolUse",
                                   {"event": "PreToolUse",
                                    "tool_input": {"filename": "x"}},
                                   match_target="any",
                                   workspace_dir=str(tmp_path))
    assert result["decision"] == "block"
    assert result["reason"] == "不许"
    assert result["ran"] == 1, f"block 后不应跑后续 hook，ran={result['ran']}"
    assert not sentinel_file.exists(), "第二个 hook 不应执行"


def test_run_hook_event_modify_chains(tmp_path, monkeypatch):
    """两个 modify hook 链式累积——第二个看到第一个的修改"""
    first = (
        "import sys, json\n"
        "d = json.loads(sys.stdin.read())\n"
        "ti = dict(d.get('tool_input', {}))\n"
        "ti['n'] = ti.get('n', 0) + 1\n"
        "print(json.dumps({'modify': {'tool_input': ti}}))\n"
    )
    second = (
        "import sys, json\n"
        "d = json.loads(sys.stdin.read())\n"
        "ti = dict(d.get('tool_input', {}))\n"
        "ti['n'] = ti.get('n', 0) * 10\n"
        "print(json.dumps({'modify': {'tool_input': ti}}))\n"
    )
    _write_hooks_json(tmp_path, monkeypatch, {
        "hooks": {"PreToolUse": [
            {"matcher": "*",
             "hooks": [
                 {"type": "command", "command": _make_python_hook(first)},
                 {"type": "command", "command": _make_python_hook(second)},
             ]},
        ]}
    })
    result = hooks.run_hook_event("PreToolUse",
                                   {"tool_input": {"n": 5}},
                                   match_target="any",
                                   workspace_dir=str(tmp_path))
    # 5+1=6, 6*10=60；第二个看到了第一个改后的 6
    assert result["modify"]["tool_input"]["n"] == 60


def test_run_hook_event_failure_does_not_block(tmp_path, monkeypatch):
    """坏 hook（非法 JSON）→ allow + errors 列表里有"""
    bad = "print('garbage output')"
    _write_hooks_json(tmp_path, monkeypatch, {
        "hooks": {"PreToolUse": [
            {"matcher": "*",
             "hooks": [{"type": "command", "command": _make_python_hook(bad)}]},
        ]}
    })
    result = hooks.run_hook_event("PreToolUse", {}, match_target="x",
                                   workspace_dir=str(tmp_path))
    assert result["decision"] == "allow"
    assert len(result["errors"]) == 1


def test_run_hook_event_disabled_returns_immediately(tmp_path, monkeypatch):
    _write_hooks_json(tmp_path, monkeypatch, {
        "hooks": {"PreToolUse": [
            {"matcher": "*",
             "hooks": [{"type": "command", "command": "echo nope"}]},
        ]}
    })
    hooks.set_disabled(True)
    try:
        result = hooks.run_hook_event("PreToolUse", {}, match_target="x",
                                       workspace_dir=str(tmp_path))
        assert result["ran"] == 0
        assert result["decision"] == "allow"
    finally:
        hooks.set_disabled(False)


def test_run_hook_event_unknown_event_skipped(tmp_path):
    result = hooks.run_hook_event("InvalidEvent", {}, workspace_dir=str(tmp_path))
    assert result["ran"] == 0
    assert "unknown event" in result.get("reason", "")


def test_run_hook_event_system_messages_collected(tmp_path, monkeypatch):
    body = (
        "import json\n"
        "print(json.dumps({'system_message': '提示：本次操作影响 prod'}))\n"
    )
    _write_hooks_json(tmp_path, monkeypatch, {
        "hooks": {"PreToolUse": [
            {"matcher": "*",
             "hooks": [{"type": "command", "command": _make_python_hook(body)}]},
        ]}
    })
    result = hooks.run_hook_event("PreToolUse", {}, match_target="x",
                                   workspace_dir=str(tmp_path))
    assert "提示：本次操作影响 prod" in result["system_messages"]


# ---------- 集成 _dispatch_tool_call ----------

def _mk_tool_call(cid, name, args):
    """构造单个 tool_call mock"""
    from unittest.mock import MagicMock
    tc = MagicMock()
    tc.id = cid
    tc.function.name = name
    tc.function.arguments = json.dumps(args)
    tc.model_dump = lambda: {"id": cid, "type": "function",
                             "function": {"name": name, "arguments": json.dumps(args)}}
    return tc


def test_pretoolse_block_returns_error_in_tool_result(tmp_path, monkeypatch):
    """PreToolUse hook block → 工具结果是 error，没真跑工具"""
    import agent
    block_body = (
        "import json\n"
        "print(json.dumps({'decision': 'block', 'reason': '禁止 read_file'}))\n"
    )
    _write_hooks_json(tmp_path, monkeypatch, {
        "hooks": {"PreToolUse": [
            {"matcher": "read_file",
             "hooks": [{"type": "command", "command": _make_python_hook(block_body)}]},
        ]}
    })
    import state
    with state.scoped_session(tmp_path):
        # 在 workspace 放一个文件——证明真跑工具会成功
        (tmp_path / "secret.txt").write_text("敏感", encoding="utf-8")

        tc = _mk_tool_call("c1", "read_file", {"filename": "secret.txt"})
        out = agent._dispatch_tool_call(tc, mode="audit",
                                         allow_hil=False, allow_confirm=False)
        # 应是 hook 阻止的 error，不含 "敏感"
        assert "error" in out["result"]
        assert "Hook 阻止" in out["result"]["error"]
        assert "敏感" not in str(out["result"])


def test_pretoolse_modify_changes_args(tmp_path, monkeypatch):
    """PreToolUse hook 把 filename 加前缀 → 真读到的是改后的文件"""
    import agent
    body = (
        "import sys, json\n"
        "d = json.loads(sys.stdin.read())\n"
        "ti = dict(d.get('tool_input', {}))\n"
        "ti['filename'] = 'real_' + ti['filename']\n"
        "print(json.dumps({'modify': {'tool_input': ti}}))\n"
    )
    _write_hooks_json(tmp_path, monkeypatch, {
        "hooks": {"PreToolUse": [
            {"matcher": "read_file",
             "hooks": [{"type": "command", "command": _make_python_hook(body)}]},
        ]}
    })
    import state
    with state.scoped_session(tmp_path):
        (tmp_path / "fake_one.txt").write_text("假的\n", encoding="utf-8")
        (tmp_path / "real_fake_one.txt").write_text("真的\n", encoding="utf-8")

        tc = _mk_tool_call("c1", "read_file", {"filename": "fake_one.txt"})
        out = agent._dispatch_tool_call(tc, mode="audit",
                                         allow_hil=False, allow_confirm=False)
        # 实际读的是改后的 real_fake_one.txt
        content = out["result"].get("content", "")
        assert "真的" in content
        assert "假的" not in content


def test_posttoolse_modify_changes_output(tmp_path, monkeypatch):
    """PostToolUse hook 改 tool_output → 上层看到改后的"""
    import agent
    body = (
        "import sys, json\n"
        "d = json.loads(sys.stdin.read())\n"
        "to = dict(d.get('tool_output', {}))\n"
        "to['content'] = '[REDACTED]'\n"
        "print(json.dumps({'modify': {'tool_output': to}}))\n"
    )
    _write_hooks_json(tmp_path, monkeypatch, {
        "hooks": {"PostToolUse": [
            {"matcher": "read_file",
             "hooks": [{"type": "command", "command": _make_python_hook(body)}]},
        ]}
    })
    import state
    with state.scoped_session(tmp_path):
        (tmp_path / "data.txt").write_text("机密", encoding="utf-8")

        tc = _mk_tool_call("c1", "read_file", {"filename": "data.txt"})
        out = agent._dispatch_tool_call(tc, mode="audit",
                                         allow_hil=False, allow_confirm=False)
        # 实际读到 "机密" 但 PostToolUse 改成 [REDACTED]
        assert out["result"]["content"] == "[REDACTED]"


def test_hooks_skipped_inside_subagent(tmp_path, monkeypatch):
    """子 agent 内部 _is_in_subagent()=True 时不触发 hooks"""
    import agent
    fired_file = tmp_path / "hook_fired.txt"
    body = (
        f"open(r'{fired_file}', 'w').write('fired')\n"
        "import json; print(json.dumps({}))\n"
    )
    _write_hooks_json(tmp_path, monkeypatch, {
        "hooks": {"PreToolUse": [
            {"matcher": "read_file",
             "hooks": [{"type": "command", "command": _make_python_hook(body)}]},
        ]}
    })
    import state
    with state.scoped_session(tmp_path):
        (tmp_path / "f.txt").write_text("x\n", encoding="utf-8")
        tc = _mk_tool_call("c1", "read_file", {"filename": "f.txt"})

        agent._set_in_subagent(True)
        try:
            agent._dispatch_tool_call(tc, mode="audit",
                                       allow_hil=False, allow_confirm=False)
        finally:
            agent._set_in_subagent(False)
        assert not fired_file.exists(), "子 agent 内不应触发 hook"


def test_hooks_skipped_when_module_disabled(tmp_path, monkeypatch):
    import agent
    fired_file = tmp_path / "fired.txt"
    body = (
        f"open(r'{fired_file}', 'w').write('1')\n"
        "import json; print(json.dumps({}))\n"
    )
    _write_hooks_json(tmp_path, monkeypatch, {
        "hooks": {"PreToolUse": [
            {"matcher": "read_file",
             "hooks": [{"type": "command", "command": _make_python_hook(body)}]},
        ]}
    })
    import state
    with state.scoped_session(tmp_path):
        (tmp_path / "f.txt").write_text("x", encoding="utf-8")
        tc = _mk_tool_call("c1", "read_file", {"filename": "f.txt"})

        hooks.set_disabled(True)
        try:
            agent._dispatch_tool_call(tc, mode="audit",
                                       allow_hil=False, allow_confirm=False)
        finally:
            hooks.set_disabled(False)
        assert not fired_file.exists()


# ---------- list_configured ----------

def test_list_configured_empty(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "fake_home")
    out = hooks.list_configured(workspace_dir=str(tmp_path))
    assert out == {}


def test_list_configured_returns_summary(tmp_path, monkeypatch):
    _write_hooks_json(tmp_path, monkeypatch, {
        "hooks": {
            "PreToolUse": [
                {"matcher": "write_file",
                 "hooks": [{"type": "command", "command": "echo a", "timeout": 5}]},
            ],
            "Stop": [
                {"matcher": "*",
                 "hooks": [{"type": "command", "command": "echo b"}]},
            ],
        },
    })
    out = hooks.list_configured(workspace_dir=str(tmp_path))
    assert "PreToolUse" in out
    assert out["PreToolUse"][0]["matcher"] == "write_file"
    assert out["PreToolUse"][0]["timeout"] == 5
    assert "Stop" in out


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
