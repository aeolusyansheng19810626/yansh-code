import json
import sys
import shutil
import threading
import difflib
import time as _time
from datetime import datetime
from openai import OpenAI
from rich.console import Console
from pathlib import Path
from config import OPENROUTER_API_KEY, OPENROUTER_BASE_URL, QUALITY_CASCADE, WORKSPACE_DIR, get_config
from tools import write_file, read_file, execute_command, list_files, replace_in_file, get_symbol_definition, search_in_files, move_file, apply_patch, list_symbols, replace_symbol
import interrupt
import tools as _tools_mod

console = Console()

# #40 批处理模式标志
_BATCH_MODE = False
_last_task_log: dict = {}  # 最近一次任务日志，供 --json 输出

# #27 项目类型（由 main.py 调用 detect_project_type() 后写入）
_PROJECT_TYPE = None
_PROJECT_TEST_CMD = None

# #37 快照 / #38 日志 目录
_YANSH_DIR     = Path(WORKSPACE_DIR) / ".yansh"
_SNAPSHOT_DIR  = _YANSH_DIR / "snapshots"
_LOG_DIR       = _YANSH_DIR / "logs"

# #38 当前任务日志状态（模块级，_run() 期间填充）
_current_task_log: dict = {}
_task_tool_calls: list  = []
_task_files_modified: list = []

# 对话历史管理
conversation_history = []
MAX_HISTORY = 20
CHAT_CONTEXT_ROUNDS = 5
COMPRESS_MODEL = "meta-llama/llama-3.1-8b-instant"


def _cfg(key):
    """读取生效配置值"""
    return get_config().get(key)


def set_batch_mode(enabled: bool, json_output: bool = False):
    """设置批处理模式；json_output=True 时将 console 重定向到 stderr"""
    global _BATCH_MODE, console
    _BATCH_MODE = enabled
    _tools_mod.set_batch_mode(enabled)
    if json_output:
        console = Console(file=sys.stderr)


def get_last_task_log() -> dict:
    return dict(_last_task_log)


def _prompt(msg: str, default: str = "y") -> str:
    """批处理模式自动返回 default；交互模式调用 console.input"""
    if _BATCH_MODE:
        console.print(f"{msg}[batch: {default}]", highlight=False)
        return default
    try:
        return console.input(msg).strip().lower()
    except EOFError:
        return default

# 初始化OpenAI客户端（兼容OpenRouter）
client = OpenAI(
    api_key=OPENROUTER_API_KEY,
    base_url=OPENROUTER_BASE_URL,
)

_HISTORY_FILE = Path(WORKSPACE_DIR) / ".yansh_history.json"

