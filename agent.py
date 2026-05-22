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
    OPENROUTER_API_KEY, OPENROUTER_BASE_URL, WORKSPACE_DIR, get_config,
    get_model_price,
)
from tools import (
    write_file, read_file, execute_command, list_files, replace_in_file,
    get_symbol_definition, search_in_files, move_file, apply_patch,
    list_symbols, replace_symbol, fetch_webpage, search_docs, append_to_file,
    find_references, glob_files, git_diff, git_log, workspace_symbols,
    directory_summary, delete_file, task_complete,
    update_plan_draft, exit_plan_mode_signal,
)
import interrupt
import tools as _tools_mod
import snapshot as _snapshot_mod
from snapshot import (
    create_snapshot, restore_snapshot, cleanup_snapshot,
    _backup_file_if_needed, _gc_old_snapshots, get_latest_snapshot,
)
import hil as _hil_mod
from hil import show_diff as _show_diff, hil_confirm as _hil_confirm
import task_log as _task_log_mod
from task_log import (
    init_task_log, finish_task_log, show_recent_logs, get_last_task_log,
)
import linter as _linter_mod
from linter import detect_project_type
import llm_client as _llm_mod
from tools_schema import TOOLS, READONLY_TOOL_NAMES
from llm_client import (
    client, _ica_client, _get_ica_client, _get_gemini_client,
    _is_gemini, _is_claude, _client_for, _call_single_model,
    _is_transient_error, call_llm, _StreamToolCall, _handle_stream,
    show_stats, LLM_TIMEOUT_SEC, LLM_MAX_RETRIES_PER_MODEL,
    get_session_total_tokens,
)

console = Console()

# Token 统计在 llm_client 模块（_session_tokens_by_model / _last_request_tokens）

# #40 批处理模式标志
_BATCH_MODE = False

# #27 项目类型（由 main.py 调用 detect_project_type() 后写入）
_PROJECT_TYPE = None
_PROJECT_TEST_CMD = None

# #61 回放目录（任务日志在 task_log 模块；快照在 snapshot 模块）
_YANSH_DIR     = Path(WORKSPACE_DIR) / ".yansh"
_LOG_DIR       = _YANSH_DIR / "logs"  # 仅 create_replay_package 引用
_REPLAY_DIR    = _YANSH_DIR / "replay"

# #37 当前任务的快照引用，code()/fix()/_auto_generate_tests 在写入前查它做增量备份
_CURRENT_SNAPSHOT: dict | None = None

# P2 #7 Plan Mode：会话级状态。state.Session 镜像这三个字段
_PLAN_MODE: bool = False
_PLAN_DRAFT: str = ""
_PLAN_HISTORY: list = []   # plan 模式独立对话历史（不混入 conversation_history）

# 对话历史管理
conversation_history = []
MAX_HISTORY = 20
CHAT_CONTEXT_ROUNDS = 5
COMPRESS_MODEL = "claude-haiku-4-5"  # 通过 ICA 网关；llama 旧值在 ICA 上必失败

# #57 session 级别上下文文件
_context_files: dict = {}  # {display_path: content}
_MAX_CONTEXT_FILE_SIZE = 100 * 1024  # 100KB

# #58 HIL 状态在 hil 模块；agent._run() 通过 _hil_mod.reset_auto_accept() 重置

# #50 多模态视觉：当前轮次待注入的图片，plan()/chat() 消费后清空
_pending_images: list = []

# #P0_3 错误恢复闭环：fix/audit 软上限 + token 预算
# - 软上限：原硬上限翻倍。LLM 用 task_complete 主动早退是正常路径；硬退是兜底。
# - token 预算：进入 loop 时记起点，超阈值往 messages 注一条 system 提醒"快收尾"，只警告一次。
_FIX_SOFT_LIMIT     = 12       # 原 6
_FIX_TOKEN_BUDGET   = 60_000   # fix loop token 增量预算
_AUDIT_SOFT_LIMIT   = 16       # 原 8
_AUDIT_TOKEN_BUDGET = 120_000  # audit loop token 增量预算


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


def _prompt(msg: str, default: str = "y") -> str:
    """批处理模式自动返回 default；交互模式调用 console.input"""
    if _BATCH_MODE:
        console.print(f"{msg}[batch: {default}]", highlight=False)
        return default
    try:
        return console.input(msg).strip().lower()
    except EOFError:
        return default


REVIEW_MODEL = None  # None = 跟随写代码模型


# ---------- LLM 结构化输出 schema（plan / review 用）----------

from pydantic import BaseModel, ConfigDict, ValidationError, Field
from typing import List, Optional, Union


class PlanFile(BaseModel):
    """plan() 输出中单个文件条目"""
    model_config = ConfigDict(extra="allow")  # LLM 偶尔加额外字段，不当成错误
    filename: str
    intent: Optional[str] = ""
    description: Optional[str] = ""


class PlanResult(BaseModel):
    """plan() 输出 schema。允许 files 元素是 dict 或裸字符串。"""
    model_config = ConfigDict(extra="allow")
    files: List[Union[PlanFile, str]] = Field(default_factory=list)
    test_command: str = ""


class ReviewResult(BaseModel):
    """review() 输出 schema"""
    model_config = ConfigDict(extra="allow")
    approved: bool
    issues: List[Union[str, dict]] = Field(default_factory=list)
    suggestions: List[Union[str, dict]] = Field(default_factory=list)


def _truncate(text: str, head: int = 400, tail: int = 200) -> str:
    """截断长文本以便日志展示"""
    if not text or len(text) <= head + tail:
        return text or ""
    return text[:head] + f"\n... (省略 {len(text) - head - tail} 字符) ...\n" + text[-tail:]


def _log_json_failure(stage: str, raw_content: str, error: str) -> None:
    """JSON 解析或 schema 校验失败时统一打印（替换原本的静默 pass）"""
    console.print(
        f"[警告] {stage} 输出 JSON 校验失败：{error}",
        style="yellow", highlight=False,
    )
    console.print(
        f"[原始内容] {_truncate(raw_content)}",
        style="yellow", highlight=False,
    )


def _call_with_json_retry(stage, messages, parser_fn,
                          response_format=None, stream=None,
                          extra_call_kwargs=None):
    """LLM 调用 + JSON 解析失败自动 retry 1 次。

    parser_fn(content) 必须返回 (ok: bool, data, error_msg: str|None)。
      - ok=True: 解析成功，data 是最终返回值
      - ok=False: 解析失败，error_msg 描述原因；返回 None data 时表示降级处理由 caller 决定

    第一次失败时把"原始内容 + 错误描述"作为 user 消息追加再调一次；仍失败则 log + 返回 parser_fn 的降级数据。
    """
    extra = dict(extra_call_kwargs or {})
    kwargs = {"messages": messages}
    if response_format is not None:
        kwargs["response_format"] = response_format
    if stream is not None:
        kwargs["stream"] = stream
    kwargs.update(extra)

    response = call_llm(**kwargs)
    content = response.choices[0].message.content or ""
    ok, data, err = parser_fn(content)
    if ok:
        return data

    # 失败 → retry 1 次
    console.print(f"[{stage}] JSON 解析失败，自动 retry 1 次：{err}",
                  style="yellow", highlight=False)
    fix_prompt = (
        f"上一轮你的输出无法被解析为合法 JSON。请仅输出合法 JSON（无多余说明、无 markdown 围栏）。\n"
        f"错误：{err}\n"
        f"前次输出（截断）：\n{_truncate(content)}"
    )
    retry_messages = list(messages) + [
        {"role": "assistant", "content": content},
        {"role": "user", "content": fix_prompt},
    ]
    retry_kwargs = dict(kwargs)
    retry_kwargs["messages"] = retry_messages
    response2 = call_llm(**retry_kwargs)
    content2 = response2.choices[0].message.content or ""
    ok2, data2, err2 = parser_fn(content2)
    if ok2:
        return data2
    # 两次都失败：log raw + 返回降级值
    _log_json_failure(stage, content2, f"retry 后仍失败：{err2}")
    return data2


