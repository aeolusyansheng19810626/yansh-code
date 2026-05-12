import json
import os
import sys
import shutil
import threading
import difflib
import time as _time
from datetime import datetime
from openai import OpenAI
from rich.console import Console
from pathlib import Path
from config import (
    OPENROUTER_API_KEY, OPENROUTER_BASE_URL, QUALITY_CASCADE, WORKSPACE_DIR, get_config,
    TOKEN_PRICE_INPUT, TOKEN_PRICE_OUTPUT
)
from tools import (
    write_file, read_file, execute_command, list_files, replace_in_file,
    get_symbol_definition, search_in_files, move_file, apply_patch,
    list_symbols, replace_symbol, fetch_webpage, search_docs, append_to_file,
    find_references
)
import interrupt
import tools as _tools_mod

console = Console()

# #62 Token 统计
_session_tokens = {"prompt": 0, "completion": 0}
_last_request_tokens = {"prompt": 0, "completion": 0}

# #40 批处理模式标志
_BATCH_MODE = False
_last_task_log: dict = {}  # 最近一次任务日志，供 --json 输出

# #27 项目类型（由 main.py 调用 detect_project_type() 后写入）
_PROJECT_TYPE = None
_PROJECT_TEST_CMD = None

# #37 快照 / #38 日志 / #61 回放 目录
_YANSH_DIR     = Path(WORKSPACE_DIR) / ".yansh"
_SNAPSHOT_DIR  = _YANSH_DIR / "snapshots"
_LOG_DIR       = _YANSH_DIR / "logs"
_REPLAY_DIR    = _YANSH_DIR / "replay"

# #38 当前任务日志状态（模块级，_run() 期间填充）
_current_task_log: dict = {}
_task_tool_calls: list  = []
_task_files_modified: list = []

# 对话历史管理
conversation_history = []
MAX_HISTORY = 20
CHAT_CONTEXT_ROUNDS = 5
COMPRESS_MODEL = "meta-llama/llama-3.1-8b-instant"

# #57 session 级别上下文文件
_context_files: dict = {}  # {display_path: content}
_MAX_CONTEXT_FILE_SIZE = 100 * 1024  # 100KB

# #58 HIL（人工介入）本轮"全部接受"标志，每次 _run() 开始时重置
_HIL_AUTO_ACCEPT = False

# #50 多模态视觉：当前轮次待注入的图片，plan()/chat() 消费后清空
_pending_images: list = []


def _cfg(key):
    """读取生效配置值"""
    return get_config().get(key)


def _load_context_file(raw_path: str):
    """将文件加载到 _context_files，含大小和文本格式校验。"""
    global _context_files
    p = Path(raw_path)
    if not p.is_absolute():
        p = (Path(os.getcwd()) / p).resolve()
    else:
        p = p.resolve()

    if not p.exists() or not p.is_file():
        console.print(f"[上下文] 文件不存在: {raw_path}", style="yellow", highlight=False)
        return

    if p.stat().st_size > _MAX_CONTEXT_FILE_SIZE:
        console.print(f"[上下文] 文件过大（>100KB），已跳过: {raw_path}", style="yellow", highlight=False)
        return

    try:
        content = p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        console.print(f"[上下文] 非文本文件，已跳过: {raw_path}", style="yellow", highlight=False)
        return
    except Exception as e:
        console.print(f"[上下文] 读取失败: {raw_path} ({e})", style="red", highlight=False)
        return

    try:
        display_path = str(p.relative_to(Path(os.getcwd())))
    except ValueError:
        display_path = str(p)

    lines = content.count("\n") + 1
    _context_files[display_path] = content
    console.print(f"✓ 已加载: {display_path} ({lines} 行)", highlight=False)


def _parse_context_cmds(user_input: str) -> str:
    """解析 @add_file <path> 和 @clear_files，更新 _context_files，返回去除指令后的文本。"""
    import re
    global _context_files

    if "@clear_files" in user_input:
        _context_files.clear()
        console.print("[上下文] 已清空所有上下文文件", highlight=False)
        user_input = re.sub(r"@clear_files\b", "", user_input).strip()

    pattern = r'@add_file\s+(?:"([^"]+)"|\'([^\']+)\'|(\S+))'
    for m in re.finditer(pattern, user_input):
        raw_path = m.group(1) or m.group(2) or m.group(3)
        _load_context_file(raw_path)
    user_input = re.sub(pattern, "", user_input).strip()
    return user_input


# 每个上下文文件注入到 prompt 的最大字符数（约 2000 token）
_MAX_CONTEXT_INJECT_CHARS = 8000

def _get_context_files_block() -> str:
    """构建上下文文件注入块。每个文件超过限制时截断并提示，避免 token 爆炸。"""
    if not _context_files:
        return ""
    parts = ["=== 附加上下文文件 ==="]
    for path, content in _context_files.items():
        if len(content) > _MAX_CONTEXT_INJECT_CHARS:
            truncated = content[:_MAX_CONTEXT_INJECT_CHARS]
            omitted = len(content) - _MAX_CONTEXT_INJECT_CHARS
            parts.append(f"--- 文件: {path} (已截断，省略末尾 {omitted} 字符) ---")
            parts.append(truncated)
            parts.append(f"... [已截断，完整文件共 {len(content)} 字符，超出限制 {_MAX_CONTEXT_INJECT_CHARS}] ...")
        else:
            parts.append(f"--- 文件: {path} ---")
            parts.append(content)
    return "\n".join(parts)


# ---------- #50 多模态视觉 ----------

def _process_pil_image(img, display: str) -> dict:
    """将 PIL Image 转为 base64 PNG dict，超过 2048px 时自动缩放。"""
    import base64, io
    MAX_SIDE = 2048
    orig_w, orig_h = img.size
    if max(orig_w, orig_h) > MAX_SIDE:
        ratio = MAX_SIDE / max(orig_w, orig_h)
        new_w = max(1, int(orig_w * ratio))
        new_h = max(1, int(orig_h * ratio))
        from PIL import Image
        img = img.resize((new_w, new_h), Image.LANCZOS)
        console.print(f"[图片] 已缩放: {orig_w}x{orig_h} → {new_w}x{new_h}", highlight=False)
    final_w, final_h = img.size
    if img.mode not in ('RGB', 'RGBA', 'L'):
        img = img.convert('RGBA')
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    b64 = base64.b64encode(buf.getvalue()).decode('ascii')
    console.print(f"✓ 已加载图片: {display} ({final_w}x{final_h} px)", highlight=False)
    return {"base64": b64, "mime_type": "image/png", "width": final_w, "height": final_h, "source": display}


