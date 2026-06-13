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


# ---------- P1-1：reader_loop EOF 唤醒 pending ----------

def _make_silent_server():
    """构造一个 MCPServer + silent mock proc：永不响应。
    用来测 EOF 唤醒——请求发出后一直 pending。"""
    def silent_logic(req):
        # 只响应 initialize 让握手过，其他全静默
        method = req.get("method")
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": req.get("id"),
                    "result": {"protocolVersion": "2024-11-05", "capabilities": {}}}
        if method == "notifications/initialized":
            return None
        return None  # 静默——请求永远 pending
    srv = mcp_client.MCPServer(name="silent", command="dummy")
    srv.proc = _MockProc(silent_logic)
    srv._reader_thread = threading.Thread(target=srv._reader_loop, daemon=True)
    srv._reader_thread.start()
    srv._stderr_thread = threading.Thread(target=srv._stderr_loop, daemon=True)
    srv._stderr_thread.start()
    return srv


def test_reader_loop_eof_wakes_pending_request():
    """server 突然崩溃（stdout 关闭）→ 等待中的 _request 应在 1s 内
    拿到错误响应，而不是死等到 _CALL_TIMEOUT_SEC（60s）。

    P1 安全：之前 reader 的 for 循环退出后 pending 永不被 set →
    _request 死等 60s timeout，LLM 看到的是"超时"而非"server 挂了"。
    """
    import time

    srv = _make_silent_server()
    try:
        srv._do_initialize(timeout=2)
        # 不调 _fetch_tools——silent server 不会响应 tools/list
        srv._initialized = True

        result_box = {}
        def call_in_bg():
            try:
                srv._request("tools/list", timeout=10)
                result_box["ok"] = True
            except Exception as e:
                result_box["err"] = e

        t = threading.Thread(target=call_in_bg, daemon=True)
        t.start()
        time.sleep(0.1)   # 让 _request 进入 pending 状态

        # 模拟 server 崩溃 —— 关 stdout 写端 → reader 的 for 循环退出
        try:
            srv.proc._stdout_write.close()
        except Exception:
            pass

        t0 = time.time()
        t.join(timeout=3)
        elapsed = time.time() - t0

        assert not t.is_alive(), "_request 没被唤醒——还在死等"
        assert elapsed < 2.0, f"唤醒太慢：{elapsed:.2f}s（应 < 2s，否则等同 60s 死等）"
        assert "err" in result_box, "应拿到错误响应"
        err_msg = str(result_box["err"])
        assert "stdout" in err_msg or "已关闭" in err_msg, \
            f"错误信息应提示 stdout 关闭，实际：{err_msg}"
    finally:
        srv.shutdown()


def test_reader_loop_eof_wakes_multiple_pending():
    """多个 pending 同时存在时 EOF → 全部都被唤醒（不是只唤醒一个）"""
    import time

    srv = _make_silent_server()
    try:
        srv._do_initialize(timeout=2)
        srv._initialized = True

        results = []
        results_lock = threading.Lock()

        def call_one():
            try:
                srv._request("tools/list", timeout=10)
                with results_lock:
                    results.append("ok")
            except Exception as e:
                with results_lock:
                    results.append(f"err:{type(e).__name__}")

        N = 5
        threads = [threading.Thread(target=call_one, daemon=True) for _ in range(N)]
        for t in threads:
            t.start()
        time.sleep(0.1)  # 让所有 _request 进入 pending

        # server 崩溃
        try:
            srv.proc._stdout_write.close()
        except Exception:
            pass

        t0 = time.time()
        for t in threads:
            t.join(timeout=3)
        elapsed = time.time() - t0

        assert all(not t.is_alive() for t in threads), \
            f"还有 pending 没被唤醒（{N} 个里 {sum(t.is_alive() for t in threads)} 个仍 alive）"
        assert elapsed < 2.0, f"唤醒太慢：{elapsed:.2f}s"
        assert len(results) == N
        assert all(r.startswith("err:") for r in results), \
            f"应全部拿到 error，实际 {results}"
    finally:
        srv.shutdown()


# ---------- P1-1：shutdown 杀进程树 ----------