def _extract_json(text: str) -> str:
    """从 LLM 响应中提取 JSON 字符串（兼容 markdown 代码块、顶层数组、顶层对象）"""
    import re
    text = text.strip()
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if m:
        return _sanitize_json_strings(m.group(1).strip())
    # 顶层数组 / 顶层对象：取最早出现的那个边界对
    obj_start = text.find("{")
    obj_end = text.rfind("}")
    arr_start = text.find("[")
    arr_end = text.rfind("]")
    obj_ok = obj_start != -1 and obj_end > obj_start
    arr_ok = arr_start != -1 and arr_end > arr_start
    # 数组开头早于对象开头时，优先按数组截取
    if arr_ok and (not obj_ok or arr_start < obj_start):
        return _sanitize_json_strings(text[arr_start:arr_end + 1])
    if obj_ok:
        return _sanitize_json_strings(text[obj_start:obj_end + 1])
    return text


def _sanitize_json_strings(text: str) -> str:
    """将 JSON 字符串值内的裸控制字符（换行、制表等）替换为合法转义序列"""
    result = []
    in_string = False
    escape_next = False
    for ch in text:
        if escape_next:
            result.append(ch)
            escape_next = False
        elif ch == "\\":
            result.append(ch)
            escape_next = True
        elif ch == '"':
            result.append(ch)
            in_string = not in_string
        elif in_string and ch == "\n":
            result.append("\\n")
        elif in_string and ch == "\r":
            result.append("\\r")
        elif in_string and ch == "\t":
            result.append("\\t")
        else:
            result.append(ch)
    return "".join(result)


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

    summary = _do_compress(old_msgs)
    if summary is None:
        return
    # #14 用 system role 而非 assistant，避免摘要被当成 LLM 上轮回复
    conversation_history = [{"role": "system", "content": f"[历史摘要]\n{summary}"}] + recent_msgs
    console.print("[上下文已自动压缩]", highlight=False)

def _do_compress(old_msgs):
    """共用压缩逻辑：调 ICA Haiku，失败返回 None"""
    history_text = "\n".join(f"[{m['role']}]: {m['content']}" for m in old_msgs)
    prompt = (
        f"请将以下对话历史压缩成摘要：\n\n{history_text}\n\n"
        "严格按以下格式输出，不添加其他内容：\n"
        '【已完成任务】\n- ...\n\n【关键文件】\n- ...\n\n【未解决问题】\n- ...（没有则写"无"）'
    )
    try:
        response = _get_ica_client().chat.completions.create(
            model=COMPRESS_MODEL,
            messages=[{"role": "user", "content": prompt}],
            timeout=LLM_TIMEOUT_SEC,
        )
        return response.choices[0].message.content
    except Exception as e:
        console.print(f"[警告] 上下文压缩失败: {e}", highlight=False)
        return None

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
    summary = _do_compress(old_msgs)
    if summary is None:
        return
    conversation_history = [{"role": "system", "content": f"[历史摘要]\n{summary}"}] + recent_msgs
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
    snap_dir.mkdir(exist_ok=True)
    _ws = _get_workspace()
    for root, _, files in os.walk(_ws):
        if any(p in root for p in (".git", ".yansh", "__pycache__", "venv", "node_modules")):
            continue
        for f in files:
            src = Path(root) / f
            rel = src.relative_to(_ws)
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
        "model": get_config().get("model") or "unknown",
        "tokens_by_model": {k: dict(v) for k, v in _llm_mod._session_tokens_by_model.items()},
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

def _get_workspace() -> str:
    """每次调用都从 config 读取最新 WORKSPACE_DIR，确保 --cwd 生效后不使用旧值。
    本模块顶部 `from config import WORKSPACE_DIR` 是初次启动时的快照，--cwd 变更后该名不会同步。
    所有运行期路径拼接都应通过本函数读取。"""
    import config as _cfg_mod
    return _cfg_mod.WORKSPACE_DIR


def _reinit_paths():
    """--cwd 变更后重新初始化 agent / snapshot / task_log 中所有依赖 WORKSPACE_DIR 的模块级变量。"""
    global _YANSH_DIR, _LOG_DIR, _REPLAY_DIR, _HISTORY_FILE
    _wd = _get_workspace()
    _YANSH_DIR     = Path(_wd) / ".yansh"
    _LOG_DIR       = _YANSH_DIR / "logs"
    _REPLAY_DIR    = _YANSH_DIR / "replay"
    _HISTORY_FILE  = Path(_wd) / ".yansh_history.json"
    _snapshot_mod._reinit_paths()
    _task_log_mod._reinit_paths()


# ---------- #37 快照已迁移至 snapshot.py；#38 任务日志已迁移至 task_log.py ----------


# ---------- #7 统一工具分发 ----------

def _strip_workspace_prefix(args: dict, *keys: str):
    """剥离 args[key] 中误带的 'workspace/' 前缀（LLM 偶尔加上）"""
    for k in keys:
        v = args.get(k)
        if isinstance(v, str):
            _ws = _get_workspace()
            for _pfx in (_ws + "/", _ws + "\\"):
                if v.startswith(_pfx):
                    args[k] = v[len(_pfx):]
                    break


def _filter_tools(allowed_names):
    """从 TOOLS 中筛选出名字在 allowed_names 集合内的工具。审计/只读人格用。"""
    return [t for t in TOOLS if t["function"]["name"] in allowed_names]


