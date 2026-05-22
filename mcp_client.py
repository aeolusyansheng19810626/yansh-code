"""MCP (Model Context Protocol) client（P2 #10 最小版）

约定：
  - 每个 MCP server 是一个独立子进程，通过 stdin/stdout 跑 newline-delimited JSON-RPC 2.0
  - 配置文件 mcp.json 跟 Claude Code 兼容：
      {"mcpServers": {"<name>": {"command": "...", "args": [...], "env": {...}, "cwd": "..."}}}
    路径优先级：<workspace>/.yansh/mcp.json → ~/.yansh/mcp.json
  - LLM 看到的工具名：mcp__<server>__<tool>，调用时 _dispatch_tool_call 拆前缀转发到对应 server

不做（留待下一波）：
  - HTTP/SSE 传输（最主流的 MCP 用 stdio，本波先打通这条）
  - 资源（resources） / 提示模板（prompts） —— 只做 tools
  - 自动重连 / 心跳：server 崩了直接报错，下一轮重启 yansh 解决
  - prompt injection 防护：第三方 server 返回的内容直接进 LLM context；
    用户责任在配置 mcp.json 时只接信任的 server
"""
from __future__ import annotations

import json
import os
import subprocess
import sys as _sys
import threading
from pathlib import Path
from typing import Optional


# ---------- 协议常量 ----------
_PROTOCOL_VERSION = "2024-11-05"
_INIT_TIMEOUT_SEC = 15
_CALL_TIMEOUT_SEC = 60