def save_history():
    """将 conversation_history 序列化到文件"""
    try:
        _HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        _HISTORY_FILE.write_text(json.dumps(conversation_history, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass

def load_history():
    """从文件加载历史，返回轮数（0 表示未加载）"""
    global conversation_history
    if not _HISTORY_FILE.exists():
        return 0
    try:
        data = json.loads(_HISTORY_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            conversation_history = data
            return len(data) // 2
    except Exception:
        pass
    return 0

def add_to_history(user_msg, assistant_msg):
    """添加对话到历史，超过最大长度时删除最早的"""
    global conversation_history
    conversation_history.append({"role": "user", "content": user_msg})
    conversation_history.append({"role": "assistant", "content": assistant_msg})

    # 保持历史在最大长度内
    while len(conversation_history) > MAX_HISTORY * 2:
        conversation_history.pop(0)
        conversation_history.pop(0)

    save_history()

def get_recent_history(rounds=CHAT_CONTEXT_ROUNDS):
    """获取最近N轮对话历史"""
    return conversation_history[-(rounds * 2):] if conversation_history else []

def maybe_compress_history():
    """历史字符数超过阈值时，压缩旧轮次，保留最近 keep_recent_turns 轮原文"""
    global conversation_history
    compress_threshold = _cfg("compress_threshold") or 6000
    keep_recent_turns  = _cfg("keep_recent_turns")  or 3
    total_chars = sum(len(m["content"]) for m in conversation_history)
    if total_chars <= compress_threshold:
        return

    keep_count = keep_recent_turns * 2
    if len(conversation_history) <= keep_count:
        return

    old_msgs = conversation_history[:-keep_count]
    recent_msgs = conversation_history[-keep_count:]

    history_text = "\n".join(f"[{m['role']}]: {m['content']}" for m in old_msgs)
    prompt = (
        f"请将以下对话历史压缩成摘要：\n\n{history_text}\n\n"
        "严格按以下格式输出，不添加其他内容：\n"
        '【已完成任务】\n- ...\n\n【关键文件】\n- ...\n\n【未解决问题】\n- ...（没有则写"无"）'
    )
    try:
        response = client.chat.completions.create(
            model=COMPRESS_MODEL,
            messages=[{"role": "user", "content": prompt}]
        )
        summary = response.choices[0].message.content
    except Exception as e:
        console.print(f"[警告] 上下文压缩失败: {e}", highlight=False)
        return

    conversation_history = [{"role": "assistant", "content": summary}] + recent_msgs
    console.print("[上下文已自动压缩]", highlight=False)

def compress_history():
    """手动压缩：复用同一逻辑"""
    global conversation_history
    compress_threshold = _cfg("compress_threshold") or 6000
    keep_recent_turns  = _cfg("keep_recent_turns")  or 3
    total_chars = sum(len(m["content"]) for m in conversation_history)
    keep_count = keep_recent_turns * 2
    if total_chars <= compress_threshold or len(conversation_history) <= keep_count:
        console.print("[上下文较短，无需压缩]", highlight=False)
        return

    old_msgs = conversation_history[:-keep_count]
    recent_msgs = conversation_history[-keep_count:]
    history_text = "\n".join(f"[{m['role']}]: {m['content']}" for m in old_msgs)
    prompt = (
        f"请将以下对话历史压缩成摘要：\n\n{history_text}\n\n"
        "严格按以下格式输出，不添加其他内容：\n"
        '【已完成任务】\n- ...\n\n【关键文件】\n- ...\n\n【未解决问题】\n- ...（没有则写"无"）'
    )
    try:
        response = client.chat.completions.create(
            model=COMPRESS_MODEL,
            messages=[{"role": "user", "content": prompt}]
        )
        summary = response.choices[0].message.content
    except Exception as e:
        console.print(f"[警告] 上下文压缩失败: {e}", highlight=False)
        return
    conversation_history = [{"role": "assistant", "content": summary}] + recent_msgs
    console.print("[上下文已手动压缩]", highlight=False)

def show_context():
    """打印当前上下文大小"""
    compress_threshold = _cfg("compress_threshold") or 6000
    turns = len(conversation_history) // 2
    total_chars = sum(len(m["content"]) for m in conversation_history)
    hint = "  （建议压缩）" if total_chars > compress_threshold else ""
    console.print(f"[上下文状态] 共 {turns} 轮，总字符数：{total_chars} / {compress_threshold}{hint}", highlight=False)

def clear_history():
    """清空对话历史（内存 + 文件）"""
    global conversation_history
    conversation_history = []
    try:
        if _HISTORY_FILE.exists():
            _HISTORY_FILE.unlink()
    except Exception:
        pass
    console.print("[上下文已清除]", highlight=False)

# ---------- #37 快照 / 回滚 ----------

def create_snapshot(file_list):
    """备份 file_list 中在 workspace 已存在的文件，返回快照目录（无文件可备份时返回 None）"""
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    snap_dir = _SNAPSHOT_DIR / timestamp
    snap_dir.mkdir(parents=True, exist_ok=True)
    backed = []
    for filename in file_list:
        src = Path(WORKSPACE_DIR) / filename
        if src.exists() and src.is_file():
            dst = snap_dir / filename
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), str(dst))
            backed.append(filename)
    if not backed:
        snap_dir.rmdir()
        return None
    (snap_dir / "meta.json").write_text(
        json.dumps({"files": backed, "timestamp": timestamp}, ensure_ascii=False),
        encoding="utf-8"
    )
    return snap_dir

def restore_snapshot(snap_dir):
    """从快照目录恢复文件，返回恢复数量"""
    meta_file = snap_dir / "meta.json"
    if not meta_file.exists():
        return 0
    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    restored = 0
    for filename in meta.get("files", []):
        src = snap_dir / filename
        dst = Path(WORKSPACE_DIR) / filename
        if src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), str(dst))
            restored += 1
    return restored

def cleanup_snapshot(snap_dir):
    """删除快照目录"""
    if snap_dir and snap_dir.exists():
        shutil.rmtree(str(snap_dir))

def get_latest_snapshot():
    """返回最新快照目录，不存在返回 None"""
    if not _SNAPSHOT_DIR.exists():
        return None
    candidates = sorted(
        (s for s in _SNAPSHOT_DIR.iterdir() if s.is_dir() and (s / "meta.json").exists()),
        reverse=True
    )
    return candidates[0] if candidates else None

# ---------- #38 任务日志 ----------

def init_task_log(requirement, mode):
    global _current_task_log, _task_tool_calls, _task_files_modified
    _task_tool_calls = []
    _task_files_modified = []
    _current_task_log = {
        "timestamp": datetime.now().isoformat(),
        "requirement": requirement,
        "mode": mode,
        "model": QUALITY_CASCADE[0] if QUALITY_CASCADE else "unknown",
        "plan": [],
        "files_modified": [],
        "tool_calls": [],
        "test_command": "",
        "test_result": "unknown",
        "attempts": 0,
        "error": None,
        "duration_seconds": 0.0,
        "_start": _time.time(),
    }