def _dispatch_tool_call(tool_call, *, mode="auto", allow_hil=True, allow_confirm=True, snap=None) -> dict:
    """统一处理 LLM 返回的单个 tool_call，code()/fix() 共用。
    返回 {"name": str, "args": dict, "id": str, "result": dict}
    - mode: "auto" 时对覆盖/移动等弹用户确认；"code" 跳过确认
    - allow_hil: 是否启用 HIL 编辑确认（fix 阶段也可启用）
    - allow_confirm: 是否在 mode=auto 时弹覆盖/移动确认
    - snap: 当前快照，用于增量备份"""
    name = tool_call.function.name
    raw_args = tool_call.function.arguments or "{}"
    try:
        args = json.loads(raw_args)
    except json.JSONDecodeError as e:
        return {"name": name, "args": {}, "id": tool_call.id,
                "result": {"error": f"Invalid JSON in arguments: {e}"}}

    # 审计模式：兜底拦截写/执行工具，防止 LLM hallucinate 超出 schema 的工具
    if mode == "audit" and name not in READONLY_TOOL_NAMES:
        return {"name": name, "args": args, "id": tool_call.id,
                "result": {"error": f"audit 模式禁止调用工具 '{name}'：仅允许只读工具"}}

    _strip_workspace_prefix(args, "filename", "file_path", "src", "dst")
    hil_on = allow_hil and _cfg("human_in_loop") and not _BATCH_MODE

    if name == "write_file":
        fname = args.get("filename", "")
        overwrite = os.path.exists(os.path.join(_get_workspace(), fname))
        new_content = args.get("content", "")
        if hil_on:
            old_content = ""
            if overwrite:
                _r = read_file(fname)
                old_content = _r.get("content", "") if "error" not in _r else ""
            accept, final_content = _hil_confirm(fname, old_content, new_content, not overwrite)
            if not accept:
                return {"name": name, "args": args, "id": tool_call.id,
                        "result": {"error": "用户已跳过此写入"}}
            args["content"] = final_content
        elif allow_confirm and mode == "auto" and overwrite:
            confirm = _prompt(f"write_file 将覆盖已有文件 {fname}，确认？(y/n) ")
            if confirm != "y":
                return {"name": name, "args": args, "id": tool_call.id,
                        "result": {"error": "用户已跳过文件覆盖"}}
        _backup_file_if_needed(snap, fname)
        result = write_file(**args)
        if "success" in result:
            console.print(f"写入{'(覆盖)' if overwrite else ''} {fname}", highlight=False)
            _task_log_mod._task_files_modified.append(fname)
        return {"name": name, "args": args, "id": tool_call.id, "result": result}

    if name == "replace_in_file":
        rfname = args.get("filename", "")
        old_str = args.get("old_str", "")
        new_str = args.get("new_str", "")
        if hil_on:
            _r = read_file(rfname)
            old_content = _r.get("content", "") if "error" not in _r else ""
            new_content = old_content.replace(old_str, new_str, 1) if old_str in old_content else old_content
            accept, final_content = _hil_confirm(rfname, old_content, new_content)
            if not accept:
                return {"name": name, "args": args, "id": tool_call.id,
                        "result": {"error": "用户已跳过此修改"}}
            _backup_file_if_needed(snap, rfname)
            if final_content != new_content:
                result = write_file(rfname, final_content)
                if "success" in result:
                    result = {"success": f"文件 {rfname} 替换成功", "filename": rfname}
            else:
                result = replace_in_file(**args)
        else:
            _show_diff(rfname, old_str, new_str)
            if allow_confirm and mode == "auto":
                confirm = _prompt("应用此修改？(y/n) ")
                if confirm != "y":
                    return {"name": name, "args": args, "id": tool_call.id,
                            "result": {"error": "用户已跳过此修改"}}
            _backup_file_if_needed(snap, rfname)
            result = replace_in_file(**args)
        if "success" in result:
            console.print(f"replace_in_file OK: {result.get('filename')}", highlight=False)
            _task_log_mod._task_files_modified.append(rfname)
        else:
            console.print(f"替换失败: {result.get('error')}", highlight=False)
        return {"name": name, "args": args, "id": tool_call.id, "result": result}

    if name == "move_file":
        if allow_confirm and mode == "auto":
            confirm = _prompt(f"移动文件 {args.get('src')} → {args.get('dst')}？(y/n) ")
            if confirm != "y":
                return {"name": name, "args": args, "id": tool_call.id,
                        "result": {"error": "用户已跳过文件移动"}}
        _backup_file_if_needed(snap, args.get("src", ""))
        _backup_file_if_needed(snap, args.get("dst", ""))
        result = move_file(**args)
        return {"name": name, "args": args, "id": tool_call.id, "result": result}

    if name == "apply_patch":
        _backup_file_if_needed(snap, args.get("file_path", ""))
        result = apply_patch(**args)
        if "success" in result:
            _task_log_mod._task_files_modified.append(args.get("file_path", ""))
        return {"name": name, "args": args, "id": tool_call.id, "result": result}

    if name == "replace_symbol":
        _backup_file_if_needed(snap, args.get("file_path", ""))
        result = replace_symbol(**args)
        if "success" in result:
            _task_log_mod._task_files_modified.append(args.get("file_path", ""))
        return {"name": name, "args": args, "id": tool_call.id, "result": result}

    if name == "append_to_file":
        _backup_file_if_needed(snap, args.get("filename", ""))
        result = append_to_file(**args)
        if "success" in result:
            _task_log_mod._task_files_modified.append(args.get("filename", ""))
        return {"name": name, "args": args, "id": tool_call.id, "result": result}

    if name == "delete_file":
        del_fname = args.get("filename", "")
        if hil_on:
            confirm = _prompt(f"确认删除文件 {del_fname}？(y/n) ")
            if confirm != "y":
                return {"name": name, "args": args, "id": tool_call.id,
                        "result": {"error": "用户已跳过此删除"}}
        elif allow_confirm and mode == "auto":
            confirm = _prompt(f"delete_file 将删除 {del_fname}，确认？(y/n) ")
            if confirm != "y":
                return {"name": name, "args": args, "id": tool_call.id,
                        "result": {"error": "用户已跳过文件删除"}}
        _backup_file_if_needed(snap, del_fname)
        result = delete_file(**args)
        if "success" in result:
            console.print(f"删除 {del_fname}", highlight=False)
            _task_log_mod._task_files_modified.append(del_fname)
        return {"name": name, "args": args, "id": tool_call.id, "result": result}

    # task_complete: sentinel 退出信号；fix()/audit() 检测后跳出循环
    if name == "task_complete":
        result = task_complete(**args)
        return {"name": name, "args": args, "id": tool_call.id, "result": result}

    # 只读工具
    readonly_handlers = {
        "read_file": read_file,
        "execute_command": execute_command,
        "get_symbol_definition": get_symbol_definition,
        "search_in_files": search_in_files,
        "list_symbols": list_symbols,
        "workspace_symbols": workspace_symbols,
        "directory_summary": directory_summary,
        "fetch_webpage": fetch_webpage,
        "search_docs": search_docs,
        "find_references": find_references,
        "glob_files": glob_files,
        "git_diff": git_diff,
        "git_log": git_log,
        # P2 #7 Plan Mode 专用工具：sentinel 形态，由 plan_chat 循环识别
        "update_plan_draft": update_plan_draft,
        "exit_plan_mode_signal": exit_plan_mode_signal,
    }
    if name == "list_files":
        return {"name": name, "args": args, "id": tool_call.id, "result": list_files()}
    if name in readonly_handlers:
        try:
            result = readonly_handlers[name](**args)
        except Exception as e:
            result = _tools_mod._err("internal", f"工具调用异常: {e}")
        return {"name": name, "args": args, "id": tool_call.id, "result": result}

    return {"name": name, "args": args, "id": tool_call.id,
            "result": _tools_mod._err("invalid_args", f"未预期的工具: {name}")}


def _record_dispatch(out: dict, msgs: list):
    """把分发结果挂回 messages，并写入 task tool_calls 日志（敏感字段除外）"""
    args = out["args"]
    safe_args = {k: v for k, v in args.items() if k not in ("content", "new_str", "new_code")}
    _task_log_mod._task_tool_calls.append({"name": out["name"], "args": safe_args})
    msgs.append({
        "tool_call_id": out["id"],
        "role": "tool",
        "name": out["name"],
        "content": json.dumps(out["result"]),
    })

# ---------- #26 Linter / #27 项目类型检测：已迁移至 linter.py ----------

def run_linter():
    return _linter_mod.run_linter_for(_PROJECT_TYPE)

_ARCHITECT_ROLE = """【角色：架构师 Agent】
你专注于分析需求和制定实现计划。
职责：只输出计划，不写代码；重点考虑风险点和文件依赖顺序；确保计划完整可执行。
原则：
- 需求模糊或有多种合理解读时，**先用 1 句话明确你的理解和取舍**，再输出计划，而不是猜
- 计划要标出"哪些文件改、哪些不改"——LLM 容易过度扩张改动范围
- **全链路意识**（重要）：当任务涉及修改函数签名（增删参数、改返回值结构）、
  重命名标识符、或改变模块导出，**必须在计划里包含一步"用 search_in_files 或
  list_symbols 列出所有调用点"**，并把可能受影响的文件全部纳入修改清单。
  典型陷阱：用户只说"改 tools.py + tools_schema.py"，但调用方（如 agent.py
  的 dispatch）也得跟着改，否则新参数被默默吞掉。**用户没列到的文件不代表
  不需要改**——你的职责是发现这些"暗依赖"。
"""

_CODER_ROLE = """【角色：码农 Agent】
你专注于根据计划生成高质量代码。
职责：严格按计划执行，不自行发挥额外功能；注重代码质量和边界处理；已有文件用 replace_in_file 精确修改，不得整体重写。
工具调用效率（重要）：
- **修改前先定位**：用 search_in_files / list_symbols / get_symbol_definition 锁定要改的位置，不要整文件 read_file 后再决定
- **无依赖的工具调用并行**：同一轮可以同时发起多个 read_file / search_in_files / list_symbols；不要串行等
- **shell 多查询合并**：查多个 env 变量或运行多个独立命令时，用 `;` 串到一次 execute_command 里
- **改完别重读验证**：write_file / replace_in_file / replace_symbol 失败会直接返回错误，不需要再 read_file 确认
任务模式识别（动手前判断属于哪类，按对应规则操作）：

1. **修改已有函数签名/返回结构**
   - 先 search_in_files 搜函数名（如 `\\blist_files\\b`），列出所有调用方
   - 调用方需要跟着改的，本轮一并改完，不要分批
   - 典型暗依赖：dispatch 表（agent.py 里 `if name == "X"` 的分支）、
     导入语句（`from X import Y` 列表）、文档/README 示例
   - 用户列出的文件清单不一定完整——grep 之后缺什么自己补上

2. **新增工具/命令/handler（≠ 仅写实现）**
   - 三件套缺一不可：**实现**（tools.py 的函数）+ **schema**（tools_schema.py
     的 TOOLS 列表）+ **分发**（agent.py 的 `if name == "X"` 分支 +
     `from tools import X`）
   - 写完实现后必须主动检查另外两处是否就位；不要等单测失败再补
   - 类比：加一道菜要更新菜单和后厨流程，不是只把菜做出来

3. **递归/剪枝控制流（max_depth、深度限制类）**
   - 优先用"算路径深度后 continue"的简单过滤，例：
     ```
     for root, dirs, fnames in os.walk(base):
         rel = os.path.relpath(root, base)
         depth = 0 if rel == "." else len(rel.split(os.sep))
         if depth >= max_depth: continue
         for f in fnames: ...
     ```
   - 不要用 `dirs.clear() if depth >= max_depth` 这类巧妙剪枝：
     root depth=0、max_depth=1 时不触发清空，子目录文件已被加进列表，off-by-one
   - max_depth=1 的语义：包含 root 直接子项，不含孙项；写完先在脑里跑一遍 depth=0/1/2

4. **范围克制（重要）**
   - diff 应只覆盖任务描述的功能；"顺手"重构一律不做：
     - 不替换路径分隔符（`rel.replace("\\\\", "/")`——破坏过 test_list_files）
     - 不改既有变量名、不补类型注解、不"美化"格式
   - 想改任务范围外的东西，先停下来报告，不要直接动手
   - **失败用例不一定是你引入的**：跑测试看到红，先核对失败 assert 引用的函数/常量是不是
     本次 plan 列出文件里的符号——不沾边的（如本次改 list_files 但 test_execute_command_timeout
     失败）大概率是 pre-existing 失败，**记录在报告里但不要碰产品代码"修复"它**
   - **linter 报错同理（ruff/flake8/pyright/mypy）**：unused import (F401)、unused variable、
     格式问题等如果**不在本次 plan 文件范围内**（比如本次改 tools.py 但 ruff 提示 agent.py
     有 F401），按 pre-existing 处理——记录但不顺手清理；本次 plan 文件内的 linter 报错才修
测试文件规则：测试文件（test_*.py / *_test.py）若位于子目录（如 tests/），必须在文件最顶部加入以下两行，确保能导入父目录模块：
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
"""

