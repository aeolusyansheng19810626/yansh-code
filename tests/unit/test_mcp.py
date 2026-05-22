"""P2 #10 MCP 客户端单元测试。

覆盖：
  - 配置加载（项目级优先 / 缺文件 / 损坏 JSON）
  - 工具名前缀解析 parse_prefixed
  - MCPServer 协议序列：用一个伪造的 stdio mock server
  - JSON-RPC：initialize / tools/list / tools/call 来回正确
  - 超时
  - call_tool 整合 content[]
  - shutdown 不抛异常
  - agent.py 路由：mcp__ 前缀分发到 _mcp_mod.call_tool
"""
import os
import sys
import json
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import mcp_client


# ---------- 配置加载 ----------

def test_load_config_missing_returns_empty(tmp_path, monkeypatch):
    """没 mcp.json 文件 → 返回 {}"""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "fake_home")
    cfg = mcp_client.load_config(workspace_dir=str(tmp_path))
    assert cfg == {}


def test_load_config_workspace_priority(tmp_path, monkeypatch):
    """项目级 mcp.json 应覆盖全局（已 trust 的前提下）"""
    home = tmp_path / "home"
    (home / ".yansh").mkdir(parents=True)
    (home / ".yansh" / "mcp.json").write_text(
        json.dumps({"mcpServers": {"global_only": {"command": "x"}}}),
        encoding="utf-8",
    )
    ws = tmp_path / "ws"
    (ws / ".yansh").mkdir(parents=True)
    (ws / ".yansh" / "mcp.json").write_text(
        json.dumps({"mcpServers": {"project_only": {"command": "y"}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    monkeypatch.setenv("YANSH_TRUST_PROJECT_CONFIG", "always")
    cfg = mcp_client.load_config(workspace_dir=str(ws))
    assert "project_only" in cfg["mcpServers"]
    assert "global_only" not in cfg["mcpServers"]


def test_load_config_corrupt_returns_error_dict(tmp_path, monkeypatch):
    """坏 JSON → 返回 {"_error": ...} 而不是抛"""
    home = tmp_path / "home"
    (home / ".yansh").mkdir(parents=True)
    (home / ".yansh" / "mcp.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    cfg = mcp_client.load_config()
    assert "_error" in cfg


# ---------- 前缀解析 ----------

def test_parse_prefixed_normal():
    assert mcp_client.parse_prefixed("mcp__github__list_issues") == ("github", "list_issues")


def test_parse_prefixed_tool_with_underscores():
    """tool name 含下划线时，第一个 __ 之后全是 tool name"""
    assert mcp_client.parse_prefixed("mcp__server__some_long_tool_name") == ("server", "some_long_tool_name")


def test_parse_prefixed_invalid_no_prefix():
    assert mcp_client.parse_prefixed("read_file") is None


def test_parse_prefixed_invalid_no_separator():
    assert mcp_client.parse_prefixed("mcp__onlyserver") is None


# ---------- MCPServer 用 mock subprocess ----------

class _MockProc:
    """伪造一个 subprocess.Popen 的 stdio。
    主线程往 stdin 写，本 mock 在另一线程模拟 server 行为，写回 stdout。
    """
    def __init__(self, server_logic):
        """server_logic: (request_dict) -> response_dict（None 表示无响应——通知）"""
        import io
        self._stdin_r, self._stdin_w = os.pipe()
        self._stdout_r, self._stdout_w = os.pipe()
        self._stderr_r, self._stderr_w = os.pipe()
        self.stdin = os.fdopen(self._stdin_w, "w", buffering=1, encoding="utf-8")
        self.stdout = os.fdopen(self._stdout_r, "r", buffering=1, encoding="utf-8")
        self.stderr = os.fdopen(self._stderr_r, "r", buffering=1, encoding="utf-8")
        self._stdin_read = os.fdopen(self._stdin_r, "r", buffering=1, encoding="utf-8")
        self._stdout_write = os.fdopen(self._stdout_w, "w", buffering=1, encoding="utf-8")
        self._alive = True
        self._server_logic = server_logic
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="mock-mcp-server")
        self._thread.start()

    def _run(self):
        try:
            for line in self._stdin_read:
                line = line.strip()
                if not line:
                    continue
                try:
                    req = json.loads(line)
                except json.JSONDecodeError:
                    continue
                resp = self._server_logic(req)
                if resp is not None:
                    self._stdout_write.write(json.dumps(resp) + "\n")
                    self._stdout_write.flush()
        except Exception:
            pass

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self._alive = False
        try:
            self.stdin.close()
        except Exception:
            pass

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.terminate()


def _make_echo_server_logic(tools=None):
    """创建一个简单的 server logic：响应 initialize / tools/list / tools/call"""
    if tools is None:
        tools = [
            {"name": "echo", "description": "echo 输入",
             "inputSchema": {"type": "object",
                             "properties": {"text": {"type": "string"}},
                             "required": ["text"]}},
            {"name": "add", "description": "加法",
             "inputSchema": {"type": "object",
                             "properties": {"a": {"type": "number"},
                                            "b": {"type": "number"}},
                             "required": ["a", "b"]}},
        ]

    def logic(req):
        method = req.get("method")
        mid = req.get("id")
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": mid,
                    "result": {"protocolVersion": "2024-11-05",
                               "capabilities": {},
                               "serverInfo": {"name": "mock", "version": "0"}}}
        if method == "notifications/initialized":
            return None  # 通知无响应
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": mid, "result": {"tools": tools}}
        if method == "tools/call":
            params = req.get("params", {})
            name = params.get("name")
            args = params.get("arguments", {})
            if name == "echo":
                return {"jsonrpc": "2.0", "id": mid,
                        "result": {"content": [{"type": "text",
                                                "text": f"echo: {args.get('text','')}"}]}}
            if name == "add":
                s = args.get("a", 0) + args.get("b", 0)
                return {"jsonrpc": "2.0", "id": mid,
                        "result": {"content": [{"type": "text", "text": str(s)}]}}
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32601, "message": f"unknown tool: {name}"}}
        # 未知方法
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": -32601, "message": f"unknown method: {method}"}}
    return logic


def _make_server_with_mock(name="mock", tools=None):
    """构造 MCPServer + 注入 mock subprocess（绕过真实 spawn）"""
    srv = mcp_client.MCPServer(name=name, command="dummy", args=[])
    srv.proc = _MockProc(_make_echo_server_logic(tools))
    # 启动 reader/stderr 线程
    srv._reader_thread = threading.Thread(target=srv._reader_loop, daemon=True)
    srv._reader_thread.start()
    srv._stderr_thread = threading.Thread(target=srv._stderr_loop, daemon=True)
    srv._stderr_thread.start()
    return srv


def test_mcpserver_initialize_and_tools_list():
    """initialize + tools/list 协议序列"""
    srv = _make_server_with_mock()
    try:
        srv._do_initialize(timeout=2)
        srv._fetch_tools(timeout=2)
        assert srv._initialized is True
        names = [t["name"] for t in srv.tools]
        assert "echo" in names
        assert "add" in names
    finally:
        srv.shutdown()


def test_mcpserver_call_tool_returns_content():
    """tools/call → 返回 content[] 形态"""
    srv = _make_server_with_mock()
    try:
        srv._do_initialize(timeout=2)
        srv._fetch_tools(timeout=2)
        result = srv.call_tool("add", {"a": 3, "b": 4}, timeout=2)
        content = result.get("content", [])
        assert isinstance(content, list)
        assert content[0]["type"] == "text"
        assert content[0]["text"] == "7"
    finally:
        srv.shutdown()


def test_mcpserver_call_tool_unknown_raises():
    srv = _make_server_with_mock()
    try:
        srv._do_initialize(timeout=2)
        srv._fetch_tools(timeout=2)
        try:
            srv.call_tool("nonexistent", {}, timeout=2)
            assert False, "应抛 RuntimeError"
        except RuntimeError as e:
            assert "unknown tool" in str(e)
    finally:
        srv.shutdown()


def test_mcpserver_call_before_init_raises():
    srv = _make_server_with_mock()
    try:
        try:
            srv.call_tool("echo", {})
            assert False, "应抛"
        except RuntimeError as e:
            assert "未完成 initialize" in str(e)
    finally:
        srv.shutdown()


def test_mcpserver_request_timeout():
    """server 不响应 → 超时抛 TimeoutError"""
    def silent_logic(req):
        return None   # 永不响应
    srv = mcp_client.MCPServer(name="silent", command="dummy")
    srv.proc = _MockProc(silent_logic)
    srv._reader_thread = threading.Thread(target=srv._reader_loop, daemon=True)
    srv._reader_thread.start()
    srv._stderr_thread = threading.Thread(target=srv._stderr_loop, daemon=True)
    srv._stderr_thread.start()
    try:
        try:
            srv._request("initialize", {}, timeout=0.3)
            assert False, "应超时"
        except TimeoutError as e:
            assert "超时" in str(e)
    finally:
        srv.shutdown()


def test_mcpserver_shutdown_idempotent():
    """重复 shutdown 不应抛"""
    srv = _make_server_with_mock()
    srv._do_initialize(timeout=2)
    srv.shutdown()
    srv.shutdown()  # 再来一次不应抛


# ---------- 模块级 registry ----------

def test_call_tool_unknown_server():
    """调一个不存在的 server → error 字典"""
    res = mcp_client.call_tool("mcp__nonexistent__tool", {})
    assert "error" in res


def test_call_tool_invalid_prefix():
    res = mcp_client.call_tool("not_mcp_prefix", {})
    assert "error" in res


def test_call_tool_returns_text_content():
    """注册一个 mock server，验证 call_tool 把 content[] 合成 text"""
    srv = _make_server_with_mock(name="testsrv")
    srv._do_initialize(timeout=2)
    srv._fetch_tools(timeout=2)
    with mcp_client._servers_lock:
        mcp_client._servers["testsrv"] = srv
    try:
        res = mcp_client.call_tool("mcp__testsrv__echo", {"text": "hello"}, timeout=2)
        assert "error" not in res
        assert "echo: hello" in res["content"]
        assert res["isError"] is False
    finally:
        with mcp_client._servers_lock:
            mcp_client._servers.pop("testsrv", None)
        srv.shutdown()


def test_discover_tools_as_schemas_includes_prefix():
    """discover_tools_as_schemas 给出的工具名带 mcp__<server>__ 前缀"""
    srv = _make_server_with_mock(name="abc")
    srv._do_initialize(timeout=2)
    srv._fetch_tools(timeout=2)
    with mcp_client._servers_lock:
        mcp_client._servers["abc"] = srv
    try:
        schemas = mcp_client.discover_tools_as_schemas()
        names = {s["function"]["name"] for s in schemas}
        assert "mcp__abc__echo" in names
        assert "mcp__abc__add" in names
        # description 含 [MCP/abc] 前缀
        echo_schema = next(s for s in schemas
                           if s["function"]["name"] == "mcp__abc__echo")
        assert "[MCP/abc]" in echo_schema["function"]["description"]
        # parameters 透传
        assert echo_schema["function"]["parameters"]["properties"]["text"]["type"] == "string"
    finally:
        with mcp_client._servers_lock:
            mcp_client._servers.pop("abc", None)
        srv.shutdown()


def test_shutdown_all_clears_registry():
    srv = _make_server_with_mock(name="cleanup_test")
    srv._do_initialize(timeout=2)
    srv._fetch_tools(timeout=2)
    with mcp_client._servers_lock:
        mcp_client._servers["cleanup_test"] = srv
    mcp_client.shutdown_all()
    with mcp_client._servers_lock:
        assert "cleanup_test" not in mcp_client._servers


# ---------- agent.py 路由 ----------

def test_agent_dispatch_mcp_tool_routes_to_call_tool(monkeypatch):
    """LLM 调 mcp__... 的 tool_call → _dispatch_tool_call 转发到 mcp_client.call_tool"""
    import agent
    from unittest.mock import MagicMock

    # mock 一个 tool_call
    tc = MagicMock()
    tc.id = "abc"
    tc.function.name = "mcp__demo__echo"
    tc.function.arguments = json.dumps({"text": "hi"})

    # 拦截 mcp_client.call_tool 验证被调用
    captured = {}

    def fake_call_tool(name, args, timeout=60):
        captured["name"] = name
        captured["args"] = args
        return {"content": "echo: hi", "isError": False}

    monkeypatch.setattr("mcp_client.call_tool", fake_call_tool)

    # 用 auto 模式——audit 模式会拦截非 READONLY 工具（mcp 不在白名单内，
    # 因为第三方 mcp 工具可能改外部状态，audit 模式默认禁用是对的）
    out = agent._dispatch_tool_call(tc, mode="auto", allow_hil=False, allow_confirm=False)
    assert captured["name"] == "mcp__demo__echo"
    assert captured["args"] == {"text": "hi"}
    assert out["result"]["content"] == "echo: hi"


def test_agent_dispatch_mcp_blocked_in_audit_mode():
    """audit 模式应拦截 mcp 工具（mcp 不在 READONLY 白名单——第三方可能改外部状态）"""
    import agent
    from unittest.mock import MagicMock

    tc = MagicMock()
    tc.id = "abc"
    tc.function.name = "mcp__demo__echo"
    tc.function.arguments = json.dumps({"text": "hi"})

    out = agent._dispatch_tool_call(tc, mode="audit", allow_hil=False, allow_confirm=False)
    assert "error" in out["result"]
    assert "audit" in out["result"]["error"]


def test_agent_init_mcp_extends_TOOLS(monkeypatch, tmp_path):
    """init_mcp 启动 server 后应把 mcp 工具注入 TOOLS"""
    import agent
    from tools_schema import TOOLS

    # 模拟 start_all_servers 返回成功
    fake_started = {"demo": 2}
    fake_errors = []
    fake_schemas = [
        {"type": "function", "function": {
            "name": "mcp__demo__echo",
            "description": "[MCP/demo] echo",
            "parameters": {"type": "object", "properties": {}}}},
        {"type": "function", "function": {
            "name": "mcp__demo__add",
            "description": "[MCP/demo] add",
            "parameters": {"type": "object", "properties": {}}}},
    ]
    monkeypatch.setattr("mcp_client.start_all_servers",
                        lambda *a, **kw: (fake_started, fake_errors))
    monkeypatch.setattr("mcp_client.discover_tools_as_schemas",
                        lambda: fake_schemas)

    # 记录初始 TOOLS 长度
    before_count = len(TOOLS)
    before_mcp = [t for t in TOOLS if t["function"]["name"].startswith("mcp__")]
    try:
        result = agent.init_mcp(verbose=False)
        assert result["started"] == fake_started
        # TOOLS 现在应含 mcp__demo__echo / add
        names_after = {t["function"]["name"] for t in TOOLS}
        assert "mcp__demo__echo" in names_after
        assert "mcp__demo__add" in names_after
    finally:
        # 清理：去掉测试注入的 mcp 工具
        TOOLS[:] = [t for t in TOOLS
                    if not t["function"]["name"].startswith("mcp__")]
        # 复原原有的 mcp 工具（一般是空）
        TOOLS.extend(before_mcp)


def test_agent_init_mcp_replaces_old_mcp_tools(monkeypatch):
    """重复调 init_mcp 应替换旧 mcp 工具，不应累积"""
    import agent
    from tools_schema import TOOLS

    # 第一次：注入 v1
    monkeypatch.setattr("mcp_client.start_all_servers",
                        lambda *a, **kw: ({"demo": 1}, []))
    monkeypatch.setattr("mcp_client.discover_tools_as_schemas", lambda: [
        {"type": "function", "function": {
            "name": "mcp__demo__v1",
            "description": "v1", "parameters": {"type": "object"}}},
    ])
    try:
        agent.init_mcp(verbose=False)
        assert any(t["function"]["name"] == "mcp__demo__v1" for t in TOOLS)

        # 第二次：注入 v2，v1 应被移除
        monkeypatch.setattr("mcp_client.discover_tools_as_schemas", lambda: [
            {"type": "function", "function": {
                "name": "mcp__demo__v2",
                "description": "v2", "parameters": {"type": "object"}}},
        ])
        agent.init_mcp(verbose=False)
        names = {t["function"]["name"] for t in TOOLS}
        assert "mcp__demo__v2" in names
        assert "mcp__demo__v1" not in names, "旧 mcp 工具应被替换"
    finally:
        TOOLS[:] = [t for t in TOOLS
                    if not t["function"]["name"].startswith("mcp__")]


# ---------- 异常路径 ----------

def test_mcpserver_invalid_command_raises():
    """不存在的 command → start() 抛 RuntimeError"""
    srv = mcp_client.MCPServer(name="bad", command="this_command_definitely_does_not_exist_xyz",
                                args=[])
    try:
        srv.start(init_timeout=1)
        assert False, "应抛"
    except RuntimeError:
        pass


def test_mcpserver_init_validates_required():
    """缺 name 或 command → 构造时报错"""
    try:
        mcp_client.MCPServer(name="", command="x")
        assert False
    except ValueError:
        pass
    try:
        mcp_client.MCPServer(name="x", command="")
        assert False
    except ValueError:
        pass


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