def finish_task_log(success, attempts, test_result=None):
    global _current_task_log, _last_task_log
    if not _current_task_log:
        return
    _current_task_log["test_result"] = "pass" if success else "fail"
    _current_task_log["attempts"] = attempts
    _current_task_log["tool_calls"] = _task_tool_calls[:]
    _current_task_log["files_modified"] = list(dict.fromkeys(_task_files_modified))
    _current_task_log["duration_seconds"] = round(_time.time() - _current_task_log.pop("_start"), 2)
    if test_result and not success:
        err = test_result.get("stderr", "") or test_result.get("stdout", "")
        _current_task_log["error"] = err[:300]
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    (_LOG_DIR / f"{ts}.jsonl").write_text(
        json.dumps(_current_task_log, ensure_ascii=False), encoding="utf-8"
    )
    _last_task_log = dict(_current_task_log)
    _current_task_log = {}

def show_recent_logs():
    """打印最近 5 条日志摘要"""
    if not _LOG_DIR.exists():
        console.print("暂无日志", highlight=False)
        return
    logs = sorted(_LOG_DIR.glob("*.jsonl"), reverse=True)[:5]
    if not logs:
        console.print("暂无日志", highlight=False)
        return
    for f in logs:
        try:
            e = json.loads(f.read_text(encoding="utf-8"))
            ts  = e.get("timestamp", "")[:19]
            req = e.get("requirement", "")[:60]
            res = "✓" if e.get("test_result") == "pass" else "✗"
            dur = e.get("duration_seconds", 0)
            att = e.get("attempts", 0)
            console.print(f"{ts} | {res} | {dur}s | {att}次 | {req}", highlight=False)
        except Exception:
            continue

# ---------- #25 彩色 diff ----------

def _show_diff(filename, old_str, new_str):
    old_lines = old_str.splitlines(keepends=True)
    new_lines = new_str.splitlines(keepends=True)
    diff = list(difflib.unified_diff(
        old_lines, new_lines,
        fromfile=f"a/{filename}", tofile=f"b/{filename}", lineterm=""
    ))
    if not diff:
        return
    console.print(f"\n--- diff: {filename} ---", highlight=False)
    for line in diff:
        if line.startswith("---") or line.startswith("+++"):
            console.print(line, style="bold", highlight=False)
        elif line.startswith("-"):
            console.print(line, style="red", highlight=False)
        elif line.startswith("+"):
            console.print(line, style="green", highlight=False)
        elif line.startswith("@@"):
            console.print(line, style="cyan", highlight=False)
        else:
            console.print(line, highlight=False)

# ---------- #26 Linter ----------

def run_linter():
    """静默运行 ruff check，有错误返回结果 dict，否则返回 None"""
    import shutil
    if not shutil.which("ruff"):
        return None
    result = execute_command("ruff check .")
    if result.get("returncode", 0) == 0:
        return None
    return result

# ---------- #27 项目类型检测 ----------

def detect_project_type():
    """扫描 workspace 目录识别项目类型，返回 (type_str, test_cmd)"""
    from pathlib import Path
    from config import WORKSPACE_DIR
    ws = Path(WORKSPACE_DIR)
    if not ws.exists():
        return None, None
    all_names = {f.name for f in ws.rglob("*") if f.is_file()}
    if any(n in all_names for n in ("requirements.txt", "pyproject.toml")) or any(n.endswith(".py") for n in all_names):
        return "Python", "pytest"
    if "package.json" in all_names:
        return "Node.js", "npm test"
    if "go.mod" in all_names:
        return "Go", "go test ./..."
    if "Cargo.toml" in all_names:
        return "Rust", "cargo test"
    if "pom.xml" in all_names:
        return "Java/Maven", "mvn test"
    return None, None