def test_shutdown_kills_real_subprocess_tree(tmp_path):
    """端到端：start 真实 Python 子进程（spawn 一个孙进程让其 sleep 60s），
    shutdown 后用 psutil 验证孙进程也被杀（不是只杀直接子进程）。

    跳过条件：psutil 没装时 skip——但 yansh 测试集已用，预期可用。
    """
    try:
        import psutil
    except ImportError:
        import pytest
        pytest.skip("需要 psutil 来验证进程树死亡")

    import time

    # 父子结构：python -c "spawn child sleep 60; sleep 60"
    import sys as _sys
    import subprocess
    py = _sys.executable
    # 父进程：fork 一个 sleep 子进程，自己也 sleep（不接 stdio 协议，纯进程树测试）
    parent_code = (
        "import subprocess, sys, time;"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']);"
        "print(p.pid, flush=True);"
        "time.sleep(60)"
    )

    srv = mcp_client.MCPServer(name="proctree-test", command=py, args=["-c", parent_code])
    # 不走 init 流程（这个进程不说 JSON-RPC）；只测进程树管理
    full_env = {**os.environ}
    popen_kwargs = dict(
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=full_env, text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
    import sys as _sys2
    if _sys2.platform == "win32":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True
    srv.proc = subprocess.Popen([py, "-c", parent_code], **popen_kwargs)

    # 读父进程 stdout 拿到孙进程 pid
    grandchild_pid_line = srv.proc.stdout.readline().strip()
    grandchild_pid = int(grandchild_pid_line)
    parent_pid = srv.proc.pid

    # 给点时间让孙进程稳定
    time.sleep(0.3)
    assert psutil.pid_exists(parent_pid), "父进程没启动？"
    assert psutil.pid_exists(grandchild_pid), "孙进程没起来？"

    # shutdown
    srv.shutdown()

    # 给系统点时间清理
    time.sleep(0.5)
    parent_alive = psutil.pid_exists(parent_pid)
    grand_alive = psutil.pid_exists(grandchild_pid)

    # 兜底再杀一次（避免测试失败时孙进程残留 60s）
    if grand_alive:
        try:
            psutil.Process(grandchild_pid).kill()
        except Exception:
            pass
    if parent_alive:
        try:
            psutil.Process(parent_pid).kill()
        except Exception:
            pass

    assert not parent_alive, f"父进程 {parent_pid} 没被杀"
    assert not grand_alive, f"孙进程 {grandchild_pid} 没被杀（进程树 kill 失败）"


# ---------- is_mcp_write / productive 判定 ----------

from subagent import is_mcp_write


def test_is_mcp_write_recognizes_write_tools():
    assert is_mcp_write("mcp__filesystem__edit_file",   {"content": "ok", "isError": False})
    assert is_mcp_write("mcp__filesystem__write_file",  {"content": "", "isError": False})
    assert is_mcp_write("mcp__fs__move_file",           {"content": "", "isError": False})
    assert is_mcp_write("mcp__filesystem__create_directory", {"content": "", "isError": False})
    assert is_mcp_write("mcp__filesystem__delete_file", {"content": "", "isError": False})


def test_is_mcp_write_rejects_read_tools():
    assert not is_mcp_write("mcp__filesystem__read_file",          {"content": "data"})
    assert not is_mcp_write("mcp__filesystem__list_directory",     {"content": "[]"})
    assert not is_mcp_write("mcp__filesystem__directory_tree",     {"content": "{}"})
    assert not is_mcp_write("mcp__filesystem__list_allowed_directories", {"content": "[]"})


def test_is_mcp_write_rejects_failed_calls():
    # isError=True → 写失败，不计 productive
    assert not is_mcp_write("mcp__filesystem__edit_file", {"isError": True})
    # error 字段 → 失败
    assert not is_mcp_write("mcp__filesystem__write_file", {"error": "server down"})
    # None result → 失败
    assert not is_mcp_write("mcp__filesystem__edit_file", None)
    # 空 dict → 无成功证据
    assert not is_mcp_write("mcp__filesystem__edit_file", {})


def test_is_mcp_write_rejects_non_mcp_tools():
    assert not is_mcp_write("write_file",       {"success": True})
    assert not is_mcp_write("replace_in_file",  {"success": True})
    assert not is_mcp_write("read_file",        {"content": "x"})
    assert not is_mcp_write("edit_file",        {"content": "x"})  # 无 mcp__ 前缀


def test_is_mcp_write_default_is_error_absent():
    # MCP 成功结果有时不带 isError 字段，缺省按成功处理
    assert is_mcp_write("mcp__filesystem__write_file", {"content": "ok"})
    assert is_mcp_write("mcp__filesystem__edit_file",  {"content": ""})


def test_is_mcp_write_server_name_not_polluted():
    # server 名含写语义词时，只读工具不应被误判
    assert not is_mcp_write("mcp__editor__read_file",       {"content": "data"})
    assert not is_mcp_write("mcp__write_service__list_dir", {"content": "[]"})
    # server 名含写词但 tool 段是读操作 → False
    assert not is_mcp_write("mcp__edit_server__get_file",   {"content": "data"})


# ---------- snapshot_files_modified 与 MCP 写工具集成 ----------

import task_log as _tl
import unittest.mock as _mock


def _make_fake_tool_call(name, args):
    import json as _json
    tc = _mock.MagicMock()
    tc.function.name = name
    tc.function.arguments = _json.dumps(args)
    tc.id = "fake-id"
    return tc


def _reset_files_modified():
    """测试用：清空 task_log 的文件修改记录。"""
    _tl._task_files_modified.clear()


def _dispatch_mcp(name, args, result):
    """调用 _dispatch_tool_call 中的 MCP 分支，返回 out dict。"""
    import agent as _agent
    tool_call = _make_fake_tool_call(name, args)
    with _mock.patch("mcp_client.call_tool", return_value=result):
        out = _agent._dispatch_tool_call(
            tool_call, mode="auto", snap=None,
            allow_hil=False, allow_confirm=False,
        )
    return out


def test_mcp_write_success_records_file(tmp_path):
    """MCP 写工具成功 → path 参数被登记到 snapshot_files_modified()"""
    _reset_files_modified()
    _dispatch_mcp(
        "mcp__filesystem__write_file",
        {"path": "project/core/processor.py"},
        {"content": "ok", "isError": False},
    )
    assert "project/core/processor.py" in _tl.snapshot_files_modified()


def test_mcp_move_records_both_ends(tmp_path):
    """MCP move_file → source + destination 都登记"""
    _reset_files_modified()
    _dispatch_mcp(
        "mcp__filesystem__move_file",
        {"source": "old.py", "destination": "new.py"},
        {"content": "", "isError": False},
    )
    modified = _tl.snapshot_files_modified()
    assert "old.py" in modified
    assert "new.py" in modified


def test_mcp_write_failure_not_recorded():
    """MCP 写失败（isError=True / error 字段）→ 不登记"""
    _reset_files_modified()
    _dispatch_mcp(
        "mcp__filesystem__edit_file",
        {"path": "foo.py"},
        {"isError": True},
    )
    assert _tl.snapshot_files_modified() == []

    _reset_files_modified()
    _dispatch_mcp(
        "mcp__filesystem__edit_file",
        {"path": "bar.py"},
        {"error": "server down"},
    )
    assert _tl.snapshot_files_modified() == []


def test_mcp_read_not_recorded():
    """MCP 只读工具成功 → 不登记"""
    _reset_files_modified()
    _dispatch_mcp(
        "mcp__filesystem__read_file",
        {"path": "readme.py"},
        {"content": "data"},
    )
    assert _tl.snapshot_files_modified() == []


def test_mcp_non_filesystem_write_with_path_not_recorded():
    """P2 边界：非文件类写工具（hint 匹配但参数含 path）→ is_mcp_write 判定为写，
    但 path 值被登记到 modified（当前 best-effort 行为），测试记录实际行为作为回归基线。
    若未来要过滤非文件类 server，修改 is_mcp_write 后此测试需更新。"""
    _reset_files_modified()
    # mcp__db__insert_row: insert 在 hint 列表里，path 参数存在
    # 当前实现会登记（best-effort），此测试记录该行为
    _dispatch_mcp(
        "mcp__db__insert_row",
        {"path": "table/users", "data": "{}"},
        {"content": "1 row inserted", "isError": False},
    )
    # 当前行为：path 参数被登记（非阻塞，记为已知 best-effort 行为）
    assert "table/users" in _tl.snapshot_files_modified()


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
