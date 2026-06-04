"""LLM 客户端层：客户端工厂、call_llm 主循环、流式响应处理、token 统计/费用预估

模型路由：
  Claude → IBM ICA gateway (_get_ica_client)
  Gemini → Vertex AI (_get_gemini_client，每次刷新 OAuth token)
  其他   → 默认 client（OPENROUTER_BASE_URL，可指向 OpenRouter 或 ICA）
"""
import sys
import threading
import time as _time
from types import SimpleNamespace

from openai import OpenAI
from console_shared import console

from config import (
    OPENROUTER_API_KEY, OPENROUTER_BASE_URL, QUALITY_CASCADE,
    get_model_price,
)
import interrupt

# 默认 client（OPENROUTER_BASE_URL 已在 config.py 中指向 ICA 或 OpenRouter）
client = OpenAI(api_key=OPENROUTER_API_KEY, base_url=OPENROUTER_BASE_URL)

_ica_client = None

# Token 统计：按 model 分桶，便于按各自价格计费
_session_tokens_by_model: dict = {}
_session_tokens_lock = threading.Lock()  # M2 修复：并发写保护
_last_request_tokens = {"prompt": 0, "completion": 0, "model": ""}

# response_format 支持探测：探测过且失败的 model 加进来，后续请求自动跳过传参
_RF_UNSUPPORTED: set = set()

LLM_TIMEOUT_SEC = 120
LLM_MAX_RETRIES_PER_MODEL = 3  # 每个模型对 429/5xx 的退避重试次数


def _looks_like_rf_rejection(exc) -> bool:
    """探测后端是否因 response_format 不支持而 400。
    匹配常见错误描述：response_format、json_object、Unknown parameter 等。"""
    msg = (str(exc) or "").lower()
    keys = ("response_format", "json_object", "unknown parameter",
            "not supported", "unsupported")
    return any(k in msg for k in keys)


def _should_skip_rf(model: str) -> bool:
    """是否跳过传 response_format。
    硬规则（已知差表现）+ 动态探测（运行时报 400 的）。

    硬规则：Claude 走 ICA 时虽然接受 response_format 不报错，但 json_object 模式下
    模型常常退化为输出 `{}`——比 400 还隐蔽。实测见笔记 _14。
    """
    if model in _RF_UNSUPPORTED:
        return True
    if _is_claude(model):
        return True
    return False


def set_quality_cascade(cascade):
    """切换实际请求用的模型降级链。main.py 解析 --model 或 /model 后调用。
    必须更新本模块的 QUALITY_CASCADE，因为 call_llm 循环读的是这里。"""
    global QUALITY_CASCADE
    QUALITY_CASCADE = list(cascade)


def get_session_total_tokens() -> int:
    """累计 prompt+completion token 数，跨模型求和。供 fix/audit 计算 token 预算用。"""
    with _session_tokens_lock:
        return sum(b["prompt"] + b["completion"] for b in _session_tokens_by_model.values())


def get_session_token_breakdown() -> dict:
    """返回 {"input": int, "output": int, "by_model": {model: {input, output}}}.

    给 task_log / AB 测试用。yansh batch 模式每次 main.py 调用 _session_tokens
    从空开始累计，finish_task_log 直接读就是单任务总数；多任务场景需配 baseline
    diff（task_log 内部已处理）。
    """
    with _session_tokens_lock:
        by_model = {
            m: {"input": b["prompt"], "output": b["completion"]}
            for m, b in _session_tokens_by_model.items()
        }
    return {
        "input": sum(v["input"] for v in by_model.values()),
        "output": sum(v["output"] for v in by_model.values()),
        "by_model": by_model,
    }


def _get_gemini_client():
    """每次调用刷新 OAuth token，供 Vertex AI 端点使用"""
    from config import GEMINI_BASE_URL
    try:
        import google.auth
        import google.auth.transport.requests
        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        credentials.refresh(google.auth.transport.requests.Request())
        token = credentials.token
    except Exception:
        from config import GEMINI_API_KEY
        token = GEMINI_API_KEY
    return OpenAI(api_key=token, base_url=GEMINI_BASE_URL)