def _load_image_file(path_str: str) -> dict:
    """从本地路径加载图片，超大自动缩放。支持 PNG/JPEG/GIF/WEBP。"""
    try:
        from PIL import Image
    except ImportError:
        return {"error": "需要安装 Pillow: pip install Pillow>=10.0.0"}
    p = Path(path_str)
    if not p.is_absolute():
        p = (Path(os.getcwd()) / p).resolve()
    else:
        p = p.resolve()
    if not p.exists() or not p.is_file():
        return {"error": f"文件不存在: {path_str}"}
    if p.suffix.lower() not in {'.png', '.jpg', '.jpeg', '.gif', '.webp'}:
        return {"error": f"不支持的图片格式: {p.suffix}，支持 PNG/JPEG/GIF/WEBP"}
    try:
        img = Image.open(str(p))
        img.load()
        try:
            img.seek(0)  # GIF 取第一帧
        except (AttributeError, EOFError):
            pass
        img = img.copy()
    except Exception as e:
        return {"error": f"图片读取失败: {e}"}
    try:
        display = str(p.relative_to(Path(os.getcwd())))
    except ValueError:
        display = p.name
    return _process_pil_image(img, display)


def _load_image_url(url: str) -> dict:
    """从 URL 下载图片并处理。"""
    try:
        from PIL import Image
    except ImportError:
        return {"error": "需要安装 Pillow: pip install Pillow>=10.0.0"}
    try:
        import requests, io as _io
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        if len(resp.content) > 20 * 1024 * 1024:
            return {"error": "图片文件过大（>20MB）"}
        img = Image.open(_io.BytesIO(resp.content))
        img = img.copy()
    except Exception as e:
        return {"error": f"图片下载失败: {url} ({e})"}
    result = _process_pil_image(img, url)
    return result


def _load_clipboard_image() -> dict:
    """从剪贴板读取图片（Windows via PIL.ImageGrab）。"""
    try:
        from PIL import ImageGrab
    except ImportError:
        return {"error": "需要安装 Pillow: pip install Pillow>=10.0.0"}
    try:
        img = ImageGrab.grabclipboard()
    except Exception as e:
        return {"error": f"读取剪贴板失败: {e}"}
    if img is None:
        return {"error": "剪贴板中没有图片，请先截图或复制图片"}
    return _process_pil_image(img, "clipboard")


def _parse_image_cmds(user_input: str):
    """解析 @image <路径/URL> 和 @paste，返回 (清理后文本, 图片列表)。"""
    import re
    images = []
    if "@paste" in user_input:
        result = _load_clipboard_image()
        if "error" in result:
            console.print(f"[图片] {result['error']}", style="yellow", highlight=False)
        else:
            images.append(result)
        user_input = re.sub(r"@paste\b", "", user_input)
    pattern = r'@image\s+(?:"([^"]+)"|\'([^\']+)\'|(\S+))'
    for m in re.finditer(pattern, user_input):
        raw = m.group(1) or m.group(2) or m.group(3)
        if raw.startswith(("http://", "https://")):
            result = _load_image_url(raw)
        else:
            result = _load_image_file(raw)
        if "error" in result:
            console.print(f"[图片] {result['error']}", style="yellow", highlight=False)
        else:
            images.append(result)
    user_input = re.sub(pattern, "", user_input).strip()
    return user_input, images


def _build_vision_content(text: str, images: list) -> list:
    """构造 OpenAI vision content 数组：图片在前，文字在后。"""
    content = []
    for img in images:
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{img['mime_type']};base64,{img['base64']}"}
        })
    content.append({"type": "text", "text": text})
    return content


def _process_at_files(user_input):
    """解析 @filename 语法并注入文件内容"""
    import re
    from tools import _validate_path
    
    # 匹配 @文件名（支持路径字符，但不包含空格）
    # 排除结尾的标点符号
    pattern = r"@([\w\.\-/]+)"
    matches = re.finditer(pattern, user_input)
    
    injected_texts = []
    found_files = []
    
    for m in matches:
        filename = m.group(1)
        resolved, err = _validate_path(filename)
        if err:
            console.print(f"[警告] 注入文件失败: {filename} ({err['error']})", style="yellow")
            continue
        
        if not resolved.exists() or not resolved.is_file():
            console.print(f"[警告] 注入文件不存在: {filename}", style="yellow")
            continue
            
        try:
            content = resolved.read_text(encoding="utf-8", errors="replace")
            # 简单的语言识别
            ext = resolved.suffix[1:] if resolved.suffix else "text"
            lang_map = {"py": "python", "js": "javascript", "ts": "typescript", "html": "html", "css": "css", "md": "markdown", "json": "json"}
            lang = lang_map.get(ext, ext)
            
            injected_texts.append(f"\n[文件上下文: {filename}]\n```{lang}\n{content}\n```")
            found_files.append(filename)
        except Exception as e:
            console.print(f"[错误] 读取注入文件失败: {filename} ({e})", style="red")

    if not found_files:
        return user_input

    # 检查 token 阈值警告
    total_len = sum(len(t) for t in injected_texts) + len(user_input)
    threshold = _cfg("compress_threshold") or 6000
    if total_len > threshold:
        console.print(f"[警告] 本次请求注入内容较多 ({total_len} 字符)，可能导致上下文提前压缩。", style="yellow")

    return user_input + "\n" + "\n".join(injected_texts)


def set_batch_mode(enabled: bool, json_output: bool = False, strict: bool | None = None):
    """设置批处理模式；json_output=True 时将 console 重定向到 stderr；
    strict=True 时批处理下仍拒绝 Level-3 需确认命令（pip install / git reset 等）"""
    global _BATCH_MODE, console
    _BATCH_MODE = enabled
    _tools_mod.set_batch_mode(enabled, strict=strict)
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

# [已弃用] OpenRouter/DeepSeek 客户端
# client = OpenAI(
#     api_key=OPENROUTER_API_KEY,
#     base_url=OPENROUTER_BASE_URL,
# )

# Claude 客户端（通过 IBM ICA 网关，走 OpenAI 兼容协议）
# OPENROUTER_API_KEY / OPENROUTER_BASE_URL 已在 config.py 中指向 Claude/ICA，
# 此处变量名保持不变，避免牵动 import。
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


# ---------- #61 失败案例回放包 ----------