_REVIEWER_ROLE = """【角色：代码审查 Agent】
你专注于审查已生成的代码。
职责：检查代码是否满足原始需求，是否存在潜在的边界漏洞，以及是否符合项目规则。
输入：本次修改的文件内容和原始需求。
原则：
- **区分 bug 和风格偏好**：前者必报（功能错误、边界 crash、安全问题）；后者克制（命名、注释多寡、可读性主观判断）
- **issues 按严重度排序**：critical（功能直接错） → major（边界/可靠性问题） → minor（代码味道）；不要平铺
- **指出问题前先排除"这是有意为之"**：不确定的标到 suggestions，不直接 reject
- **不臆断未读过的代码**：引用证据时给出 file:line
输出：必须严格返回 JSON 格式，包含 "approved" (bool), "issues" (字符串数组), "suggestions" (字符串数组)。
"""

_TESTER_ROLE = """【角色：测试 Agent】
你专注于分析测试失败原因并指导修复。

【收尾要求 - 必读】
**每次任务结束都必须调用 `task_complete(success, summary)` 显式收尾**——这是协议要求，不是建议。
- 修复完成、推断 pre-existing 失败可跳过、或确认无法继续，都用 task_complete 表达：
  - `task_complete(success=True, summary="修复了 X，跳过 Y/Z 两条 pre-existing 失败")`
  - `task_complete(success=False, summary="错误归属本次任务但缺少 Z 上下文，建议人工介入")`
- **不要沉默退出**（这一轮没调工具就直接结束）——loop 会再问你一次"是否完成"，浪费一轮。

【few-shot 示例】
示例 1（修了任务相关 + 跳过 pre-existing）：
  → replace_in_file(...)
  → task_complete(success=True, summary="修复 list_files max_depth=1 边界 bug；test_execute_command_timeout 等 5 条断言不属于本次范围，已跳过")

示例 2（确认无法修复）：
  → read_file(...)
  → task_complete(success=False, summary="测试期望 result['error'] 含'超时'但工具返回 security——这是 pre-existing 失败，本次任务不应改测试期望也不该改工具行为")

职责：只关注测试结果和错误信息；给出精准、最小化的修复建议；避免引入不相关改动。
排查顺序：
1. **先识别归属**：失败 assert 引用的符号是否在本次 plan 列出的文件里？
   - 是 → 本次任务引入的失败，继续走流程
   - 否（如本次改 list_files 但 test_execute_command_timeout 失败）→ 大概率 pre-existing 失败，**跳过不修**，task_complete 时在 summary 里列出来让用户判断
2. **先读测试代码**：找出失败的 assert 语句，理解期望是什么
3. **再读被测代码**：定位实际行为偏离期望的位置
4. **不要先改产品代码**：先确认是产品 bug 还是测试 bug；测试期望本身可能是错的
5. 报告时引用 file:line，不臆断"应该是 X 错了"
错误信息使用规则：
- `error_kind` 字段只是错误**分类标签**（让你判断该 retry 还是放弃），
  **不是改测试期望的依据**——pre-existing 测试用 "超时" 期望但工具返回 security 错误时，
  按归属规则（第 1 条）跳过这个失败，**不要把测试 assert 改成匹配 error_kind**。
"""

_AUDITOR_ROLE = """【角色：审计 Agent】
你专注于审计现有代码，输出可读的 Markdown 报告，绝不修改任何文件。

【收尾要求 - 必读】
**每次审计结束都必须调用 `task_complete(success, summary)` 显式收尾**——这是协议要求，不是建议。
- 报告输出在 assistant 消息正文里，task_complete 是最后一次工具调用：
  - `task_complete(success=True, summary="审计完成：发现 3 处 critical / 5 处 minor")`
  - `task_complete(success=False, summary="workspace 为空 / 目标符号不存在 / 无法继续")`
- **不要沉默退出**——loop 会再问你一次"是否完成"，浪费一轮。

工作流：先看 system 中预注入的 workspace_symbols 顶层摘要锁定关注目录/文件；再用 read_file/get_symbol_definition/search_in_files/find_references 按需深挖；最后输出报告 + task_complete。
**分层索引使用**：注入的是顶层结构，子目录只有计数。深挖某目录用 `workspace_symbols(path="<dir>")` 看该目录顶层符号，或 `directory_summary(path="<dir>")` 看文件清单/扩展名分布。**不要一次拿全树**（recursive=true 在大项目会撑爆 context）。
工具调用效率（重要）：
- **先定位再精读**：用 search_in_files / list_symbols / get_symbol_definition 锁定具体行号或符号，不要整文件 read_file 后再筛选
- **无依赖工具调用并行**：同一轮可同时发起多个 read/search/list_symbols
- **整文件 read 是最后手段**：只在确实需要看完整上下文（< 200 行）才读全文；大文件用 offset+limit 区间读
任务尺度感知（关键）：
- **简单问题给简单答**：用户问"有几个 X"、"X 在哪"这类计数/定位问题，直接给数字+清单，**不要套审计报告模板**（不需要总览/总评/分级）
- **审计报告模板只用于**："审一下 / 找出潜在问题 / 评估代码质量"这类开放性任务
- 输出长度匹配输入复杂度——一句话能答完的不要堆 5 段
报告结构（仅开放性审计任务用）：
## 总览（项目类型、规模、关注重点）
## 重要发现
- 按 严重 / 中 / 低 三级分类
- 每条标注 `file:line` + 现状描述 + 建议
## 总评（整体健康度、最值得优先处理的 1-3 项）
克制原则：
- 区分 bug 与风格偏好；指出问题前先排除"这是有意为之的设计选择"
- 不臆断未读过的代码；引用证据时给出 file:line
- 对小问题保持比例感，不要为凑数堆砌
"""

_PLANNER_ROLE = """【角色：Planner Agent（Plan Mode）】
你正处于 Plan Mode——**所有写工具被禁用**，你只能用只读工具探索代码与思考方案。
任务是：与用户多轮对话，沉淀一份清晰、可执行的实施方案（plan 草稿），由用户用 /approve 决定是否实施。

【收尾要求 - 必读】
- 每一轮工作完了用 `exit_plan_mode_signal(reason)` 表明"等待用户审阅"——不要沉默
- 想沉淀/修改方案就调 `update_plan_draft(content)`——**整体替换**最新草稿（不是追加）。每次给完整版
- **不要**调 `task_complete`——Plan Mode 的退出由用户 /approve 触发，不由你

【对话节奏】
- 用户提了新需求/补充信息 → 你先必要的探索（读关键文件、grep、看符号），再 update_plan_draft（如果方案有变），最后 exit_plan_mode_signal
- 用户表示满意但还没 /approve → 简短确认即可（一句话），别再大改草稿
- 用户要求修改方案 → 直接 update_plan_draft 改完，再 exit_plan_mode_signal

【plan 草稿建议结构】
## 目标
（一句话：要解决什么问题/达到什么效果）
## 改动文件
- file_a.py：做什么 / 为什么
- file_b.py：...
## 步骤
1. ...
2. ...
## 风险与权衡
- ...

【避免的反模式】
- 一上来不探索就给方案——先读 1-3 个关键文件再下笔
- 改一个字也大改方案——增量更新，复用前一版结构
- 写代码或建议执行命令——这是实施期的事，Plan Mode 只产出方案
"""