# 定义可用工具
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "在workspace目录下写入文件",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "文件名（相对于workspace）"},
                    "content": {"type": "string", "description": "文件内容"}
                },
                "required": ["filename", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取workspace目录下的文件",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "文件名（相对于workspace）"}
                },
                "required": ["filename"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "execute_command",
            "description": "在workspace目录下执行命令（30秒超时）",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的命令"}
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "列出workspace目录下的所有文件",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "replace_in_file",
            "description": "在workspace文件中精确替换字符串。old_str必须唯一匹配，否则返回错误",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "文件名（相对于workspace）"},
                    "old_str": {"type": "string", "description": "要替换的旧字符串（必须唯一匹配）"},
                    "new_str": {"type": "string", "description": "替换后的新字符串"}
                },
                "required": ["filename", "old_str", "new_str"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_symbol_definition",
            "description": "用 AST 精确查找函数或类的定义，返回所在文件、起始行号、完整代码。比读整个文件更高效，适合定位某个函数/类时使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol_name": {"type": "string", "description": "要查找的函数名或类名"},
                    "file_path": {"type": "string", "description": "指定搜索文件（相对于workspace，可选），不填则搜索整个workspace"}
                },
                "required": ["symbol_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_in_files",
            "description": "在workspace内搜索匹配字符串，返回文件名、行号和匹配内容",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "搜索模式（字符串或正则）"},
                    "regex": {"type": "boolean", "description": "是否使用正则表达式匹配（默认false）"},
                    "extensions": {"type": "array", "items": {"type": "string"}, "description": "文件扩展名过滤列表，如 [\".py\", \".md\"]"}
                },
                "required": ["pattern"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "move_file",
            "description": "移动文件从src到dst（相对于workspace），自动创建目标父目录",
            "parameters": {
                "type": "object",
                "properties": {
                    "src": {"type": "string", "description": "源文件路径（相对于workspace）"},
                    "dst": {"type": "string", "description": "目标文件路径（相对于workspace）"}
                },
                "required": ["src", "dst"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "apply_patch",
            "description": "应用 unified diff 格式的 patch 到文件，比 replace_in_file 更适合多处批量修改",
            "parameters": {
                "type": "object",
                "properties": {
                    "patch_text": {"type": "string", "description": "unified diff 格式的 patch 字符串"},
                    "file_path": {"type": "string", "description": "目标文件路径（相对于workspace，可从 patch 自动推断）"}
                },
                "required": ["patch_text"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_symbols",
            "description": "列出文件中所有函数和类（名称、类型、行号），用于了解文件结构再做精确修改",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "文件路径（相对于workspace）"}
                },
                "required": ["file_path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "replace_symbol",
            "description": "替换指定函数或类的完整实现（用 AST 定位，不依赖字符串精确匹配，比 replace_in_file 更稳）",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol_name": {"type": "string", "description": "要替换的函数名或类名"},
                    "new_code": {"type": "string", "description": "新的完整实现代码"},
                    "file_path": {"type": "string", "description": "文件路径（相对于workspace）"}
                },
                "required": ["symbol_name", "new_code", "file_path"]
            }
        }
    }
]

def call_llm(messages, tools=None, tool_choice=None, response_format=None):
    """尝试QUALITY_CASCADE中的模型，依次降级调用。在子线程中执行，每100ms检查一次ESC中断。"""
    result_holder = [None]
    exc_holder = [None]

    def _worker():
        for model in QUALITY_CASCADE:
            try:
                kwargs = {"model": model, "messages": messages}
                if tools is not None:
                    kwargs["tools"] = tools
                if tool_choice is not None:
                    kwargs["tool_choice"] = tool_choice
                if response_format is not None:
                    kwargs["response_format"] = response_format
                result_holder[0] = client.chat.completions.create(**kwargs)
                return
            except Exception as e:
                exc_holder[0] = e
                continue

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    while t.is_alive():
        if interrupt.is_interrupted():
            raise interrupt.Interrupted()
        t.join(timeout=0.1)

    if result_holder[0] is None:
        raise RuntimeError(f"所有模型调用均失败: {exc_holder[0]}")
    return result_holder[0]


_ARCHITECT_ROLE = """【角色：架构师 Agent】
你专注于分析需求和制定实现计划。
职责：只输出计划，不写代码；重点考虑风险点和文件依赖顺序；确保计划完整可执行。
"""

_CODER_ROLE = """【角色：码农 Agent】
你专注于根据计划生成高质量代码。
职责：严格按计划执行，不自行发挥额外功能；注重代码质量和边界处理；已有文件用 replace_in_file 精确修改，不得整体重写。
"""

_TESTER_ROLE = """【角色：测试 Agent】
你专注于分析测试失败原因并指导修复。
职责：只关注测试结果和错误信息；给出精准、最小化的修复建议；避免引入不相关改动。
"""

def plan(requirement):
    """制定计划：生成文件列表和测试命令"""
    import platform

    # 检测系统并生成命令提示
    system_name = platform.system()
    if system_name == "Windows":
        cmd_hint = "当前运行环境是 Windows，使用 Windows 命令：查看文件用 type，列目录用 dir，禁止使用 cat、ls、grep。"
    else:
        cmd_hint = "当前运行环境是 Linux/Mac，使用 Unix 命令：查看文件用 cat，列目录用 ls。"

    # 先获取当前 workspace 文件结构，注入到 LLM 上下文中避免重复创建
    ws_files = list_files()
    files_list = "\n".join(f"- {f}" for f in ws_files.get("files", []))
    project_hint = (
        f"\n当前项目类型：{_PROJECT_TYPE}，默认测试命令：{_PROJECT_TEST_CMD}。"
        if _PROJECT_TYPE else ""
    )
    console.print("[Agent: Architect]", highlight=False)
    system_prompt = f"""{_ARCHITECT_ROLE}
你是一个代码规划助手。根据用户需求，返回JSON格式的计划，包含：
- files：数组，每个元素为 {{"filename": "文件名", "description": "修改意图/需求说明"}}；对于已有文件只需填写修改意图，不要重复列出完整内容
- test_command：测试命令

注意目录结构：实现文件放workspace/根目录（如add.py），测试文件必须放workspace/tests/目录（如tests/test_add.py）。
filename 字段只填相对路径，不要加 "workspace/" 前缀，正确示例：hello.py、tests/test_hello.py；错误示例：workspace/hello.py。
test_command 禁止使用 python -c 内联执行（会被安全策略拦截），应使用 python filename.py 方式。

{cmd_hint}{project_hint}

当前workspace已有文件：
{files_list if files_list else "(空)"}

注意：不要重复创建已有文件，尽量基于已有文件做增量修改。对已有文件只描述要追加/修改什么。"""
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"需求：{requirement}"}
    ]
    response = call_llm(messages, response_format={"type": "json_object"})
    return json.loads(response.choices[0].message.content)