class MCPServer:
    """一个 MCP server 的本地 stdio 客户端。

    协议序列：
      1) Popen 子进程
      2) initialize → server 返回 capabilities
      3) notifications/initialized 通知 server 准备就绪
      4) tools/list 拉工具清单
      5) （随后）tools/call 调用工具
      6) shutdown 时关 stdin → terminate
    """

    def __init__(self, name: str, command: str,
                 args: Optional[list] = None,
                 env: Optional[dict] = None,
                 cwd: Optional[str] = None):
        if not name or not command:
            raise ValueError(f"MCPServer 需要 name 和 command（实际 name={name!r}, cmd={command!r}）")
        self.name = name
        self.command = command
        self.args = list(args or [])
        self.env = dict(env or {})
        self.cwd = cwd
        self.proc: Optional[subprocess.Popen] = None
        self.tools: list = []
        self._next_id = 1
        # mid -> (Event, holder dict)
        self._pending: dict = {}
        self._pending_lock = threading.Lock()
        self._writer_lock = threading.Lock()
        self._id_lock = threading.Lock()
        self._reader_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._initialized = False
        self.stderr_buffer: list = []   # 最近的 stderr 行，用于报错诊断

    # ---------- 生命周期 ----------

    def start(self, init_timeout: float = _INIT_TIMEOUT_SEC) -> None:
        """spawn server + initialize 握手 + tools/list"""
        if self.proc is not None:
            raise RuntimeError(f"server {self.name} 已启动")
        full_env = {**os.environ, **self.env}
        # P1 安全：开新进程组，shutdown 时能 kill 整棵进程树。
        # 否则 npx → node → mcp-server.js 这条链上的孙进程会变孤儿。
        popen_kwargs = dict(
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=full_env,
            cwd=self.cwd,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,   # line-buffered
        )
        if _sys.platform == "win32":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True
        try:
            self.proc = subprocess.Popen([self.command, *self.args], **popen_kwargs)
        except FileNotFoundError as e:
            raise RuntimeError(f"找不到命令 {self.command!r}：{e}")
        # reader 线程读 stdout（JSON-RPC 响应）
        self._reader_thread = threading.Thread(
            target=self._reader_loop, daemon=True,
            name=f"mcp-reader-{self.name}",
        )
        self._reader_thread.start()
        # stderr 线程读错误日志（保留最近 50 行用于诊断）
        self._stderr_thread = threading.Thread(
            target=self._stderr_loop, daemon=True,
            name=f"mcp-stderr-{self.name}",
        )
        self._stderr_thread.start()

        try:
            self._do_initialize(init_timeout)
            self._fetch_tools(init_timeout)
        except Exception:
            self.shutdown()
            raise

    def shutdown(self) -> None:
        """关掉子进程 + 整棵进程树（孙进程也杀）。失败也尽量清理（不抛异常）。

        P1 安全：仅 self.proc.kill() 只杀直接子进程，对 shell 包装 / npx 启动的
        node 子进程会泄漏成孤儿。这里按平台杀整棵进程组。
        """
        if self.proc is None:
            return
        proc = self.proc
        try:
            # 关 stdin → 给 server 退出信号
            if proc.stdin and not proc.stdin.closed:
                try:
                    proc.stdin.close()
                except Exception:
                    pass
            # 直接 kill 整棵进程树——必须趁父进程还活着、还能被 psutil 枚举到
            # 时一次性枚举出所有后代再 kill。先 graceful 让父退会丢失孙的访问路径。
            if proc.poll() is None:
                self._kill_tree(proc)
            try:
                proc.wait(timeout=2)
            except Exception:
                pass
        except Exception:
            pass
        self.proc = None

    @staticmethod
    def _kill_tree(proc: subprocess.Popen) -> None:
        """跨平台杀进程树：Windows 优先 psutil（最可靠），fallback taskkill；POSIX killpg。

        Windows 单 Popen + CREATE_NEW_PROCESS_GROUP 不构成 Job Object——taskkill /T
        只能找到通过 cmd.exe 派生的孙进程，对 Popen → Popen 链失效。psutil 走
        children(recursive=True) 是真"看见全树"的方案。
        """
        pid = proc.pid
        # 路径 1: psutil（跨平台最可靠）
        try:
            import psutil as _psutil
            try:
                root = _psutil.Process(pid)
                victims = root.children(recursive=True) + [root]
                for p in victims:
                    try:
                        p.kill()
                    except _psutil.NoSuchProcess:
                        pass
                _psutil.wait_procs(victims, timeout=2)
                return
            except _psutil.NoSuchProcess:
                # 父已死——孤儿孙没法通过父枚举找到了，只能尽力
                return
        except ImportError:
            pass

        # 路径 2: 平台原生（无 psutil 时兜底）
        try:
            if _sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    capture_output=True, timeout=3,
                )
            else:
                import signal as _signal
                os.killpg(os.getpgid(pid), _signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    # ---------- JSON-RPC 收发 ----------

    def _next_request_id(self) -> int:
        with self._id_lock:
            mid = self._next_id
            self._next_id += 1
        return mid

    def _send_line(self, obj: dict) -> None:
        """序列化为单行 JSON 写入 stdin。线程安全（writer_lock）"""
        if self.proc is None or self.proc.stdin is None:
            raise RuntimeError(f"server {self.name} 未启动或 stdin 已关")
        line = json.dumps(obj, ensure_ascii=False) + "\n"
        with self._writer_lock:
            try:
                self.proc.stdin.write(line)
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                raise RuntimeError(f"server {self.name} stdin 写入失败：{e}")

    def _request(self, method: str, params: Optional[dict] = None,
                 timeout: float = _CALL_TIMEOUT_SEC) -> dict:
        """发请求并阻塞等响应。超时抛 TimeoutError，server 错误抛 RuntimeError。"""
        mid = self._next_request_id()
        ev = threading.Event()
        holder: dict = {}
        with self._pending_lock:
            self._pending[mid] = (ev, holder)
        try:
            msg = {"jsonrpc": "2.0", "id": mid, "method": method}
            if params is not None:
                msg["params"] = params
            self._send_line(msg)
            if not ev.wait(timeout):
                raise TimeoutError(
                    f"MCP server {self.name} 方法 {method} 超时 {timeout}s"
                )
            resp = holder.get("msg", {})
            if "error" in resp:
                err = resp["error"]
                raise RuntimeError(
                    f"MCP server {self.name} {method} 报错："
                    f"code={err.get('code')} msg={err.get('message')}"
                )
            return resp.get("result", {})
        finally:
            with self._pending_lock:
                self._pending.pop(mid, None)

    def _notify(self, method: str, params: Optional[dict] = None) -> None:
        """发通知（无响应）"""
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._send_line(msg)

    def _reader_loop(self) -> None:
        """后台读 stdout，解析 JSON 后按 id 分发到 _pending。

        P1 重要：server 崩溃时 stdout 关闭 → for 循环退出 → 所有 _pending 死等。
        finally 里把 pending 全部 set 一个错误响应，避免 _request 死等到 60s timeout。
        """
        proc = self.proc
        try:
            if proc is None or proc.stdout is None:
                return
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue   # 不是合法 JSON 就忽略（server 可能输出诊断信息）
                mid = msg.get("id")
                if mid is None:
                    # 服务端发起的通知/请求——本最小版暂不处理
                    continue
                with self._pending_lock:
                    slot = self._pending.get(mid)
                if slot is not None:
                    ev, holder = slot
                    holder["msg"] = msg
                    ev.set()
        finally:
            # server 死了——把所有 pending 全部唤醒，写一个错误响应。
            # 否则 _request 会死等到 _CALL_TIMEOUT_SEC（60s）才返回。
            stderr_tail = list(self.stderr_buffer[-3:]) if self.stderr_buffer else []
            with self._pending_lock:
                for mid, (ev, holder) in self._pending.items():
                    if "msg" not in holder:
                        holder["msg"] = {
                            "id": mid,
                            "error": {
                                "code": -32000,
                                "message": (
                                    f"server {self.name} stdout 已关闭"
                                    + (f"（stderr 末尾：{stderr_tail}）" if stderr_tail else "")
                                ),
                            },
                        }
                    ev.set()

    def _stderr_loop(self) -> None:
        """收集 server 的 stderr（用于失败诊断）"""
        proc = self.proc
        if proc is None or proc.stderr is None:
            return
        for line in proc.stderr:
            line = line.rstrip("\n")
            self.stderr_buffer.append(line)
            if len(self.stderr_buffer) > 50:
                self.stderr_buffer.pop(0)

    # ---------- MCP 协议方法 ----------

    def _do_initialize(self, timeout: float) -> None:
        result = self._request("initialize", {
            "protocolVersion": _PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "yansh", "version": "0.1"},
        }, timeout=timeout)
        # 协议要求 client 在 initialize 响应后发 initialized 通知
        self._notify("notifications/initialized")
        self._initialized = True
        # result 里有 server 的 capabilities/serverInfo——本最小版不存

    def _fetch_tools(self, timeout: float) -> None:
        result = self._request("tools/list", timeout=timeout)
        raw = result.get("tools", [])
        self.tools = [t for t in raw if isinstance(t, dict) and "name" in t]

    def call_tool(self, tool_name: str, arguments: Optional[dict] = None,
                  timeout: float = _CALL_TIMEOUT_SEC) -> dict:
        """调用 server 上的某个工具。返回原始 result（含 content / isError）。"""
        if not self._initialized:
            raise RuntimeError(f"server {self.name} 未完成 initialize")
        return self._request("tools/call", {
            "name": tool_name,
            "arguments": arguments or {},
        }, timeout=timeout)