def _get_project_rules():
    rules_path = Path(_get_workspace()) / ".agent_rules"
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
        rules_path = Path(_get_workspace()) / ".agent_rules"
        if rules_path.exists():
            try:
                content = rules_path.read_text(encoding="utf-8").strip()
                if content:
                    return f"\n项目规则：\n{content}\n"
            except Exception:
                pass
        return ""

    def _generate_tree():
        ws = Path(_get_workspace())
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
你是一个代码规划助手。根据用户需求，返回严格符合以下 JSON Schema 的计划：

{{
  "files": [
    {{"filename": "<相对路径>", "description": "<修改意图>"}}
  ],
  "test_command": "<执行测试的命令>"
}}

完整示例：
{{"files": [{{"filename": "add.py", "description": "实现 add(a,b) 函数"}}, {{"filename": "tests/test_add.py", "description": "覆盖正常/边界用例"}}], "test_command": "python tests/test_add.py"}}

字段约束：
- files 元素必填 filename；description 描述本次修改意图（不要包含完整代码）
- 对已有文件只描述要追加/修改什么，不要重新创建

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
    return _call_with_json_retry(
        "plan", messages, _parse_plan_with_status,
        response_format={"type": "json_object"},
    )


def _parse_plan_with_status(content: str):
    """返回 (ok, plan_dict, err_msg)。retry 包装用此版本；旧 _parse_plan_response 委托到这里。"""
    if not content.strip():
        return False, {"files": [], "test_command": ""}, "LLM 返回空内容"
    extracted = _extract_json(content)
    try:
        raw = json.loads(extracted)
    except json.JSONDecodeError as e:
        return False, {"files": [], "test_command": ""}, f"json.loads 失败：{e}"
    # 兼容 LLM 直接返回数组的旧形态
    if isinstance(raw, list):
        raw = {"files": raw, "test_command": ""}
    if not isinstance(raw, dict):
        return False, {"files": [], "test_command": ""}, f"顶层不是 dict/list，而是 {type(raw).__name__}"
    try:
        validated = PlanResult(**raw)
    except ValidationError as e:
        # 校验失败仍返回原 dict，让下游尽力处理
        return False, raw, f"schema 校验失败：{e.errors()}"
    # 序列化回 plain dict（与历史行为保持兼容：files 元素可能是 str 或 dict）
    files_out = []
    for f in validated.files:
        if isinstance(f, PlanFile):
            d = f.model_dump()
            if d.get("description") and not d.get("intent"):
                d["intent"] = d["description"]
            files_out.append(d)
        else:
            files_out.append(f)
    return True, {"files": files_out, "test_command": validated.test_command}, None


def _parse_plan_response(content: str) -> dict:
    """旧入口：失败时 log raw（保留向后兼容，单测仍直接调它）"""
    ok, data, err = _parse_plan_with_status(content)
    if not ok:
        _log_json_failure("plan", content, err)
    return data

def code(plan, mode="auto", requirement=""):
    """根据计划逐个文件生成/修改代码。文件已存在优先用replace_in_file做精确修改；不存在则用write_file新建。

    返回 None 或 dict {"early_exit": True, "success": bool, "summary": str}：
      - None：正常完成所有文件，没有 task_complete 信号
      - success=True：Coder 主动声明本文件/任务完成（信息性，run() 不会因此跳过测试）
      - success=False：Coder 主动放弃整个任务——run() 应跳过 review/测试直接标失败
    """
    import os as _os
    from tools import write_file, read_file, replace_in_file

    files = plan.get("files", [])
    console.print("[Agent: Coder]", highlight=False)
    console.print(f"计划处理 {len(files)} 个文件...")
    coder_signal = None  # 多文件循环结束时上送给 run()

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
        _ws = _get_workspace()
        for _pfx in (_ws + "/", _ws + "\\"):
            if filename.startswith(_pfx):
                filename = filename[len(_pfx):]
                break

        filepath = _os.path.join(_ws, filename)
        file_exists = _os.path.exists(filepath)

        if file_exists:
            existing = read_file(filename)
            if "error" in existing:
                console.print(f"读取 {filename} 失败: {existing['error']}")
                continue
            existing_content = existing.get("content", "")
            console.print(f"{filename} 已存在，读取现有内容进行增量修改...")

            req_block = f"\n原始需求（必须严格遵守，包括变量名、库名、API key 名称等技术细节）：\n{requirement}\n" if requirement else ""
            sys_prompt = f"""{_CODER_ROLE}
{_get_project_rules()}{req_block}
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
            req_block = f"\n原始需求（必须严格遵守，包括变量名、库名、API key 名称等技术细节）：\n{requirement}\n" if requirement else ""
            sys_prompt = f"""{_CODER_ROLE}{req_block}
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
                _early_exit_inner = False  # 收到 task_complete sentinel 时用来跳出 inner while
                for tool_call in response_message.tool_calls:
                    out = _dispatch_tool_call(tool_call, mode=mode, snap=_CURRENT_SNAPSHOT)
                    _record_dispatch(out, msgs)
                    # P0 #3 sentinel：Coder 主动声明任务结束
                    if out["result"].get("_task_complete"):
                        _success = bool(out["result"].get("success"))
                        _summary = out["result"].get("summary", "")
                        if not _success:
                            console.print(f"[Coder task_complete] 主动放弃：{_summary}",
                                          style="yellow", highlight=False)
                            return {"early_exit": True, "success": False, "summary": _summary}
                        # success=True：本文件完成，跳出 inner loop（multi-file 循环继续）
                        coder_signal = {"early_exit": True, "success": True, "summary": _summary}
                        _early_exit_inner = True
                        break
                if _early_exit_inner:
                    break
            else:
                break

        if attempts_left <= 0 and response_message.tool_calls:
            # #8 上限耗尽仍在调工具，提示并记录
            warn = f"[警告] {filename} 已用尽 5 轮工具调用上限"
            console.print(warn, style="yellow", highlight=False)
            _task_log_mod._current_task_log.setdefault("warnings", []).append(warn)

    console.print("代码生成/修改完成")

    # pyproject.toml 有变更时自动重装包，确保新增模块立即可用
    if any("pyproject.toml" in (f or "") for f in _task_log_mod._task_files_modified):
        import subprocess as _sp
        console.print("[自动] 检测到 pyproject.toml 变更，执行 pip install -e . ...", highlight=False)
        r = _sp.run(["pip", "install", "-e", "."], capture_output=True, text=True)
        if r.returncode == 0:
            console.print("[自动] pip install -e . 完成", highlight=False)
        else:
            console.print(f"[警告] pip install -e . 失败: {r.stderr[:200]}", style="yellow", highlight=False)

    return coder_signal