def code(plan, mode="auto"):
    """根据计划逐个文件生成/修改代码。文件已存在优先用replace_in_file做精确修改；不存在则用write_file新建。"""
    import os as _os
    from tools import write_file, read_file, replace_in_file
    from config import WORKSPACE_DIR

    files = plan.get("files", [])
    console.print("[Agent: Coder]", highlight=False)
    console.print(f"计划处理 {len(files)} 个文件...")

    for file_entry in files:
        if interrupt.is_interrupted():
            raise interrupt.Interrupted()

        if isinstance(file_entry, dict):
            filename = file_entry.get("filename", "")
            intent = file_entry.get("intent", file_entry.get("description", ""))
        else:
            filename = file_entry
            intent = ""

        if not filename:
            continue

        # 剥离 LLM 偶尔错误添加的 workspace/ 前缀
        for _pfx in (WORKSPACE_DIR + "/", WORKSPACE_DIR + "\\"):
            if filename.startswith(_pfx):
                filename = filename[len(_pfx):]
                break

        filepath = _os.path.join(WORKSPACE_DIR, filename)
        file_exists = _os.path.exists(filepath)

        if file_exists:
            existing = read_file(filename)
            if "error" in existing:
                console.print(f"读取 {filename} 失败: {existing['error']}")
                continue
            existing_content = existing.get("content", "")
            console.print(f"{filename} 已存在，读取现有内容进行增量修改...")

            sys_prompt = f"""{_CODER_ROLE}
你是一个代码修改助手。对已有文件进行精确修改。

可用操作：
1. replace_in_file(filename, old_str, new_str) — 对已有文件做精确替换
2. write_file(filename, content) — 仅用于新建文件

规则：
- 已有文件**必须**使用 replace_in_file 做精确替换，不得使用 write_file 重写整个文件
- write_file 只允许用于新建文件
- 每次调用 replace_in_file 只修改一处，如有多处修改需要多次调用"""
        else:
            console.print(f"{filename} 是新建文件...")
            sys_prompt = f"""{_CODER_ROLE}
你是一个代码生成助手。请生成文件 `{filename}` 的完整代码。

可用操作：
1. write_file(filename, content) — 写入新文件

需求/修改意图：{intent}

注意：必须使用 write_file 写入文件，文件名严格使用 `{filename}`，不要修改路径或添加目录前缀。"""

        # 构建消息
        user_content = f"当前文件：{filename}\n修改意图：{intent}"
        if file_exists:
            user_content += f"\n\n现有内容：\n```\n{existing_content}\n```"

        msgs = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_content}
        ]

        # 多轮工具调用循环；新文件第一轮强制调用 write_file
        attempts_left = 5
        first_call = True
        while attempts_left > 0:
            attempts_left -= 1
            if first_call and not file_exists:
                tc = {"type": "function", "function": {"name": "write_file"}}
            else:
                tc = "auto"
            first_call = False
            response = call_llm(msgs, tools=TOOLS, tool_choice=tc)
            response_message = response.choices[0].message
            msgs.append(response_message)

            if response_message.tool_calls:
                for tool_call in response_message.tool_calls:
                    func_name = tool_call.function.name
                    func_args = json.loads(tool_call.function.arguments)

                    if func_name == "write_file":
                        fname = func_args.get("filename", "")
                        for _pfx in (WORKSPACE_DIR + "/", WORKSPACE_DIR + "\\"):
                            if fname.startswith(_pfx):
                                fname = fname[len(_pfx):]
                                func_args["filename"] = fname
                                break
                        import os as _os2
                        overwrite = _os2.path.exists(_os2.path.join(WORKSPACE_DIR, fname))
                        if mode == "auto" and overwrite:
                            confirm = _prompt(f"write_file 将覆盖已有文件 {fname}，确认？(y/n) ")
                            if confirm != "y":
                                result = {"error": "用户已跳过文件覆盖"}
                            else:
                                result = write_file(**func_args)
                                console.print(f"写入(覆盖) {fname}")
                        else:
                            result = write_file(**func_args)
                            console.print(f"写入 {fname}")
                        if "success" in result:
                            _task_files_modified.append(fname)
                    elif func_name == "read_file":
                        result = read_file(**func_args)
                    elif func_name == "replace_in_file":
                        _show_diff(
                            func_args.get("filename", ""),
                            func_args.get("old_str", ""),
                            func_args.get("new_str", ""),
                        )
                        skip = False
                        if mode == "auto":
                            confirm = _prompt("应用此修改？(y/n) ")
                            if confirm != "y":
                                result = {"error": "用户已跳过此修改"}
                                skip = True
                        if not skip:
                            result = replace_in_file(**func_args)
                            if "success" in result:
                                console.print(f"replace_in_file OK: {result.get('filename')}", highlight=False)
                                _task_files_modified.append(func_args.get("filename", ""))
                            else:
                                console.print(f"替换失败: {result.get('error')}", highlight=False)
                    elif func_name == "execute_command":
                        result = execute_command(**func_args)
                    elif func_name == "list_files":
                        result = list_files()
                    elif func_name == "get_symbol_definition":
                        result = get_symbol_definition(**func_args)
                    elif func_name == "search_in_files":
                        result = search_in_files(**func_args)
                    elif func_name == "move_file":
                        if mode == "auto":
                            confirm = _prompt(f"移动文件 {func_args.get('src')} → {func_args.get('dst')}？(y/n) ")
                            if confirm != "y":
                                result = {"error": "用户已跳过文件移动"}
                            else:
                                result = move_file(**func_args)
                        else:
                            result = move_file(**func_args)
                    elif func_name == "apply_patch":
                        result = apply_patch(**func_args)
                        if "success" in result:
                            _task_files_modified.append(func_args.get("file_path", ""))
                    elif func_name == "list_symbols":
                        result = list_symbols(**func_args)
                    elif func_name == "replace_symbol":
                        result = replace_symbol(**func_args)
                        if "success" in result:
                            _task_files_modified.append(func_args.get("file_path", ""))
                    else:
                        result = {"error": "未预期的调用"}

                    _task_tool_calls.append({"name": func_name, "args": {k: v for k, v in func_args.items() if k not in ("content", "new_str")}})
                    msgs.append({
                        "tool_call_id": tool_call.id,
                        "role": "tool",
                        "name": func_name,
                        "content": json.dumps(result)
                    })
            else:
                break

    console.print("代码生成/修改完成")