def _get_ica_client():
    """专用 ICA 客户端，用于 Claude 模型（主 client 可能指向 OpenRouter）"""
    global _ica_client
    if _ica_client is None:
        import os as _os
        ica_key = (
            _os.getenv("CLAUDE_API_KEY")
            or _os.getenv("ANTHROPIC_AUTH_TOKEN")
            or _os.getenv("ANTHROPIC_API_KEY")
            or OPENROUTER_API_KEY
        )
        ica_base = (
            _os.getenv("CLAUDE_BASE_URL")
            or _os.getenv("ANTHROPIC_BASE_URL")
            or "https://api.nextgen-beta.ica.ibm.com/ica/v1"
        )
        if not ica_base.rstrip("/").endswith("/v1"):
            ica_base = ica_base.rstrip("/") + "/v1"
        _ica_client = OpenAI(api_key=ica_key, base_url=ica_base)
    return _ica_client


def _is_gemini(model: str) -> bool:
    return model is not None and ("gemini" in model)


def _is_claude(model: str) -> bool:
    return model is not None and model.startswith("claude")


def _client_for(model: str):
    if _is_gemini(model):
        return _get_gemini_client()
    if _is_claude(model):
        return _get_ica_client()
    return client


def _call_single_model(cl, model, messages, response_format=None, stream=False):
    kwargs = {"model": model, "messages": messages, "timeout": LLM_TIMEOUT_SEC}
    if response_format and not _should_skip_rf(model):
        kwargs["response_format"] = response_format
    if stream and not _is_gemini(model):
        kwargs["stream"] = True
        kwargs["stream_options"] = {"include_usage": True}
        try:
            return _handle_stream(cl.chat.completions.create(**kwargs), model)
        except Exception as e:
            if response_format and "response_format" in kwargs and _looks_like_rf_rejection(e):
                _RF_UNSUPPORTED.add(model)
                kwargs.pop("response_format", None)
                return _handle_stream(cl.chat.completions.create(**kwargs), model)
            raise
    try:
        return cl.chat.completions.create(**kwargs)
    except Exception as e:
        if response_format and "response_format" in kwargs and _looks_like_rf_rejection(e):
            _RF_UNSUPPORTED.add(model)
            kwargs.pop("response_format", None)
            return cl.chat.completions.create(**kwargs)
        raise


def _is_transient_error(exc) -> bool:
    """429 / 5xx / 连接错误"""
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


def call_llm(messages, tools=None, tool_choice=None, response_format=None,
             stream: bool | None = None, model_override: str | None = None):
    """尝试 QUALITY_CASCADE 中的模型依次降级；每模型对 429/5xx 指数退避重试。
    在子线程中执行，每 100ms 检查一次 ESC 中断。
    stream=True 且 tools=None 时启用流式输出，实时打印 token。

    P2.2 model_override：仅跑指定模型（不走 cascade），失败抛错——给 subagent 用便宜模型。
    """
    use_stream = (stream is True) or (
        stream is None and tools is None and response_format is None
    )

    result_holder = [None]
    exc_holder = [None]

    # P2.2：override 时只跑这一个模型；不传则走原 cascade
    cascade = [model_override] if model_override else QUALITY_CASCADE

    def _worker():
        for model in cascade:
            backoff = 1.0
            for attempt in range(LLM_MAX_RETRIES_PER_MODEL):
                try:
                    kwargs = {"model": model, "messages": messages, "timeout": LLM_TIMEOUT_SEC}
                    if tools is not None:
                        kwargs["tools"] = tools
                    if tool_choice is not None:
                        kwargs["tool_choice"] = tool_choice
                    if response_format is not None and not _should_skip_rf(model):
                        kwargs["response_format"] = response_format
                    cl = _client_for(model)
                    try:
                        if use_stream and not _is_gemini(model):
                            kwargs["stream"] = True
                            kwargs["stream_options"] = {"include_usage": True}
                            result_holder[0] = _handle_stream(
                                cl.chat.completions.create(**kwargs), model
                            )
                        else:
                            result_holder[0] = cl.chat.completions.create(**kwargs)
                    except Exception as inner:
                        # 后端拒 response_format 时记忆并自动重试一次（无该参数）
                        if (response_format is not None
                                and "response_format" in kwargs
                                and _looks_like_rf_rejection(inner)):
                            _RF_UNSUPPORTED.add(model)
                            kwargs.pop("response_format", None)
                            if use_stream and not _is_gemini(model):
                                result_holder[0] = _handle_stream(
                                    cl.chat.completions.create(**kwargs), model
                                )
                            else:
                                result_holder[0] = cl.chat.completions.create(**kwargs)
                        else:
                            raise
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
        scope_desc = f"override={model_override}" if model_override else "cascade"
        raise RuntimeError(f"所有模型调用均失败 ({scope_desc}): {exc_holder[0]}")

    res = result_holder[0]
    if hasattr(res, "usage") and res.usage:
        p = res.usage.prompt_tokens or 0
        c = res.usage.completion_tokens or 0
        used_model = getattr(res, "model", None) or (QUALITY_CASCADE[0] if QUALITY_CASCADE else "unknown")
        _last_request_tokens["prompt"] = p
        _last_request_tokens["completion"] = c
        _last_request_tokens["model"] = used_model
        with _session_tokens_lock:
            bucket = _session_tokens_by_model.setdefault(used_model, {"prompt": 0, "completion": 0})
            bucket["prompt"] += p
            bucket["completion"] += c

    return res