def audit(requirement):
    """审计现有代码，只读多轮工具调用，最终输出 markdown 报告。
    返回 {"success": bool, "report": str}。"""
    console.print("[Agent: Auditor]", highlight=False)

    # P0 #1：默认只注入顶层结构，子目录用 path 参数按需深挖（避免大项目撑爆 context）
    ws_symbols_result = workspace_symbols()  # default top
    if "error" in ws_symbols_result:
        symbols_brief = f"（workspace_symbols 失败：{ws_symbols_result['error']}）"
    else:
        files_map = ws_symbols_result.get("files", {})
        subdirs_map = ws_symbols_result.get("subdirs", {})
        lines = []
        for path, syms in sorted(files_map.items()):
            head = ", ".join(f"{s['name']}({s['type'][0]}:L{s['line']})" for s in syms[:30])
            extra = f" +{len(syms)-30}" if len(syms) > 30 else ""
            lines.append(f"  {path}: {head}{extra}")
        if subdirs_map:
            lines.append("")
            lines.append("子目录（用 workspace_symbols(path='<dir>') 或 directory_summary(path='<dir>') 深入）：")
            for d, info in sorted(subdirs_map.items()):
                lines.append(f"  {d}/  ({info['py_files']} 个 .py / {info['total_symbols']} 个符号)")
        symbols_brief = (
            f"workspace 顶层符号索引（{ws_symbols_result['total_files']} 顶层文件 / "
            f"{ws_symbols_result['total_symbols']} 顶层符号；子目录按需深挖）：\n"
            + "\n".join(lines)
        )

    sys_prompt = f"{_AUDITOR_ROLE}{_get_project_rules()}\n\n{symbols_brief}"
    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": f"审计需求：{requirement}"}
    ]

    audit_tools = _filter_tools(READONLY_TOOL_NAMES)
    rounds_used = 0
    last_text = ""

    # P0 #3 token 预算
    start_tokens = get_session_total_tokens()
    budget_warned = False
    silent_prompted = False  # 沉默退出兜底：LLM 没调工具时追问一次

    while rounds_used < _AUDIT_SOFT_LIMIT:
        rounds_used += 1
        if interrupt.is_interrupted():
            raise interrupt.Interrupted()

        # token 预算检查（每轮开头）：超阈值时往 messages 注一条 system 提醒，只警告一次
        if not budget_warned:
            used = get_session_total_tokens() - start_tokens
            if used > _AUDIT_TOKEN_BUDGET:
                console.print(f"[预算] audit token 增量 {used} > {_AUDIT_TOKEN_BUDGET}，提醒 LLM 收尾",
                              style="yellow", highlight=False)
                messages.append({
                    "role": "system",
                    "content": (
                        f"⚠️ 本次 audit 累计已用 {used} tokens（预算 {_AUDIT_TOKEN_BUDGET}）。"
                        "请尽快用 task_complete(success, summary) 收尾，不要再发起新的探索性工具调用。"
                    ),
                })
                budget_warned = True

        response = call_llm(messages, tools=audit_tools, tool_choice="auto", stream=False)
        msg = response.choices[0].message
        messages.append({
            "role": "assistant",
            "content": msg.content,
            "tool_calls": [tc.model_dump() for tc in msg.tool_calls] if msg.tool_calls else None,
        })

        if msg.content:
            last_text = msg.content

        if msg.tool_calls:
            console.print(f"审计轮 {rounds_used}: {len(msg.tool_calls)} 次工具调用")
            for tc in msg.tool_calls:
                out = _dispatch_tool_call(tc, mode="audit", allow_hil=False, allow_confirm=False, snap=None)
                _record_dispatch(out, messages)
                # P0 #3 sentinel：LLM 主动声明任务结束
                if out["result"].get("_task_complete"):
                    success = bool(out["result"].get("success"))
                    summary = out["result"].get("summary", "")
                    console.print(f"审计完成（task_complete: {'成功' if success else '放弃'}）：{summary}")
                    console.print()
                    console.print(last_text or summary or "（无报告内容）", highlight=False)
                    return {
                        "success": success,
                        "report": last_text or summary,
                        "task_complete_signal": {
                            "early_exit": True, "success": success, "summary": summary,
                        },
                    }
        else:
            # 沉默退出兜底：第一次没调工具 → 追问一次让它显式 task_complete
            if not silent_prompted:
                silent_prompted = True
                console.print("[兜底] LLM 未调工具，追问一次要求显式 task_complete", style="yellow", highlight=False)
                messages.append({
                    "role": "system",
                    "content": (
                        "你这一轮没调任何工具——按协议必须用 task_complete(success, summary) 显式收尾。"
                        "如果报告已写完请 task_complete(success=true, summary=...)；"
                        "确认无法完成请 task_complete(success=false, summary=...)。"
                    ),
                })
                continue
            console.print(f"审计完成（{rounds_used} 轮，沉默退出已追问过一次）")
            console.print()
            console.print(last_text or "（无报告内容）", highlight=False)
            return {"success": True, "report": last_text}

    console.print(f"[警告] audit 已达 {_AUDIT_SOFT_LIMIT} 轮上限，输出当前报告", style="yellow", highlight=False)
    console.print()
    console.print(last_text or "（无报告内容）", highlight=False)
    return {"success": bool(last_text), "report": last_text}


# ============================================================
# P2 #7 Plan Mode 方案 C：会话级 plan_mode + 多轮探索 + /approve
# ============================================================

_PLAN_SOFT_LIMIT = 12  # plan_chat 单轮（一次用户输入）内的最大工具调用轮数


def enter_plan_mode():
    """打开 plan_mode flag。新对话历史从空开始。"""
    global _PLAN_MODE, _PLAN_HISTORY
    _PLAN_MODE = True
    _PLAN_HISTORY = []
    console.print("[Plan Mode] 已进入 plan 模式——写工具被禁用，多轮对话精炼方案；"
                  "用 /plan 查看草稿，/approve 批准并实施，/plan_off 取消", highlight=False)


def cancel_plan_mode():
    """关 plan_mode 不实施。草稿丢弃。"""
    global _PLAN_MODE, _PLAN_DRAFT, _PLAN_HISTORY
    _PLAN_MODE = False
    _PLAN_DRAFT = ""
    _PLAN_HISTORY = []
    console.print("[Plan Mode] 已退出，草稿已丢弃", highlight=False)


def get_plan_draft() -> str:
    return _PLAN_DRAFT


def is_plan_mode() -> bool:
    return _PLAN_MODE


def approve_plan() -> str:
    """用户 /approve：返回需要交给 run() 的 requirement（含原始上下文 + 草稿）。
    草稿会作为强约束拼进 requirement。退出 plan_mode 但保留草稿到最后清理。
    """
    global _PLAN_MODE, _PLAN_DRAFT, _PLAN_HISTORY
    if not _PLAN_DRAFT:
        return ""
    # 拼接 requirement：草稿作为强约束 + 历次用户指令的拼接（最近一条作为主诉求）
    user_msgs = [m["content"] for m in _PLAN_HISTORY if m.get("role") == "user"]
    headline = user_msgs[0] if user_msgs else "按以下方案实施"
    enriched = (
        f"{headline}\n\n"
        f"【已批准的实施方案 - 严格按此执行】\n{_PLAN_DRAFT}"
    )
    _PLAN_MODE = False
    _PLAN_DRAFT = ""
    _PLAN_HISTORY = []
    return enriched