def fix(test_result, plan):
    """根据测试错误修复代码（多轮工具调用）"""
    console.print("[Agent: Tester]", highlight=False)
    console.print("开始修复代码...")
    
    # 优先使用 stderr，如果为空则使用截断的 stdout
    stderr = test_result.get("stderr", "")
    stdout = test_result.get("stdout", "")
    
    if stderr:
        error_info = stderr
    elif stdout:
        # 截断 stdout 到最多 500 字符
        error_info = stdout[:500]
        if len(stdout) > 500:
            error_info += "\n... (输出已截断)"
    else:
        error_info = "未知错误"
    
    content = f"测试失败！\n错误输出：\n{error_info}\n\n计划：{json.dumps(plan)}"
    
    messages = [
        {"role": "system", "content": f"{_TESTER_ROLE}\n你是代码修复助手。根据错误信息精准修复代码，优先使用 replace_in_file 做最小化修改，必要时才用 write_file 重写整个文件。"},
        {"role": "user", "content": content}
    ]
    
    while True:
        response = call_llm(messages, tools=TOOLS, tool_choice="auto")
        response_message = response.choices[0].message
        messages.append(response_message)

        if response_message.tool_calls:
            console.print(f"执行 {len(response_message.tool_calls)} 个修复操作...")
            for tool_call in response_message.tool_calls:
                func_name = tool_call.function.name
                func_args = json.loads(tool_call.function.arguments)
                
                if func_name == "write_file":
                    _fn = func_args.get("filename", "")
                    for _pfx in (WORKSPACE_DIR + "/", WORKSPACE_DIR + "\\"):
                        if _fn.startswith(_pfx):
                            func_args["filename"] = _fn[len(_pfx):]
                            break
                    result = write_file(**func_args)
                    console.print(f"修复 {func_args.get('filename')}")
                    if "success" in result:
                        _task_files_modified.append(func_args.get("filename", ""))
                elif func_name == "read_file":
                    result = read_file(**func_args)
                elif func_name == "replace_in_file":
                    result = replace_in_file(**func_args)
                    if "success" in result:
                        _task_files_modified.append(func_args.get("filename", ""))
                elif func_name == "execute_command":
                    result = execute_command(**func_args)
                elif func_name == "list_files":
                    result = list_files()
                elif func_name == "get_symbol_definition":
                    result = get_symbol_definition(**func_args)
                elif func_name == "search_in_files":
                    result = search_in_files(**func_args)
                elif func_name == "move_file":
                    result = move_file(**func_args)
                elif func_name == "apply_patch":
                    result = apply_patch(**func_args)
                    if "success" in result:
                        _task_files_modified.append(func_args.get("file_path", ""))
                elif func_name == "list_symbols":
                    result = list_symbols(**func_args)
                elif func_name == "replace_symbol":
                    result = replace_symbol(**func_args)
                    if "success" in result:
                        _task_files_modified.append(func_args.get("file_path", ""))
                else:
                    result = {"error": "未预期的调用"}

                _task_tool_calls.append({"name": func_name, "args": {k: v for k, v in func_args.items() if k not in ("content", "new_str", "new_code")}})
                messages.append({
                    "tool_call_id": tool_call.id,
                    "role": "tool",
                    "name": func_name,
                    "content": json.dumps(result)
                })
        else:
            console.print("修复完成")
            break