def create_replay_package(failure_reason):
    """当任务失败时，自动打包现场供回放调试"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    replay_dir = _REPLAY_DIR / f"replay_{timestamp}"
    replay_dir.mkdir(parents=True, exist_ok=True)
    
    # 1. 完整对话历史
    (replay_dir / "conversation.json").write_text(
        json.dumps(conversation_history, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    
    # 2. workspace 快照
    snap_dir = replay_dir / "workspace_snapshot"
    snap_dir.mkdir()
    for root, _, files in os.walk(WORKSPACE_DIR):
        if any(p in root for p in (".git", ".yansh", "__pycache__", "venv", "node_modules")):
            continue
        for f in files:
            src = Path(root) / f
            rel = src.relative_to(WORKSPACE_DIR)
            dst = snap_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(str(src), str(dst))
            except Exception:
                pass
                
    # 3. 复制 agent 日志 (最近一个)
    if _LOG_DIR.exists():
        logs = sorted(_LOG_DIR.glob("*.jsonl"), reverse=True)
        if logs:
            shutil.copy2(str(logs[0]), str(replay_dir / "agent.log"))
            
    # 4. 元数据
    meta = {
        "failure_reason": failure_reason,
        "timestamp": timestamp,
        "model": QUALITY_CASCADE[0] if QUALITY_CASCADE else "unknown",
        "tokens": _session_tokens.copy()
    }
    (replay_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    
    console.print(f"\n💾 [bold]失败案例已保存[/bold]: {replay_dir}", highlight=False)
    return replay_dir.name

def list_replays():
    """列出所有回放包"""
    if not _REPLAY_DIR.exists():
        console.print("无回放包", highlight=False)
        return
    dirs = sorted([d for d in _REPLAY_DIR.iterdir() if d.is_dir()], reverse=True)
    if not dirs:
        console.print("无回放包", highlight=False)
        return
    for d in dirs:
        meta_file = d / "meta.json"
        reason = "未知原因"
        if meta_file.exists():
            try:
                reason = json.loads(meta_file.read_text(encoding="utf-8")).get("failure_reason", reason)
            except Exception: pass
        console.print(f"- {d.name} | {reason[:40]}", highlight=False)

def load_replay(replay_id):
    """加载回放包的对话历史"""
    target = _REPLAY_DIR / replay_id
    if not target.exists():
        # 尝试模糊匹配 (replay_YYYYMMDD_HHMMSS)
        target = _REPLAY_DIR / f"replay_{replay_id}"
        if not target.exists():
            console.print(f"[错误] 回放包 {replay_id} 不存在", style="red")
            return
            
    conv_file = target / "conversation.json"
    if not conv_file.exists():
        console.print("[错误] 回放包中未找到历史记录", style="red")
        return
        
    try:
        global conversation_history
        conversation_history = json.loads(conv_file.read_text(encoding="utf-8"))
        save_history()
        console.print(f"[成功] 已加载回放包 {target.name} 的对话历史 (共 {len(conversation_history)//2} 轮)", highlight=False)
    except Exception as e:
        console.print(f"[错误] 加载失败: {e}", style="red")

def _reinit_paths():
    """--cwd 变更后重新初始化 agent 中所有依赖 WORKSPACE_DIR 的模块级变量。"""
    global _YANSH_DIR, _SNAPSHOT_DIR, _LOG_DIR, _REPLAY_DIR, _HISTORY_FILE
    import config as _cfg_mod
    _wd = _cfg_mod.WORKSPACE_DIR
    _YANSH_DIR     = Path(_wd) / ".yansh"
    _SNAPSHOT_DIR  = _YANSH_DIR / "snapshots"
    _LOG_DIR       = _YANSH_DIR / "logs"
    _REPLAY_DIR    = _YANSH_DIR / "replay"
    _HISTORY_FILE  = Path(_wd) / ".yansh_history.json"


# ---------- #37 快照 / 回滚 ----------

# 快照/回滚时需要跳过的目录
_SNAPSHOT_IGNORE_DIRS = {".git", ".yansh", "__pycache__", "venv", "node_modules", ".pytest_cache"}

def _should_skip_dir(root: str) -> bool:
    """判断 os.walk 的某个 root 路径是否应跳过"""
    parts = set(Path(root).parts)
    return bool(parts & _SNAPSHOT_IGNORE_DIRS)


def _git_run(args: list, cwd: str) -> tuple:
    """在指定目录运行 git 命令，返回 (returncode, stdout, stderr)。"""
    import subprocess
    r = subprocess.run(
        ["git"] + args,
        cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    return r.returncode, r.stdout, r.stderr


def _git_is_repo(cwd: str) -> bool:
    """检查 cwd 是否在 git 仓库内。"""
    rc, _, _ = _git_run(["rev-parse", "--git-dir"], cwd)
    return rc == 0


def create_snapshot(file_list):
    """备份工作区：若在 git 仓库内用 git stash，否则回退到文件复制快照。
    返回快照标识（git 模式：stash 消息前缀；文件模式：Path 对象）。"""
    import config as _cfg_mod
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    ws = _cfg_mod.WORKSPACE_DIR

    if _git_is_repo(ws):
        # git 模式：先 add 所有未追踪文件（确保新建文件也被 stash），再 stash
        stash_msg = f"yansh-snapshot-{timestamp}"
        _git_run(["add", "-A"], ws)
        rc, stdout, stderr = _git_run(["stash", "push", "-m", stash_msg], ws)
        if rc == 0 and "No local changes" not in stdout:
            console.print(f"[快照] git stash: {stash_msg}", highlight=False)
            return {"mode": "git", "msg": stash_msg, "timestamp": timestamp}
        else:
            # 工作区干净，没有东西可 stash
            console.print("[快照] 工作区干净，无需 stash", highlight=False)
            return {"mode": "git_clean", "timestamp": timestamp}

    # 文件复制模式（兜底）
    snap_dir = _SNAPSHOT_DIR / timestamp
    snap_dir.mkdir(parents=True, exist_ok=True)
    workspace_files = []
    for root, dirs, files in os.walk(ws):
        if _should_skip_dir(root):
            dirs.clear()
            continue
        for filename in files:
            rel_path = os.path.relpath(os.path.join(root, filename), ws)
            workspace_files.append(rel_path.replace("\\", "/"))
    backed = []
    for filename in file_list:
        src = Path(ws) / filename
        if src.exists() and src.is_file():
            dst = snap_dir / filename
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), str(dst))
            backed.append(filename)
    (snap_dir / "meta.json").write_text(
        json.dumps({"files": backed, "workspace_files": workspace_files, "timestamp": timestamp},
                   ensure_ascii=False), encoding="utf-8"
    )
    console.print(f"[快照] 文件复制模式: {snap_dir.name}", highlight=False)
    return {"mode": "file", "path": str(snap_dir), "timestamp": timestamp}


def restore_snapshot(snap_info):
    """根据快照标识恢复工作区，返回恢复数量。"""
    if not snap_info:
        return 0

    mode = snap_info.get("mode") if isinstance(snap_info, dict) else None

    # 兼容旧版 Path 对象形式
    if isinstance(snap_info, Path) or mode == "file":
        snap_dir = Path(snap_info["path"]) if isinstance(snap_info, dict) else snap_info
        return _restore_file_snapshot(snap_dir)

    if mode == "git_clean":
        console.print("[回滚] 工作区原本干净，无需恢复", highlight=False)
        return 0

    if mode == "git":
        import config as _cfg_mod
        stash_msg = snap_info["msg"]
        ws = _cfg_mod.WORKSPACE_DIR
        # 找到对应的 stash 索引
        rc, stdout, _ = _git_run(["stash", "list"], ws)
        stash_idx = None
        for line in stdout.splitlines():
            if stash_msg in line:
                # 格式: stash@{0}: On branch: msg
                stash_idx = line.split(":")[0]  # "stash@{0}"
                break
        if stash_idx is None:
            console.print(f"[回滚] 未找到 stash: {stash_msg}", style="yellow", highlight=False)
            return 0
        # 恢复：先丢弃当前改动，再 pop
        _git_run(["checkout", "--", "."], ws)
        _git_run(["clean", "-fd", "--exclude=.yansh"], ws)
        rc, _, stderr = _git_run(["stash", "pop", stash_idx], ws)
        if rc == 0:
            console.print(f"[回滚] git stash pop 成功", highlight=False)
            # 把恢复的文件重新变为未追踪状态（stash pop 后是 staged）
            _git_run(["reset", "HEAD", "."], ws)
            return 1
        else:
            console.print(f"[回滚] git stash pop 失败: {stderr}", style="red", highlight=False)
            return 0

    return 0


def _restore_file_snapshot(snap_dir: Path) -> int:
    """文件复制模式的恢复逻辑（原 restore_snapshot）。"""
    import config as _cfg_mod
    ws = _cfg_mod.WORKSPACE_DIR
    meta_file = snap_dir / "meta.json"
    if not meta_file.exists():
        return 0
    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    restored = 0
    for filename in meta.get("files", []):
        src = snap_dir / filename
        dst = Path(ws) / filename
        if src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), str(dst))
            restored += 1
    workspace_files_then = set(meta.get("workspace_files", []))
    current_files = []
    for root, dirs, files in os.walk(ws):
        if _should_skip_dir(root):
            dirs.clear()
            continue
        for filename in files:
            rel_path = os.path.relpath(os.path.join(root, filename), ws)
            current_files.append(rel_path.replace("\\", "/"))
    for f in current_files:
        if f not in workspace_files_then:
            path = Path(ws) / f
            try:
                if path.exists():
                    path.unlink()
            except Exception as e:
                console.print(f"[警告] 回滚时无法删除 {f}: {e}", style="yellow", highlight=False)
    return restored


def cleanup_snapshot(snap_info):
    """清理快照：git 模式 drop stash，文件模式删除目录。"""
    import config as _cfg_mod
    if not snap_info:
        return
    mode = snap_info.get("mode") if isinstance(snap_info, dict) else None
    if mode == "git":
        stash_msg = snap_info["msg"]
        ws = _cfg_mod.WORKSPACE_DIR
        rc, stdout, _ = _git_run(["stash", "list"], ws)
        for line in stdout.splitlines():
            if stash_msg in line:
                stash_idx = line.split(":")[0]
                _git_run(["stash", "drop", stash_idx], ws)
                break
    elif mode == "file":
        snap_dir = Path(snap_info["path"])
        if snap_dir.exists():
            shutil.rmtree(str(snap_dir))
    elif isinstance(snap_info, Path) and snap_info.exists():
        shutil.rmtree(str(snap_info))


def get_latest_snapshot():
    """返回最新快照目录，不存在返回 None（文件复制模式兼容）"""
    if not _SNAPSHOT_DIR.exists():
        return None
    candidates = sorted(
        (s for s in _SNAPSHOT_DIR.iterdir() if s.is_dir() and (s / "meta.json").exists()),
        reverse=True
    )
    if not candidates:
        return None
    s = candidates[0]
    return {"mode": "file", "path": str(s), "timestamp": s.name}


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

# ---------- #58 HIL（人工介入编辑）----------

def _detect_editor():
    """返回可用编辑器命令列表。Windows 优先 VS Code，其次 notepad；Unix 读 $VISUAL/$EDITOR，否则 vi。"""
    import shutil
    if sys.platform == "win32":
        if shutil.which("code"):
            return ["code", "--wait"]
        return ["notepad"]
    for var in ("VISUAL", "EDITOR"):
        val = os.environ.get(var)
        if val:
            return val.split()
    return ["vi"]


def _build_diff_lines(filename, old_content, new_content, is_new_file=False):
    """生成 unified diff 行列表。超过 50 行时截断（头30 + 尾10）。"""
    old_lines = [] if is_new_file else old_content.splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)
    from_file = "新建文件" if is_new_file else f"a/{filename}"
    diff = list(difflib.unified_diff(
        old_lines, new_lines,
        fromfile=from_file, tofile=f"b/{filename}", lineterm=""
    ))
    if len(diff) > 50:
        omitted = len(diff) - 40
        diff = diff[:30] + [f"...已截断，共 {len(diff)} 行变更，省略 {omitted} 行..."] + diff[-10:]
    return diff


def _print_diff_colored(diff_lines):
    """彩色输出 diff 行列表。"""
    for line in diff_lines:
        if line.startswith("---") or line.startswith("+++"):
            console.print(line, style="bold", highlight=False)
        elif line.startswith("-"):
            console.print(line, style="red", highlight=False)
        elif line.startswith("+"):
            console.print(line, style="green", highlight=False)
        elif line.startswith("@@"):
            console.print(line, style="cyan", highlight=False)
        elif line.startswith("..."):
            console.print(line, style="yellow", highlight=False)
        else:
            console.print(line, highlight=False)


def _hil_confirm(filename, old_content, new_content, is_new_file=False):
    """展示 diff 并询问用户处理方式。
    返回 (accept: bool, final_content: str)。
    选 'a' 时设置 _HIL_AUTO_ACCEPT = True，后续不再询问。
    """
    global _HIL_AUTO_ACCEPT
    if _HIL_AUTO_ACCEPT:
        return True, new_content

    diff_lines = _build_diff_lines(filename, old_content, new_content, is_new_file)
    if not diff_lines:
        return True, new_content

    label = "新建文件" if is_new_file else "修改文件"
    console.print(f"\n[HIL] {label}: {filename}", highlight=False)
    _print_diff_colored(diff_lines)
    console.print("\n[y] 接受  [n] 拒绝  [e] 编辑后接受  [a] 全部接受（本轮）  ?", highlight=False)

    try:
        answer = _prompt("").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = "n"

    if answer == "a":
        _HIL_AUTO_ACCEPT = True
        return True, new_content
    if answer == "e":
        import tempfile
        suffix = Path(filename).suffix or ".txt"
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=suffix, delete=False, encoding="utf-8"
        ) as tf:
            tf.write(new_content)
            tmp_path = tf.name
        try:
            import subprocess as _sp
            _sp.call(_detect_editor() + [tmp_path])
            edited = Path(tmp_path).read_text(encoding="utf-8")
        except Exception as e:
            console.print(f"[HIL] 编辑器错误: {e}", style="red", highlight=False)
            edited = new_content
        finally:
            Path(tmp_path).unlink(missing_ok=True)
        return True, edited
    if answer == "n":
        console.print(f"[HIL] 已跳过: {filename}", highlight=False)
        return False, new_content
    return True, new_content


# ---------- #26 Linter ----------

def run_linter():
    """根据 _PROJECT_TYPE 运行对应的 Linter，有错误返回结果 dict，否则返回 None"""
    import shutil
    if not _PROJECT_TYPE:
        return None
    
    cmd = None
    if _PROJECT_TYPE == "Python":
        if shutil.which("ruff"):
            cmd = "ruff check ."
        elif shutil.which("mypy"):
            cmd = "mypy ."
        else:
            import sys
            cmd = f'"{sys.executable}" -m ruff check .'
    elif _PROJECT_TYPE == "Node.js":
        cmd = "npm run lint --if-present"
    elif _PROJECT_TYPE == "Go":
        if shutil.which("go"):
            cmd = "go vet ./..."
    elif _PROJECT_TYPE == "Rust":
        if shutil.which("cargo"):
            cmd = "cargo clippy"
    elif _PROJECT_TYPE == "Java/Maven":
        if shutil.which("mvn"):
            cmd = "mvn checkstyle:check"
            
    if not cmd:
        return None
        
    result = execute_command(cmd)
    if result.get("returncode", 0) == 0:
        return None
    return result

# ---------- #27 项目类型检测 ----------

def detect_project_type():
    """扫描 workspace 目录识别项目类型，返回 (type_str, test_cmd)。
    优先读取配置文件中用户自定义的 test 命令，而不是硬编码 pytest/npm test。"""
    from pathlib import Path
    from config import WORKSPACE_DIR
    ws = Path(WORKSPACE_DIR)
    if not ws.exists():
        return None, None
    all_names = {f.name for f in ws.rglob("*") if f.is_file()}

    # ---------- Python ----------
    if any(n in all_names for n in ("requirements.txt", "pyproject.toml", "setup.py", "setup.cfg")) \
            or any(n.endswith(".py") for n in all_names):
        test_cmd = _detect_python_test_cmd(ws)
        return "Python", test_cmd

    # ---------- Node.js ----------
    if "package.json" in all_names:
        test_cmd = _detect_node_test_cmd(ws)
        return "Node.js", test_cmd

    # ---------- Go ----------
    if "go.mod" in all_names:
        return "Go", "go test ./..."

    # ---------- Rust ----------
    if "Cargo.toml" in all_names:
        return "Rust", "cargo test"

    # ---------- Java/Maven ----------
    if "pom.xml" in all_names:
        return "Java/Maven", "mvn test"

    return None, None


def _detect_python_test_cmd(ws: "Path") -> str:
    """从 pyproject.toml / tox.ini / setup.cfg 读取 Python 项目的真实测试命令。"""
    import shutil

    # 1. pyproject.toml — 检查 [tool.pytest.ini_options] 或 [tool.uv] / [tool.poetry]
    pyproject = ws / "pyproject.toml"
    if pyproject.exists():
        try:
            content = pyproject.read_text(encoding="utf-8")
            # uv 项目：优先用 uv run pytest
            if "[tool.uv]" in content or (ws / "uv.lock").exists():
                if shutil.which("uv"):
                    return "uv run pytest"
            # poetry 项目
            if "[tool.poetry]" in content or (ws / "poetry.lock").exists():
                if shutil.which("poetry"):
                    return "poetry run pytest"
            # 有 pytest 配置就用 pytest
            if "[tool.pytest" in content or "[pytest]" in content:
                return "pytest"
        except Exception:
            pass

    # 2. tox.ini — 有 tox 就用 tox
    if (ws / "tox.ini").exists() and shutil.which("tox"):
        return "tox"

    # 3. Makefile 里有 test target
    makefile = ws / "Makefile"
    if makefile.exists():
        try:
            mk = makefile.read_text(encoding="utf-8", errors="replace")
            if "test:" in mk or "test :" in mk:
                return "make test"
        except Exception:
            pass

    # 4. 默认 pytest（最通用）
    if shutil.which("pytest"):
        return "pytest"
    # 没有全局 pytest 时用 python -m pytest
    return "python -m pytest"


def _detect_node_test_cmd(ws: "Path") -> str:
    """从 package.json scripts.test 读取 Node 项目的真实测试命令。"""
    import json as _json
    import shutil
    pkg = ws / "package.json"
    if pkg.exists():
        try:
            data = _json.loads(pkg.read_text(encoding="utf-8"))
            scripts = data.get("scripts", {})
            # 优先用明确的 test script
            if scripts.get("test"):
                # 检测包管理器
                if (ws / "yarn.lock").exists() and shutil.which("yarn"):
                    return "yarn test"
                if (ws / "pnpm-lock.yaml").exists() and shutil.which("pnpm"):
                    return "pnpm test"
                return "npm test"
        except Exception:
            pass
    return "npm test"

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
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_webpage",
            "description": "读取网页内容，提取正文文本，截断到3000字符。用于查询外部API文档等。",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "要读取的网页URL"}
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_docs",
            "description": "使用 DuckDuckGo 搜索文档，返回前3条结果的标题+摘要+URL",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "append_to_file",
            "description": "向指定文件末尾追加内容",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "文件名（相对于workspace）"},
                    "content": {"type": "string", "description": "要追加的内容"}
                },
                "required": ["filename", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "find_references",
            "description": "在指定目录下递归搜索所有 .py 文件中的符号引用（排除定义行）",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "要查找的符号名称"},
                    "path": {"type": "string", "description": "搜索起始路径（相对于workspace，默认 \".\"）"}
                },
                "required": ["symbol"]
            }
        }
    }
]

LLM_TIMEOUT_SEC = 120
LLM_MAX_RETRIES_PER_MODEL = 3  # 每个模型对 429/5xx 的退避重试次数


def _is_transient_error(exc) -> bool:
    """判断是否为应退避重试的瞬时错误（429 / 5xx / 连接错误）"""
    try:
        from openai import APIConnectionError, APITimeoutError, RateLimitError, InternalServerError
        if isinstance(exc, (APIConnectionError, APITimeoutError, RateLimitError, InternalServerError)):
            return True
    except ImportError:
        pass
    status = getattr(exc, "status_code", None) or getattr(exc, "http_status", None)
    if isinstance(status, int) and (status == 429 or 500 <= status < 600):
        return True
    return False


def call_llm(messages, tools=None, tool_choice=None, response_format=None, stream: bool | None = None):
    """尝试QUALITY_CASCADE中的模型，依次降级调用。每模型对 429/5xx 指数退避重试。
    在子线程中执行，每100ms检查一次ESC中断。
    stream=True 且 tools=None 时启用流式输出，实时打印 token。"""
    # 无工具调用时默认开启流式（response_format=json_object 时强制关闭，JSON 流解析复杂）
    use_stream = (stream is True) or (
        stream is None and tools is None and response_format is None
    )

    result_holder = [None]
    exc_holder = [None]

    def _worker():
        for model in QUALITY_CASCADE:
            backoff = 1.0
            for attempt in range(LLM_MAX_RETRIES_PER_MODEL):
                try:
                    kwargs = {"model": model, "messages": messages, "timeout": LLM_TIMEOUT_SEC}
                    if tools is not None:
                        kwargs["tools"] = tools
                    if tool_choice is not None:
                        kwargs["tool_choice"] = tool_choice
                    if response_format is not None:
                        kwargs["response_format"] = response_format
                    if use_stream:
                        kwargs["stream"] = True
                        result_holder[0] = _handle_stream(
                            client.chat.completions.create(**kwargs), model
                        )
                    else:
                        result_holder[0] = client.chat.completions.create(**kwargs)
                    return
                except Exception as e:
                    exc_holder[0] = e
                    if _is_transient_error(e) and attempt < LLM_MAX_RETRIES_PER_MODEL - 1:
                        deadline = _time.time() + backoff
                        while _time.time() < deadline:
                            if interrupt.is_interrupted():
                                return
                            _time.sleep(0.1)
                        backoff *= 2
                        continue
                    break

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    while t.is_alive():
        if interrupt.is_interrupted():
            raise interrupt.Interrupted()
        t.join(timeout=0.1)

    if result_holder[0] is None:
        raise RuntimeError(f"所有模型调用均失败: {exc_holder[0]}")

    # #62 统计 Token
    res = result_holder[0]
    if hasattr(res, "usage") and res.usage:
        p = res.usage.prompt_tokens or 0
        c = res.usage.completion_tokens or 0
        _last_request_tokens["prompt"] = p
        _last_request_tokens["completion"] = c
        _session_tokens["prompt"] += p
        _session_tokens["completion"] += c

    return res


def _handle_stream(stream_iter, model: str):
    """消费流式响应，实时打印 token，返回与非流式兼容的伪 response 对象。"""
    from types import SimpleNamespace
    collected_content = []
    usage_data = None

    for chunk in stream_iter:
        if interrupt.is_interrupted():
            raise interrupt.Interrupted()
        if not chunk.choices:
            # 某些模型在最后一个 chunk 放 usage
            if hasattr(chunk, "usage") and chunk.usage:
                usage_data = chunk.usage
            continue
        delta = chunk.choices[0].delta
        if delta and delta.content:
            sys.stdout.write(delta.content)
            sys.stdout.flush()
            collected_content.append(delta.content)

    # 流结束后换行
    if collected_content:
        sys.stdout.write("\n")
        sys.stdout.flush()

    full_content = "".join(collected_content)

    # 构造与非流式兼容的伪 response 对象
    from types import SimpleNamespace
    message = SimpleNamespace(
        content=full_content,
        tool_calls=None,
        role="assistant",
    )
    choice = SimpleNamespace(message=message, finish_reason="stop")
    usage = usage_data or SimpleNamespace(prompt_tokens=0, completion_tokens=len(full_content) // 4)
    response = SimpleNamespace(choices=[choice], usage=usage, model=model)
    return response


def show_stats():
    """📊 显示 Token 消耗统计"""
    p_last = _last_request_tokens["prompt"]
    c_last = _last_request_tokens["completion"]
    p_total = _session_tokens["prompt"]
    c_total = _session_tokens["completion"]
    total = p_total + c_total
    
    # 费用计算 (per 1M tokens)
    cost = (p_total / 1_000_000 * TOKEN_PRICE_INPUT) + (c_total / 1_000_000 * TOKEN_PRICE_OUTPUT)
    
    console.print("\n📊 [bold]Token 消耗统计[/bold]", highlight=False)
    console.print(f"  本次请求: prompt={p_last:,}  completion={c_last:,}", highlight=False)
    console.print(f"  会话累计: prompt={p_total:,}  completion={c_total:,}  total={total:,}", highlight=False)
    console.print(f"  预估费用: [green]${cost:.4f}[/green] (按 ${TOKEN_PRICE_INPUT}/1M input, ${TOKEN_PRICE_OUTPUT}/1M output)", highlight=False)


_ARCHITECT_ROLE = """【角色：架构师 Agent】
你专注于分析需求和制定实现计划。
职责：只输出计划，不写代码；重点考虑风险点和文件依赖顺序；确保计划完整可执行。
"""

_CODER_ROLE = """【角色：码农 Agent】
你专注于根据计划生成高质量代码。
职责：严格按计划执行，不自行发挥额外功能；注重代码质量和边界处理；已有文件用 replace_in_file 精确修改，不得整体重写。
"""

_REVIEWER_ROLE = """【角色：代码审查 Agent】
你专注于审查已生成的代码。
职责：检查代码是否满足原始需求，是否存在潜在的边界漏洞，以及是否符合项目规则。
输入：本次修改的文件内容和原始需求。
输出：必须严格返回 JSON 格式，包含 "approved" (bool), "issues" (字符串数组), "suggestions" (字符串数组)。
"""

_TESTER_ROLE = """【角色：测试 Agent】
你专注于分析测试失败原因并指导修复。
职责：只关注测试结果和错误信息；给出精准、最小化的修复建议；避免引入不相关改动。
"""

def _get_project_rules():
    rules_path = Path(WORKSPACE_DIR) / ".agent_rules"
    if rules_path.exists():
        try:
            content = rules_path.read_text(encoding="utf-8").strip()
            if content:
                return f"\n项目规则：\n{content}\n"
        except Exception:
            pass
    return ""

def plan(requirement):
    """制定计划：生成文件列表和测试命令"""
    import platform

    def _get_project_rules():
        rules_path = Path(WORKSPACE_DIR) / ".agent_rules"
        if rules_path.exists():
            try:
                content = rules_path.read_text(encoding="utf-8").strip()
                if content:
                    return f"\n项目规则：\n{content}\n"
            except Exception:
                pass
        return ""

    def _generate_tree():
        ws = Path(WORKSPACE_DIR)
        ignore_dirs = {".git", "__pycache__", "node_modules", ".yansh", ".pytest_cache", "venv"}
        def walk(path, prefix="", level=0):
            if level > 2:
                return []
            lines = []
            try:
                entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name))
            except PermissionError:
                return []
            entries = [e for e in entries if e.name not in ignore_dirs]
            for i, entry in enumerate(entries):
                is_last = i == len(entries) - 1
                marker = "└── " if is_last else "├── "
                lines.append(f"{prefix}{marker}{entry.name}")
                if entry.is_dir():
                    ext = "    " if is_last else "│   "
                    lines.extend(walk(entry, prefix + ext, level + 1))
            return lines
        return "当前项目结构：\n" + "\n".join(walk(ws)) + "\n"

    # 检测系统并生成命令提示
    system_name = platform.system()
    if system_name == "Windows":
        cmd_hint = "当前运行环境是 Windows，使用 Windows 命令：查看文件用 type，列目录用 dir，禁止使用 cat、ls、grep。"
    else:
        cmd_hint = "当前运行环境是 Linux/Mac，使用 Unix 命令：查看文件用 cat，列目录用 ls。"

    # 先获取当前 workspace 文件结构，注入到 LLM 上下文中避免重复创建
    tree_output = _generate_tree()
    project_rules = _get_project_rules()
    ws_files = list_files()
    files_list = "\n".join(f"- {f}" for f in ws_files.get("files", []))
    project_hint = (
        f"\n当前项目类型：{_PROJECT_TYPE}，默认测试命令：{_PROJECT_TEST_CMD}。"
        if _PROJECT_TYPE else ""
    )
    console.print("[Agent: Architect]", highlight=False)
    system_prompt = f"""{_ARCHITECT_ROLE}{project_rules}