def plan_chat(user_input: str) -> str:
    """Plan Mode 下的对话循环：READONLY tools + multi-round + sentinel 识别。

    每次用户输入触发一次循环——LLM 探索 / 更新草稿 / 调 exit_plan_mode_signal 收尾。
    返回最后一段 assistant 文本供主循环显示。
    """
    global _PLAN_DRAFT, _PLAN_HISTORY

    # 注入 workspace 顶层结构（与 audit 同款）
    try:
        ws_symbols_result = workspace_symbols()
    except Exception:
        ws_symbols_result = {"error": "workspace_symbols 失败", "files": {}, "subdirs": {}}
    if "error" in ws_symbols_result:
        symbols_brief = f"（workspace_symbols 失败：{ws_symbols_result['error']}）"
    else:
        files_map = ws_symbols_result.get("files", {})
        subdirs_map = ws_symbols_result.get("subdirs", {})
        lines = []
        for path, syms in sorted(files_map.items()):
            head = ", ".join(f"{s['name']}({s['type'][0]}:L{s['line']})" for s in syms[:30])
            extra = f" +{len(syms)-30}" if len(syms) > 30 else ""
            lines.append(f"  {path}: {head}{extra}")
        if subdirs_map:
            lines.append("")
            lines.append("子目录（按需深挖：workspace_symbols(path='...') / directory_summary(path='...')）:")
            for d, info in sorted(subdirs_map.items()):
                lines.append(f"  {d}/  ({info['py_files']} 个 .py / {info['total_symbols']} 个符号)")
        symbols_brief = (
            f"workspace 顶层符号索引（{ws_symbols_result['total_files']} 顶层文件 / "
            f"{ws_symbols_result['total_symbols']} 顶层符号）：\n" + "\n".join(lines)
        )

    sys_prompt = f"{_PLANNER_ROLE}{_get_project_rules()}\n\n{symbols_brief}"
    if _PLAN_DRAFT:
        sys_prompt += f"\n\n【当前 plan 草稿】\n{_PLAN_DRAFT}"

    # 用 plan_history 做对话连续性，但每次重新构造 messages（避免无限累积 system）
    messages = [{"role": "system", "content": sys_prompt}]
    messages.extend(_PLAN_HISTORY)
    messages.append({"role": "user", "content": user_input})
    _PLAN_HISTORY.append({"role": "user", "content": user_input})

    plan_tools = _filter_tools(READONLY_TOOL_NAMES)
    rounds_used = 0
    last_text = ""
    silent_prompted = False

    while rounds_used < _PLAN_SOFT_LIMIT:
        rounds_used += 1
        if interrupt.is_interrupted():
            raise interrupt.Interrupted()

        response = call_llm(messages, tools=plan_tools, tool_choice="auto", stream=False)
        msg = response.choices[0].message
        messages.append({
            "role": "assistant",
            "content": msg.content,
            "tool_calls": [tc.model_dump() for tc in msg.tool_calls] if msg.tool_calls else None,
        })
        if msg.content:
            last_text = msg.content

        if msg.tool_calls:
            console.print(f"[plan 轮 {rounds_used}] {len(msg.tool_calls)} 次工具调用", highlight=False)
            done = False
            for tc in msg.tool_calls:
                out = _dispatch_tool_call(tc, mode="audit", allow_hil=False, allow_confirm=False, snap=None)
                _record_dispatch(out, messages)
                # plan 草稿更新
                if out["result"].get("_plan_draft_update"):
                    _PLAN_DRAFT = out["result"].get("content", "")
                    console.print(f"[plan 草稿已更新 / {len(_PLAN_DRAFT)} 字符]", highlight=False)
                # exit signal：本轮收尾
                if out["result"].get("_exit_plan_mode_signal"):
                    reason = out["result"].get("reason", "")
                    if reason:
                        console.print(f"[plan 等待审阅] {reason}", highlight=False)
                    done = True
            if done:
                break
        else:
            # 沉默退出兜底：追问一次
            if not silent_prompted:
                silent_prompted = True
                messages.append({
                    "role": "system",
                    "content": (
                        "你这一轮没调任何工具——按协议本轮结束应调 `exit_plan_mode_signal()` 表明等待审阅；"
                        "若有方案变更先 `update_plan_draft(content=...)`。"
                    ),
                })
                continue
            break

    # 把本轮 assistant 文本和草稿沉淀进 plan_history（不存 tool_calls，避免历史膨胀）
    if last_text:
        _PLAN_HISTORY.append({"role": "assistant", "content": last_text})

    return last_text or "（无文本输出）"


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
        rules_path = Path(_get_workspace()) / ".agent_rules"
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
        # P1 #4：走 retry 包装；REVIEW_MODEL 走单模型路径，仍保留旧行为（不 retry，避免变更面）
        if REVIEW_MODEL:
            response = _call_single_model(_client_for(REVIEW_MODEL), REVIEW_MODEL, messages, stream=True)
            content = response.choices[0].message.content or ""
            return _parse_review_response(content)
        return _call_with_json_retry(
            "review", messages, _parse_review_with_status, stream=True,
        )
    except Exception as e:
        return {
            "approved": False,
            "issues": [f"review_error: {e}"],
            "suggestions": [],
        }


def _parse_review_with_status(content: str):
    """返回 (ok, review_dict, err_msg)。retry 包装用此版本。"""
    if not content.strip():
        return False, {"approved": False,
                       "issues": ["review_error: LLM 返回空内容"],
                       "suggestions": []}, "LLM 返回空内容"
    extracted = _extract_json(content)
    try:
        raw = json.loads(extracted)
    except json.JSONDecodeError as e:
        return False, {"approved": False,
                       "issues": [f"review_error: LLM 返回非 JSON（{e}）"],
                       "suggestions": []}, f"json.loads 失败：{e}"
    if not isinstance(raw, dict):
        return False, {"approved": False,
                       "issues": ["review_error: 顶层非 dict"],
                       "suggestions": []}, f"顶层不是 dict，而是 {type(raw).__name__}"
    try:
        validated = ReviewResult(**raw)
        return True, validated.model_dump(), None
    except ValidationError as e:
        # 兜底：尽力按字段类型抢救
        salvage = {
            "approved": bool(raw.get("approved", False)),
            "issues": list(raw.get("issues", [])) or ["review_error: schema 校验失败"],
            "suggestions": list(raw.get("suggestions", [])),
        }
        return False, salvage, f"schema 校验失败：{e.errors()}"


def _parse_review_response(content: str) -> dict:
    """旧入口：失败时 log raw（向后兼容）"""
    ok, data, err = _parse_review_with_status(content)
    if not ok:
        _log_json_failure("review", content, err)
    return data

def fix(test_result, plan, reason="test_failure"):
    """根据测试错误或审查意见修复代码（多轮工具调用）
    reason: "test_failure" | "review_rejection"

    返回 dict: {"early_exit": bool, "success": bool, "summary": str}
      - early_exit=True：LLM 主动调了 task_complete，外层应立即按 success 决定终止/标记
      - early_exit=False：沉默退出（兜底已追问过）或软上限耗尽——外层走原路径继续 retry
    """
    console.print("[Agent: Tester]", highlight=False)
    console.print("开始修复代码...")

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

    if reason == "review_rejection":
        content = f"代码审查未通过，请按以下审查意见逐条修复代码：\n\n{error_info}\n\n计划：{json.dumps(plan)}"
        sys_role = f"{_TESTER_ROLE}\n{_get_project_rules()}你是代码审查修复助手。请严格按审查意见逐条修复，每条对应一次 replace_in_file 精确修改。不要改与审查意见无关的代码。"
    else:
        content = f"测试失败！\n错误输出：\n{error_info}\n\n计划：{json.dumps(plan)}"
        sys_role = f"{_TESTER_ROLE}\n{_get_project_rules()}你是代码修复助手。根据错误信息精准修复代码，优先使用 replace_in_file 做最小化修改，必要时才用 write_file 重写整个文件。"

    messages = [
        {"role": "system", "content": sys_role},
        {"role": "user", "content": content}
    ]

    # P0 #3 软上限 + token 预算 + sentinel 退出
    rounds_used = 0
    start_tokens = get_session_total_tokens()
    budget_warned = False
    silent_prompted = False  # 沉默退出兜底：LLM 没调工具时追问一次

    while rounds_used < _FIX_SOFT_LIMIT:
        rounds_used += 1
        if interrupt.is_interrupted():
            raise interrupt.Interrupted()

        # token 预算检查（每轮开头）
        if not budget_warned:
            used = get_session_total_tokens() - start_tokens
            if used > _FIX_TOKEN_BUDGET:
                console.print(f"[预算] fix token 增量 {used} > {_FIX_TOKEN_BUDGET}，提醒 LLM 收尾",
                              style="yellow", highlight=False)
                messages.append({
                    "role": "system",
                    "content": (
                        f"⚠️ 本次 fix 累计已用 {used} tokens（预算 {_FIX_TOKEN_BUDGET}）。"
                        "请尽快用 task_complete(success, summary) 收尾——确认无法继续就 success=false。"
                    ),
                })
                budget_warned = True

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
                # fix 阶段不弹覆盖确认（已经是修复路径，再问无意义），但仍允许 HIL
                out = _dispatch_tool_call(
                    tool_call, mode="code", allow_hil=True, allow_confirm=False, snap=_CURRENT_SNAPSHOT
                )
                _record_dispatch(out, messages)
                # P0 #3 sentinel：LLM 主动声明任务结束
                if out["result"].get("_task_complete"):
                    success = bool(out["result"].get("success"))
                    summary = out["result"].get("summary", "")
                    console.print(f"修复结束（task_complete: {'成功' if success else '放弃'}）：{summary}",
                                  style="yellow" if not success else None, highlight=False)
                    return {"early_exit": True, "success": success, "summary": summary}
        else:
            # 沉默退出兜底：第一次没调工具 → 追问一次让它显式 task_complete
            if not silent_prompted:
                silent_prompted = True
                console.print("[兜底] LLM 未调工具，追问一次要求显式 task_complete", style="yellow", highlight=False)
                messages.append({
                    "role": "system",
                    "content": (
                        "你这一轮没调任何工具——按协议必须用 task_complete(success, summary) 显式收尾。"
                        "如果已修完请 task_complete(success=true, summary='做了什么')；"
                        "确认无法继续请 task_complete(success=false, summary='为什么放弃')。"
                    ),
                })
                continue
            console.print("修复完成（沉默退出，已追问过一次）")
            return {"early_exit": False, "success": False, "summary": ""}
    console.print(f"[警告] fix 已达 {_FIX_SOFT_LIMIT} 轮上限，强制退出", style="yellow", highlight=False)
    return {"early_exit": False, "success": False, "summary": ""}

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