def test(test_command):
    """执行测试命令"""
    if not test_command or not test_command.strip():
        console.print("警告：无测试命令，跳过测试")
        return {"returncode": 0, "stdout": "", "stderr": ""}
    
    console.print(f"执行测试：{test_command}")
    return execute_command(test_command)

def judge(test_result):
    """判断测试是否通过"""
    return test_result.get("returncode") == 0

def report(success, test_result):
    """输出最终结果"""
    return {
        "success": success,
        "test_result": test_result
    }

def _interrupted_result():
    console.print("已中断")
    return {"success": False, "test_result": {"returncode": -1, "stdout": "", "stderr": "已中断"}}


def run(requirement, mode="auto"):
    """主运行流程。mode: auto（默认）| plan（只出计划）| code（跳过确认直接执行）"""
    try:
        return _run(requirement, mode)
    except interrupt.Interrupted:
        return _interrupted_result()


def _run(requirement, mode):
    original_requirement = requirement
    init_task_log(requirement, mode)

    # 阶段1：制定计划
    console.print("阶段1：制定计划")
    plan_result = plan(requirement)
    _current_task_log["plan"] = plan_result.get("files", [])
    _current_task_log["test_command"] = plan_result.get("test_command", "")

    # 格式化计划输出
    def print_plan(plan_result):
        files = plan_result.get("files", [])
        test_cmd = plan_result.get("test_command", "")
        total_steps = len(files) + (1 if test_cmd else 0)
        for idx, file_entry in enumerate(files, 1):
            filename = file_entry.get("filename", "") if isinstance(file_entry, dict) else file_entry
            console.print(f"[{idx}/{total_steps}] write_file: {filename}")
        if test_cmd:
            console.print(f"[{total_steps}/{total_steps}] execute: {test_cmd}")

    print_plan(plan_result)

    # plan 模式：只输出计划，直接返回
    if mode == "plan":
        finish_task_log(True, 0)
        return {"success": True, "test_result": {"returncode": 0, "stdout": "", "stderr": ""}}

    # auto 模式：等待用户确认，支持修改重试（批处理模式自动确认）
    if mode == "auto":
        retry_count = 0
        max_retries = 3
        while retry_count <= max_retries:
            user_confirm = _prompt("\n确认执行？(y/n/修改) ")
            if interrupt.is_interrupted():
                raise interrupt.Interrupted()
            if user_confirm == 'y':
                break
            elif user_confirm == 'n':
                console.print("已取消")
                finish_task_log(False, 0)
                return {"success": False, "test_result": {"returncode": -1, "stdout": "", "stderr": "用户取消"}}
            else:
                if retry_count >= max_retries:
                    console.print(f"已达到最大重试次数 ({max_retries})，使用当前计划")
                    break
                console.print(f"正在根据修改意见重新生成计划... (尝试 {retry_count + 1}/{max_retries})")
                plan_result = plan(f"{requirement}\n\n修改意见：{user_confirm}")
                print_plan(plan_result)
                retry_count += 1

    # #37 任务开始前快照已有文件
    file_list = [
        (f.get("filename") if isinstance(f, dict) else f)
        for f in plan_result.get("files", [])
    ]
    file_list = [f for f in file_list if f]
    current_snapshot = create_snapshot(file_list)

    # code / auto（确认后）：生成代码 + 测试
    console.print("\n阶段2：生成代码")
    code(plan_result, mode=mode)

    console.print("\n阶段3：测试与修复")
    attempts = 0
    test_result = None
    max_attempts = _cfg("max_attempts") or 3

    # #42 如果 workspace 中没有测试文件，自动生成
    ws = Path(WORKSPACE_DIR)
    has_tests = bool(
        [f for f in ws.rglob("test_*.py") if ".yansh" not in f.parts]
        + [f for f in ws.rglob("*_test.py") if ".yansh" not in f.parts]
    )
    if not has_tests:
        _auto_generate_tests(plan_result, _task_files_modified[:])

    # #26 Linter：先跑 ruff，有错误走修复循环
    linter_result = run_linter()
    if linter_result:
        console.print(f"Linter 发现错误，开始修复 (尝试 {attempts + 1}/{max_attempts})", highlight=False)
        fix(linter_result, plan_result)
        attempts += 1

    while attempts < max_attempts:
        test_result = test(plan_result.get("test_command", ""))
        if judge(test_result):
            console.print("测试通过！")
            cleanup_snapshot(current_snapshot)
            files = plan_result.get("files", [])
            file_names = [f.get("filename") if isinstance(f, dict) else str(f) for f in files]
            file_names = [name for name in file_names if name]
            summary = f"执行了任务：{original_requirement}。创建/修改了文件：{', '.join(file_names)}"
            add_to_history(original_requirement, summary)
            finish_task_log(True, attempts, test_result)
            return report(True, test_result)
        else:
            console.print(f"测试失败 (尝试 {attempts + 1}/{max_attempts})")
            if attempts < max_attempts - 1:
                fix(test_result, plan_result)
            attempts += 1

    console.print("达到最大尝试次数，任务失败")
    # #37 失败时提示回滚
    if current_snapshot:
        answer = _prompt("是否回滚到任务开始前的状态？(y/n) ", default="n")
        if answer == "y":
            n = restore_snapshot(current_snapshot)
            console.print(f"[已回滚] 恢复 {n} 个文件", highlight=False)
            cleanup_snapshot(current_snapshot)
        else:
            cleanup_snapshot(current_snapshot)

    add_to_history(original_requirement, f"任务失败：{original_requirement}")
    finish_task_log(False, attempts, test_result)
    return report(False, test_result)