class _StreamToolCall:
    """流式累积的 tool_call，提供 model_dump 兼容 OpenAI Pydantic API"""
    def __init__(self, tc_id: str, name: str, arguments: str):
        self.id = tc_id
        self.type = "function"
        self.function = SimpleNamespace(name=name, arguments=arguments)

    def model_dump(self):
        return {
            "id": self.id,
            "type": self.type,
            "function": {"name": self.function.name, "arguments": self.function.arguments},
        }


def _handle_stream(stream_iter, model: str):
    """消费流式响应，实时打印 content；按 index 累积 tool_calls 片段；末尾抽 usage。
    返回与非流式兼容的伪 response 对象。"""
    collected_content = []
    usage_data = None
    tool_calls_buf: dict = {}

    for chunk in stream_iter:
        if interrupt.is_interrupted():
            raise interrupt.Interrupted()
        if not chunk.choices:
            if hasattr(chunk, "usage") and chunk.usage:
                usage_data = chunk.usage
            continue
        delta = chunk.choices[0].delta
        if delta and delta.content:
            sys.stdout.write(delta.content)
            sys.stdout.flush()
            collected_content.append(delta.content)
        tc_delta_list = getattr(delta, "tool_calls", None) if delta else None
        if tc_delta_list:
            for tc_delta in tc_delta_list:
                idx = getattr(tc_delta, "index", 0) or 0
                slot = tool_calls_buf.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                if getattr(tc_delta, "id", None):
                    slot["id"] = tc_delta.id
                fn = getattr(tc_delta, "function", None)
                if fn is not None:
                    if getattr(fn, "name", None):
                        slot["name"] = fn.name
                    if getattr(fn, "arguments", None):
                        slot["arguments"] += fn.arguments

    if collected_content:
        sys.stdout.write("\n")
        sys.stdout.flush()

    full_content = "".join(collected_content)

    tool_calls = None
    if tool_calls_buf:
        tool_calls = [
            _StreamToolCall(
                tool_calls_buf[i]["id"],
                tool_calls_buf[i]["name"],
                tool_calls_buf[i]["arguments"],
            )
            for i in sorted(tool_calls_buf.keys())
        ]

    message = SimpleNamespace(
        content=full_content if full_content else None,
        tool_calls=tool_calls,
        role="assistant",
    )
    finish_reason = "tool_calls" if tool_calls else "stop"
    choice = SimpleNamespace(message=message, finish_reason=finish_reason)
    usage = usage_data or SimpleNamespace(prompt_tokens=0, completion_tokens=len(full_content) // 4)
    response = SimpleNamespace(choices=[choice], usage=usage, model=model)
    return response


def show_stats():
    """显示 Token 消耗统计（按 model 分别按价计费）"""
    p_last = _last_request_tokens["prompt"]
    c_last = _last_request_tokens["completion"]
    last_model = _last_request_tokens.get("model", "")

    p_total = sum(b["prompt"] for b in _session_tokens_by_model.values())
    c_total = sum(b["completion"] for b in _session_tokens_by_model.values())
    total = p_total + c_total

    cost = 0.0
    for model, b in _session_tokens_by_model.items():
        price = get_model_price(model)
        cost += b["prompt"] / 1_000_000 * price["input"]
        cost += b["completion"] / 1_000_000 * price["output"]

    console.print("\n📊 [bold]Token 消耗统计[/bold]", highlight=False)
    console.print(f"  本次请求: prompt={p_last:,}  completion={c_last:,}  ({last_model})", highlight=False)
    console.print(f"  会话累计: prompt={p_total:,}  completion={c_total:,}  total={total:,}", highlight=False)
    if len(_session_tokens_by_model) > 1:
        for model, b in _session_tokens_by_model.items():
            price = get_model_price(model)
            sub = b["prompt"] / 1_000_000 * price["input"] + b["completion"] / 1_000_000 * price["output"]
            console.print(f"    └─ {model}: ${sub:.4f}  (in {b['prompt']:,} / out {b['completion']:,})", highlight=False)
    console.print(f"  预估费用: [green]${cost:.4f}[/green]", highlight=False)