你是一个代码规划助手。根据用户需求，返回JSON格式的计划，包含：
- files：数组，每个元素为 {{"filename": "文件名", "description": "修改意图/需求说明"}}；对于已有文件只需填写修改意图，不要重复列出完整内容
- test_command：测试命令

注意目录结构：实现文件放workspace/根目录（如add.py），测试文件必须放workspace/tests/目录（如tests/test_add.py）。
filename 字段只填相对路径，不要加 "workspace/" 前缀，正确示例：hello.py、tests/test_hello.py；错误示例：workspace/hello.py。
test_command 禁止使用 python -c 内联执行（会被安全策略拦截），应使用 python filename.py 方式。

{cmd_hint}{project_hint}

{tree_output}

当前workspace已有文件列表：
{files_list if files_list else "(空)"}

注意：不要重复创建已有文件，尽量基于已有文件做增量修改。对已有文件只描述要追加/修改什么。"""
    # #50 若有待注入图片，构造 vision content（消费后清空）
    user_text = f"需求：{requirement}"
    if _pending_images:
        user_content = _build_vision_content(user_text, _pending_images)
        _pending_images.clear()
    else:
        user_content = user_text
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content}
    ]
    response = call_llm(messages, response_format={"type": "json_object"})
    content = response.choices[0].message.content
    if not content:
        content = "{}"
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return {"files": [], "test_command": ""}

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
{_get_project_rules()}
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
                    args_str = tool_call.function.arguments
                    if not args_str:
                        args_str = "{}"
                    try:
                        func_args = json.loads(args_str)
                    except json.JSONDecodeError as e:
                        console.print(f"[警告] 工具调用参数解析失败: {e}", highlight=False)
                        msgs.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": func_name,
                            "content": json.dumps({"error": f"Invalid JSON in arguments: {str(e)}"}, ensure_ascii=False)
                        })
                        continue

                    if func_name == "write_file":
                        fname = func_args.get("filename", "")
                        for _pfx in (WORKSPACE_DIR + "/", WORKSPACE_DIR + "\\"):
                            if fname.startswith(_pfx):
                                fname = fname[len(_pfx):]
                                func_args["filename"] = fname
                                break
                        import os as _os2
                        overwrite = _os2.path.exists(_os2.path.join(WORKSPACE_DIR, fname))
                        hil_on = _cfg("human_in_loop") and not _BATCH_MODE
                        if hil_on:
                            old_content = ""
                            if overwrite:
                                _r = read_file(fname)
                                old_content = _r.get("content", "") if "error" not in _r else ""
                            new_content = func_args.get("content", "")
                            accept, final_content = _hil_confirm(fname, old_content, new_content, not overwrite)
                            if not accept:
                                result = {"error": "用户已跳过此写入"}
                            else:
                                func_args["content"] = final_content
                                result = write_file(**func_args)
                                console.print(f"写入{'(覆盖)' if overwrite else ''} {fname}")
                        elif mode == "auto" and overwrite:
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
                        hil_on = _cfg("human_in_loop") and not _BATCH_MODE
                        rfname = func_args.get("filename", "")
                        if hil_on:
                            _r = read_file(rfname)
                            old_content = _r.get("content", "") if "error" not in _r else ""
                            old_str = func_args.get("old_str", "")
                            new_str = func_args.get("new_str", "")
                            new_content = old_content.replace(old_str, new_str, 1) if old_str in old_content else old_content
                            accept, final_content = _hil_confirm(rfname, old_content, new_content)
                            if not accept:
                                result = {"error": "用户已跳过此修改"}
                            else:
                                if final_content != new_content:
                                    result = write_file(rfname, final_content)
                                    if "success" in result:
                                        result = {"success": f"文件 {rfname} 替换成功", "filename": rfname}
                                else:
                                    result = replace_in_file(**func_args)
                                if "success" in result:
                                    console.print(f"replace_in_file OK: {result.get('filename')}", highlight=False)
                                    _task_files_modified.append(rfname)
                                else:
                                    console.print(f"替换失败: {result.get('error')}", highlight=False)
                        else:
                            _show_diff(rfname, func_args.get("old_str", ""), func_args.get("new_str", ""))
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
                                    _task_files_modified.append(rfname)
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
                    elif func_name == "fetch_webpage":
                        result = fetch_webpage(**func_args)
                    elif func_name == "search_docs":
                        result = search_docs(**func_args)
                    elif func_name == "append_to_file":
                        result = append_to_file(**func_args)
                        if "success" in result:
                            _task_files_modified.append(func_args.get("filename", ""))
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

def review(requirement, modified_files):
    """代码审查阶段"""
    console.print("[Agent: Reviewer]", highlight=False)
    console.print("开始审查代码...")
    
    file_contents = []
    for filename in dict.fromkeys(modified_files):
        if not filename:
            continue
        content = read_file(filename).get("content", "")
        if content:
            file_contents.append(f"--- {filename} ---\n{content}")
            
    if not file_contents:
        return {"approved": True, "issues": [], "suggestions": []}
        
    def _get_project_rules():
        rules_path = Path(WORKSPACE_DIR) / ".agent_rules"
        if rules_path.exists():
            try:
                content = rules_path.read_text(encoding="utf-8").strip()
                if content:
                    return f"\n项目规则：\n{content}\n"
            except Exception:
                pass
        return ""
        
    sys_prompt = f"{_REVIEWER_ROLE}{_get_project_rules()}"
    user_content = f"原始需求：{requirement}\n\n修改的文件内容：\n" + "\n\n".join(file_contents)
    
    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_content}
    ]
    
    try:
        response = call_llm(messages, response_format={"type": "json_object"})
        content = response.choices[0].message.content or ""
        return json.loads(content)
    except json.JSONDecodeError as e:
        return {
            "approved": False,
            "issues": [f"review_error: LLM 返回非 JSON（{e}）"],
            "suggestions": [],
        }
    except Exception as e:
        return {
            "approved": False,
            "issues": [f"review_error: {e}"],
            "suggestions": [],
        }

def fix(test_result, plan):
    """根据测试错误修复代码（多轮工具调用）"""
    console.print("[Agent: Tester]", highlight=False)
    console.print("开始修复代码...")
    
    # 优先使用 stderr，为空回落到 stdout；关键栈常在末尾，采用 head + tail 截断
    stderr = test_result.get("stderr", "")
    stdout = test_result.get("stdout", "")
    raw = stderr or stdout or "未知错误"

    HEAD, TAIL = 800, 2000
    if len(raw) > HEAD + TAIL:
        error_info = (
            raw[:HEAD]
            + f"\n... (中间省略 {len(raw) - HEAD - TAIL} 字符) ...\n"
            + raw[-TAIL:]
        )
    else:
        error_info = raw
    
    content = f"测试失败！\n错误输出：\n{error_info}\n\n计划：{json.dumps(plan)}"
    
    messages = [
        {"role": "system", "content": f"{_TESTER_ROLE}\n{_get_project_rules()}你是代码修复助手。根据错误信息精准修复代码，优先使用 replace_in_file 做最小化修改，必要时才用 write_file 重写整个文件。"},
        {"role": "user", "content": content}
    ]
    
    while True:
        response = call_llm(messages, tools=TOOLS, tool_choice="auto")
        response_message = response.choices[0].message
        # 显式序列化为 dict，避免 Pydantic 对象在不同 SDK 版本下序列化异常
        messages.append({
            "role": "assistant",
            "content": response_message.content,
            "tool_calls": [tc.model_dump() for tc in response_message.tool_calls] if response_message.tool_calls else None,
        })

        if response_message.tool_calls:
            console.print(f"执行 {len(response_message.tool_calls)} 个修复操作...")
            for tool_call in response_message.tool_calls:
                func_name = tool_call.function.name
                args_str = tool_call.function.arguments
                if not args_str:
                    args_str = "{}"
                try:
                    func_args = json.loads(args_str)
                except json.JSONDecodeError as e:
                    console.print(f"[警告] 工具调用参数解析失败: {e}", highlight=False)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": func_name,
                        "content": json.dumps({"error": f"Invalid JSON in arguments: {str(e)}"}, ensure_ascii=False)
                    })
                    continue
                
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
                elif func_name == "append_to_file":
                    result = append_to_file(**func_args)
                    if "success" in result:
                        _task_files_modified.append(func_args.get("filename", ""))
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
    global _pending_images
    # #57 显示已加载文件状态（处理当前指令前）
    if _context_files:
        console.print(f"[上下文] 已加载文件: {', '.join(_context_files.keys())}", highlight=False)
    # #50 解析图片指令（@image / @paste）
    requirement, _img_list = _parse_image_cmds(requirement)
    _pending_images = _img_list
    # #57 解析 @add_file / @clear_files，再注入 @file 语法
    requirement = _parse_context_cmds(requirement)
    requirement = _process_at_files(requirement)
    ctx_block = _get_context_files_block()
    if ctx_block:
        requirement = requirement + "\n\n" + ctx_block

    try:
        res = _run(requirement, mode)
        # #61 任务失败自动保存回放
        if not res["success"]:
            create_replay_package(res["test_result"].get("stderr", "任务失败"))
        return res
    except interrupt.Interrupted:
        return _interrupted_result()
    except Exception as e:
        # 异常退出也保存回放
        create_replay_package(str(e))
        raise


def _run(requirement, mode):
    global _HIL_AUTO_ACCEPT
    _HIL_AUTO_ACCEPT = False  # 每轮任务重置"全部接受"标志
    os.makedirs(WORKSPACE_DIR, exist_ok=True)  # 兜底：新目录首次运行时 workspace/ 可能不存在
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
    
    # #48 Review Agent：在阶段2和阶段3之间进行代码审查
    review_attempts = 0
    max_review_attempts = 3
    review_passed = False
    last_review_result = None
    while review_attempts < max_review_attempts:
        review_result = review(original_requirement, _task_files_modified)
        last_review_result = review_result
        if review_result.get("approved"):
            console.print("代码审查通过！")
            review_passed = True
            break
        else:
            console.print(f"代码审查未通过 (尝试 {review_attempts + 1}/{max_review_attempts})")
            issues = review_result.get("issues", [])
            suggestions = review_result.get("suggestions", [])
            for issue in issues:
                console.print(f"- 问题: {issue}", style="red")
            for sug in suggestions:
                console.print(f"- 建议: {sug}", style="yellow")

            # 使用 fix 逻辑修改代码
            mock_test_result = {
                "returncode": 1,
                "stderr": "代码审查未通过：\n" + "\n".join(issues) + "\n建议：\n" + "\n".join(suggestions),
                "stdout": ""
            }
            fix(mock_test_result, plan_result)
            review_attempts += 1

    # 达到上限仍未通过：降级处理 —— 记录到日志，HIL 模式下征询用户
    if not review_passed:
        _current_task_log["review_failed"] = True
        _current_task_log["review_last"] = last_review_result or {}
        console.print("[审查未通过] 已达到最大尝试次数", style="bold yellow")
        if _cfg("human_in_loop"):
            ans = _prompt("是否继续进入测试阶段？(y/n) ", default="n")
            if ans != "y":
                cleanup_snapshot(current_snapshot)
                finish_task_log(False, 0, {"returncode": -1, "stdout": "", "stderr": "审查未通过，用户终止"})
                return {"success": False, "test_result": {"returncode": -1, "stdout": "", "stderr": "审查未通过，用户终止"}}
        else:
            console.print("继续进入测试阶段（非 HIL 模式）", style="yellow")

    console.print("\n阶段3：测试与修复")
    attempts = 0
    test_result = None
    max_attempts = _cfg("max_attempts") or 3

    # #42 如果 workspace 中没有测试文件，自动生成
    ws = Path(WORKSPACE_DIR)
    _ignore = {".yansh", ".git", "__pycache__", "node_modules", "venv", "workspace"}
    has_tests = bool([
        f for f in ws.rglob("test_*.py")
        if not any(part in _ignore for part in f.relative_to(ws).parts)
    ] + [
        f for f in ws.rglob("*_test.py")
        if not any(part in _ignore for part in f.relative_to(ws).parts)
    ])
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
                _args_str = tc.function.arguments
                if not _args_str:
                    _args_str = "{}"
                try:
                    args = json.loads(_args_str)
                except json.JSONDecodeError:
                    msgs.append({"tool_call_id": tc.id, "role": "tool", "name": fname,
                                 "content": json.dumps({"error": "Invalid JSON in arguments"}, ensure_ascii=False)})
                    continue
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


# 明确是闲聊的关键词规则（规则优先，避免每次都调 LLM）
_CHAT_PATTERNS = {"你好", "hi", "hello", "谢谢", "thanks", "thank you", "再见", "bye",
                  "帮我", "解释", "什么是", "如何", "为什么", "介绍", "说说"}

def classify_input(user_input):
    """判断用户输入是新任务还是闲聊。规则优先，歧义时才调 LLM。"""
    stripped = user_input.strip().lower()
    # 极短输入（≤5字）直接视为闲聊
    if len(stripped) <= 5:
        return "chat"
    # 关键词匹配
    if any(kw in stripped for kw in _CHAT_PATTERNS):
        return "chat"
    # 明显任务关键词
    task_kws = {"写", "创建", "实现", "修改", "修复", "生成", "添加", "删除", "重构",
                "create", "write", "implement", "fix", "add", "remove", "refactor", "build"}
    if any(kw in stripped for kw in task_kws):
        return "task"
    # 歧义时调 LLM
    messages = [
        {"role": "system", "content": "判断以下输入是'新任务'还是'闲聊'，只回复 task 或 chat。"},
        {"role": "user", "content": f"输入：{user_input}"}
    ]
    try:
        response = call_llm(messages)
        result = response.choices[0].message.content.strip().lower()
        return "task" if "task" in result else "chat"
    except Exception:
        return "task"  # 调用失败时保守地当作任务

def chat(user_input):
    """闲聊模式，LLM 直接回复，控制在 100 字以内"""
    global _pending_images
    # #57 显示已加载文件状态（处理当前指令前）
    if _context_files:
        console.print(f"[上下文] 已加载文件: {', '.join(_context_files.keys())}", highlight=False)
    # #50 解析图片指令（@image / @paste）
    user_input, _img_list = _parse_image_cmds(user_input)
    _pending_images = _img_list
    # #57 解析 @add_file / @clear_files，再注入 @file 语法
    user_input = _parse_context_cmds(user_input)
    user_input = _process_at_files(user_input)
    ctx_block = _get_context_files_block()
    if ctx_block:
        user_input = user_input + "\n\n" + ctx_block

    messages = [
        {"role": "system", "content": "你是一个友好的助手。简洁回复用户，控制在 100 字以内。"}
    ]

    # 添加最近5轮历史
    messages.extend(get_recent_history())

    # #50 构建 vision content（图片仅本轮携带，不存入历史）
    user_text = user_input  # 纯文本用于历史保存
    if _pending_images:
        msg_content = _build_vision_content(user_input, _pending_images)
        _pending_images = []
    else:
        msg_content = user_input
    messages.append({"role": "user", "content": msg_content})

    response = call_llm(messages)
    assistant_reply = response.choices[0].message.content

    # 保存到历史（仅文本，不含图片 base64）
    add_to_history(user_text, assistant_reply)

    return assistant_reply