def _auto_generate_tests(plan_result, modified_files):
    """#42 无测试文件时，自动为本次修改的 .py 文件生成最小测试"""
    non_test_srcs = [
        f for f in modified_files
        if f and f.endswith(".py")
        and not Path(f).name.startswith("test_")
        and not Path(f).stem.endswith("_test")
    ]
    if not non_test_srcs:
        return

    console.print("[自动生成测试] 未发现测试文件，自动生成最小测试...", highlight=False)

    file_contents = []
    for src in non_test_srcs:
        r = read_file(src)
        if "content" in r:
            file_contents.append(f"# {src}\n{r['content']}")

    if not file_contents:
        return

    combined = "\n\n".join(file_contents)
    test_targets = ", ".join(f"tests/test_{Path(f).name}" for f in non_test_srcs)

    msgs = [
        {"role": "system", "content": f"""{_CODER_ROLE}
你是测试生成助手。根据源代码生成最小测试文件，覆盖正常路径、边界值、异常输入三种case。
测试文件放在 tests/ 目录下，文件名格式：test_<原文件名>.py。
测试文件开头必须加：
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
使用 write_file 工具写入测试文件，不要用其他工具。"""},
        {"role": "user", "content": f"为以下源文件生成测试（目标文件：{test_targets}）：\n\n{combined}"}
    ]

    rounds = 3
    first_round = True
    while rounds > 0:
        rounds -= 1
        tc = {"type": "function", "function": {"name": "write_file"}} if first_round else "auto"
        first_round = False
        response = call_llm(msgs, tools=TOOLS, tool_choice=tc)
        msg = response.choices[0].message
        msgs.append(msg)
        if msg.tool_calls:
            for tc in msg.tool_calls:
                fname = tc.function.name
                args = json.loads(tc.function.arguments)
                if fname == "write_file":
                    _fn = args.get("filename", "")
                    for _pfx in (WORKSPACE_DIR + "/", WORKSPACE_DIR + "\\"):
                        if _fn.startswith(_pfx):
                            _fn = _fn[len(_pfx):]
                            break
                    # 确保测试文件写入 tests/ 子目录
                    _base = Path(_fn).name
                    if _base.startswith("test_") and "/" not in _fn and "\\" not in _fn:
                        _fn = "tests/" + _base
                    args["filename"] = _fn
                    result = write_file(**args)
                    console.print(f"[自动测试] 生成: {args.get('filename')}", highlight=False)
                    if "success" in result:
                        _task_files_modified.append(args.get("filename", ""))
                else:
                    result = {"error": "测试生成阶段只允许 write_file"}
                msgs.append({"tool_call_id": tc.id, "role": "tool", "name": fname, "content": json.dumps(result)})
        else:
            break


def classify_input(user_input):
    """判断用户输入是新任务还是闲聊"""
    messages = [
        {"role": "system", "content": "判断以下输入是'新任务'还是'闲聊'，只回复 task 或 chat。"},
        {"role": "user", "content": f"输入：{user_input}"}
    ]
    response = call_llm(messages)
    result = response.choices[0].message.content.strip().lower()
    return "task" if "task" in result else "chat"

def chat(user_input):
    """闲聊模式，LLM 直接回复，控制在 100 字以内"""
    messages = [
        {"role": "system", "content": "你是一个友好的助手。简洁回复用户，控制在 100 字以内。"}
    ]
    
    # 添加最近5轮历史
    messages.extend(get_recent_history())
    
    # 添加当前用户输入
    messages.append({"role": "user", "content": user_input})
    
    response = call_llm(messages)
    assistant_reply = response.choices[0].message.content
    
    # 保存到历史
    add_to_history(user_input, assistant_reply)
    
    return assistant_reply