# ---------- 模块级 server registry ----------

_servers: dict = {}        # name -> MCPServer
_servers_lock = threading.Lock()


def _config_paths(workspace_dir: Optional[str] = None) -> list:
    """配置文件查找路径（项目级优先；调用方需自行过滤未 trust 的项目级路径）"""
    paths = []
    if workspace_dir:
        paths.append(Path(workspace_dir) / ".yansh" / "mcp.json")
    paths.append(Path.home() / ".yansh" / "mcp.json")
    return paths


def load_config(workspace_dir: Optional[str] = None) -> dict:
    """加载 mcp.json（项目级 trust 通过才用项目级；否则 fallback 到 ~/.yansh）。

    P0 安全：默认拒绝项目级配置（防恶意 repo 通过 .yansh/mcp.json 启动任意进程）；
    用户在交互模式下首次见时 trust 后才允许，详见 workspace_trust 模块。
    """
    import workspace_trust as _wt
    home_path = Path.home() / ".yansh" / "mcp.json"
    if workspace_dir:
        proj_path = Path(workspace_dir) / ".yansh" / "mcp.json"
        if proj_path.exists() and _wt.check_or_prompt(workspace_dir, "mcp.json"):
            try:
                return json.loads(proj_path.read_text(encoding="utf-8"))
            except Exception as e:
                return {"_error": f"解析失败 {proj_path}: {e}"}
    if home_path.exists():
        try:
            return json.loads(home_path.read_text(encoding="utf-8"))
        except Exception as e:
            return {"_error": f"解析失败 {home_path}: {e}"}
    return {}