def report(success, test_result, task_complete_signal=None):
    """输出最终结果。task_complete_signal: LLM 主动声明信号 {early_exit, success, summary}，可选。"""
    out = {
        "success": success,
        "test_result": test_result,
    }
    if task_complete_signal:
        out["task_complete_signal"] = task_complete_signal
    return out

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
    _hil_mod.reset_auto_accept()  # 每轮任务重置 HIL "全部接受"标志
    os.makedirs(_get_workspace(), exist_ok=True)  # 兜底：新目录首次运行时 workspace/ 可能不存在
    original_requirement = requirement
    init_task_log(requirement, mode)

    # audit 模式：完全独立路径，不进 plan/code/review/fix
    if mode == "audit":
        res = audit(original_requirement)
        finish_task_log(res["success"], 0,
                        task_complete_signal=res.get("task_complete_signal"))
        return {"success": res["success"],
                "test_result": {"returncode": 0 if res["success"] else 1,
                                "stdout": res.get("report", ""), "stderr": ""}}

    # 阶段1：制定计划
    console.print("阶段1：制定计划")
    plan_result = plan(requirement)
    _task_log_mod._current_task_log["plan"] = plan_result.get("files", [])
    _task_log_mod._current_task_log["test_command"] = plan_result.get("test_command", "")

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
    _gc_old_snapshots(keep=10)
    current_snapshot = create_snapshot(file_list)
    # 让 code()/fix()/_auto_generate_tests 通过模块变量访问当前快照（用于增量备份）
    global _CURRENT_SNAPSHOT
    _CURRENT_SNAPSHOT = current_snapshot

    # code / auto（确认后）：生成代码 + 测试
    console.print("\n阶段2：生成代码")
    coder_signal = code(plan_result, mode=mode, requirement=original_requirement)

    # P0 #3 第二波：Coder 主动放弃 → 跳过 review/test/fix 直接标失败
    if coder_signal and coder_signal.get("early_exit") and not coder_signal.get("success"):
        _summary = coder_signal.get("summary", "")
        console.print(f"[task_complete] Coder 主动放弃任务：{_summary}",
                      style="yellow", highlight=False)
        add_to_history(original_requirement, f"任务被 LLM 主动放弃：{_summary}")
        # test_result 用 dummy dict（returncode=-1）避免外层 .get() 崩溃，
        # 同时把 LLM 的放弃理由放进 stderr 便于回放
        _dummy_tr = {"returncode": -1, "stdout": "", "stderr": f"Coder 主动放弃：{_summary}"}
        finish_task_log(False, 0, _dummy_tr, task_complete_signal=coder_signal)
        return report(False, _dummy_tr, task_complete_signal=coder_signal)

    # 砍掉独立的 reviewer agent 循环：对标 Claude Code 单 agent 设计——
    # coder 自己负责"做+验证"，由后面的"测试与修复"循环承担质量把关。
    # review() 函数保留为独立可调用工具（如未来加 /review skill 时复用）。

    console.print("\n阶段3：测试与修复")
    attempts = 0
    test_result = None
    max_attempts = _cfg("max_attempts") or 3

    # #42 如果 workspace 中没有测试文件，自动生成
    ws = Path(_get_workspace())
    _ignore = {".yansh", ".git", "__pycache__", "node_modules", "venv", "workspace"}
    has_tests = bool([
        f for f in ws.rglob("test_*.py")
        if not any(part in _ignore for part in f.relative_to(ws).parts)
    ] + [
        f for f in ws.rglob("*_test.py")
        if not any(part in _ignore for part in f.relative_to(ws).parts)
    ])
    if not has_tests:
        _auto_generate_tests(plan_result, _task_log_mod._task_files_modified[:])



    # #26 Linter：先跑 ruff，有错误走修复循环
    linter_result = run_linter()
    if linter_result:
        console.print(f"Linter 发现错误，开始修复 (尝试 {attempts + 1}/{max_attempts})", highlight=False)
        fix_signal = fix(linter_result, plan_result)
        # P0 #3 第二波：linter 阶段 LLM 主动放弃也直接终止
        if fix_signal.get("early_exit") and not fix_signal.get("success"):
            _summary = fix_signal.get("summary", "")
            console.print(f"[task_complete] LLM 在 linter 修复阶段主动放弃：{_summary}",
                          style="yellow", highlight=False)
            add_to_history(original_requirement, f"任务被 LLM 主动放弃：{_summary}")
            _dummy_tr = {"returncode": -1, "stdout": "", "stderr": f"linter 阶段放弃：{_summary}"}
            finish_task_log(False, attempts, _dummy_tr, task_complete_signal=fix_signal)
            return report(False, _dummy_tr, task_complete_signal=fix_signal)
        attempts += 1

    while attempts < max_attempts:
        test_result = test(plan_result.get("test_command", ""))
        if judge(test_result):
            console.print("测试通过！")
            # 保留快照供 /revert（旧快照由下次任务的 _gc_old_snapshots 自动清理）
            files = plan_result.get("files", [])
            file_names = [f.get("filename") if isinstance(f, dict) else str(f) for f in files]
            file_names = [name for name in file_names if name]
            summary = f"执行了任务：{original_requirement}。创建/修改了文件：{', '.join(file_names)}"
            add_to_history(original_requirement, summary)
            finish_task_log(True, attempts, test_result, task_complete_signal=coder_signal)
            return report(True, test_result, task_complete_signal=coder_signal)
        else:
            console.print(f"测试失败 (尝试 {attempts + 1}/{max_attempts})")
            if attempts < max_attempts - 1:
                fix_signal = fix(test_result, plan_result)
                # P0 #3 第二波：识别 fix() 的主动声明，避免无谓 retry
                if fix_signal.get("early_exit"):
                    _summary = fix_signal.get("summary", "")
                    if fix_signal.get("success"):
                        # LLM 说"任务完成（剩下是 pre-existing 等不归我管）"
                        console.print(f"[task_complete] LLM 主动声明任务完成：{_summary}",
                                      highlight=False)
                        add_to_history(original_requirement,
                                       f"执行了任务（LLM 主动收尾）：{_summary}")
                        finish_task_log(True, attempts, test_result, task_complete_signal=fix_signal)
                        return report(True, test_result, task_complete_signal=fix_signal)
                    else:
                        # LLM 说"放弃"
                        console.print(f"[task_complete] LLM 主动放弃任务：{_summary}",
                                      style="yellow", highlight=False)
                        add_to_history(original_requirement, f"任务被 LLM 主动放弃：{_summary}")
                        finish_task_log(False, attempts, test_result, task_complete_signal=fix_signal)
                        return report(False, test_result, task_complete_signal=fix_signal)
            attempts += 1

    console.print("达到最大尝试次数，任务失败")
    # #37 失败时提示回滚
    if current_snapshot:
        answer = _prompt("是否回滚到任务开始前的状态？(y/n) ", default="n")
        if answer == "y":
            n = restore_snapshot(current_snapshot)
            console.print(f"[已回滚] 恢复 {n} 个文件", highlight=False)
            cleanup_snapshot(current_snapshot)
        # 不主动 cleanup：失败时也保留快照供后续 /revert，由 _gc_old_snapshots 控制总量

    add_to_history(original_requirement, f"任务失败：{original_requirement}")
    finish_task_log(False, attempts, test_result, task_complete_signal=coder_signal)
    return report(False, test_result, task_complete_signal=coder_signal)


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
                    _ws = _get_workspace()
                    for _pfx in (_ws + "/", _ws + "\\"):
                        if _fn.startswith(_pfx):
                            _fn = _fn[len(_pfx):]
                            break
                    # 确保测试文件写入 tests/ 子目录
                    _base = Path(_fn).name
                    if _base.startswith("test_") and "/" not in _fn and "\\" not in _fn:
                        _fn = "tests/" + _base
                    args["filename"] = _fn
                    _backup_file_if_needed(_CURRENT_SNAPSHOT, _fn)
                    result = write_file(**args)
                    console.print(f"[自动测试] 生成: {args.get('filename')}", highlight=False)
                    if "success" in result:
                        _task_log_mod._task_files_modified.append(args.get("filename", ""))
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