def start_all_servers(workspace_dir: Optional[str] = None,
                      verbose: bool = True) -> tuple:
    """启动配置里的全部 server。返回 (started, errors)：
    - started: dict {name: tool_count}
    - errors: list of (name, error_msg)
    """
    cfg = load_config(workspace_dir)
    if "_error" in cfg:
        return {}, [("config", cfg["_error"])]
    servers_cfg = cfg.get("mcpServers", {})
    started: dict = {}
    errors: list = []
    for name, sc in servers_cfg.items():
        if not isinstance(sc, dict):
            errors.append((name, f"非法配置（不是对象）: {sc!r}"))
            continue
        cmd = sc.get("command")
        if not cmd:
            errors.append((name, "缺 command 字段"))
            continue
        try:
            srv = MCPServer(
                name=name, command=cmd,
                args=sc.get("args"), env=sc.get("env"),
                cwd=sc.get("cwd"),
            )
            srv.start()
            with _servers_lock:
                _servers[name] = srv
            started[name] = len(srv.tools)
        except Exception as e:
            errors.append((name, str(e)))
    return started, errors


def get_servers() -> dict:
    """当前已启动 server 的浅拷贝，给 /mcp 命令用"""
    with _servers_lock:
        return dict(_servers)


def discover_tools_as_schemas() -> list:
    """聚合所有 server 的工具，转成 yansh TOOLS 兼容 schema 格式。
    工具名加前缀：mcp__<server>__<tool>
    """
    out = []
    with _servers_lock:
        servers_snapshot = list(_servers.items())
    for name, srv in servers_snapshot:
        for t in srv.tools:
            tool_name = t.get("name")
            if not tool_name:
                continue
            prefixed = f"mcp__{name}__{tool_name}"
            schema = t.get("inputSchema") or {"type": "object", "properties": {}}
            desc = t.get("description") or ""
            out.append({
                "type": "function",
                "function": {
                    "name": prefixed,
                    "description": f"[MCP/{name}] {desc}",
                    "parameters": schema,
                },
            })
    return out


def parse_prefixed(prefixed_name: str) -> Optional[tuple]:
    """拆 mcp__<server>__<tool> → (server, tool)。失败返回 None。"""
    if not prefixed_name.startswith("mcp__"):
        return None
    rest = prefixed_name[len("mcp__"):]
    sep = rest.find("__")
    if sep < 0:
        return None
    return rest[:sep], rest[sep + 2:]


def call_tool(prefixed_name: str, arguments: Optional[dict] = None,
              timeout: float = _CALL_TIMEOUT_SEC) -> dict:
    """LLM 调 mcp__server__tool 时的入口。返回 yansh 友好的 result：
    - 成功：{"content": "<合并后的 text>", "isError": bool, "raw": <MCP 原始>}
    - 失败：{"error": "<原因>"}
    """
    parsed = parse_prefixed(prefixed_name)
    if parsed is None:
        return {"error": f"非法 MCP 工具名: {prefixed_name}"}
    server_name, tool_name = parsed
    with _servers_lock:
        srv = _servers.get(server_name)
    if srv is None:
        return {"error": f"MCP server 未启动: {server_name}"}
    if not srv.is_alive():
        return {"error": f"MCP server {server_name} 已退出（stderr 末尾：{srv.stderr_buffer[-3:]}）"}
    try:
        result = srv.call_tool(tool_name, arguments, timeout=timeout)
    except Exception as e:
        return {"error": f"MCP 调用失败: {e}"}
    # 把 content[] 合成一段 text，方便 LLM 看
    content = result.get("content", [])
    if isinstance(content, list):
        texts = []
        for c in content:
            if isinstance(c, dict):
                ctype = c.get("type")
                if ctype == "text":
                    texts.append(c.get("text", ""))
                else:
                    # 非 text 类型（image/resource）原样转 JSON 表示
                    texts.append(json.dumps(c, ensure_ascii=False))
        return {
            "content": "\n".join(texts),
            "isError": bool(result.get("isError", False)),
        }
    # 不是预期形态，原样返回
    return {"content": json.dumps(result, ensure_ascii=False), "isError": False}


def shutdown_all() -> None:
    """关闭所有 server。atexit 钩子用。"""
    with _servers_lock:
        names = list(_servers.keys())
    for name in names:
        srv = _servers.get(name)
        if srv is not None:
            try:
                srv.shutdown()
            except Exception:
                pass
    with _servers_lock:
        _servers.clear()
