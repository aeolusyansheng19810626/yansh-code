"""P1.0：探测 ICA gateway 是否透传 cache_control。

行为：发两次相同长 system prompt，看 usage 是否报 cache_creation_input_tokens
和 cache_read_input_tokens 字段。

判断：
  - 两个字段都出现且 cache_read > 0 → ICA 透传 cache，P1.1 可做
  - 两个字段都为 0 或不存在 → ICA 不透传，P1.1 跳过

跑法： python scripts/probe_ica_cache.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from llm_client import _get_ica_client

MODEL = "claude-sonnet-4-6"

# 构造 ≥1024 token 的 system prompt（Anthropic cache 最小命中粒度）
LONG_PREFIX = (
    "You are a helpful coding assistant. " * 200
    + "\n\nFollow these rules strictly:\n"
    + "\n".join(f"- Rule {i}: be precise about technical details." for i in range(50))
)

USER_MSG = "Reply with the single word OK."


def call_once(label, cl):
    print(f"\n=== {label} ===")
    # 走 content blocks 形式塞 cache_control
    messages = [
        {
            "role": "system",
            "content": [
                {
                    "type": "text",
                    "text": LONG_PREFIX,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
        },
        {"role": "user", "content": USER_MSG},
    ]
    try:
        resp = cl.chat.completions.create(
            model=MODEL,
            messages=messages,
            timeout=60,
            extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
        )
    except Exception as e:
        print(f"call failed: {type(e).__name__}: {e}")
        return None
    usage = resp.usage
    print(f"prompt_tokens     : {getattr(usage, 'prompt_tokens', None)}")
    print(f"completion_tokens : {getattr(usage, 'completion_tokens', None)}")
    # Anthropic-style cache 字段（OpenAI SDK 兼容时可能进 model_extra 或直接挂 usage）
    for attr in ("cache_creation_input_tokens", "cache_read_input_tokens"):
        v = getattr(usage, attr, None)
        if v is None and hasattr(usage, "model_extra"):
            v = (usage.model_extra or {}).get(attr)
        print(f"{attr:32s}: {v}")
    # 全字段 dump
    try:
        print("usage raw:", json.dumps(usage.model_dump(), default=str))
    except Exception:
        print("usage raw:", repr(usage))
    return usage


def main():
    cl = _get_ica_client()
    print(f"base_url: {cl.base_url}")
    u1 = call_once("call #1 (expect cache_creation > 0)", cl)
    u2 = call_once("call #2 (expect cache_read > 0)", cl)

    print("\n=== conclusion ===")
    if u1 is None or u2 is None:
        print("调用失败 → 无法判断")
        sys.exit(2)

    def get(u, k):
        v = getattr(u, k, None)
        if v is None and hasattr(u, "model_extra"):
            v = (u.model_extra or {}).get(k)
        return v or 0

    cc1 = get(u1, "cache_creation_input_tokens")
    cr2 = get(u2, "cache_read_input_tokens")
    if cc1 > 0 and cr2 > 0:
        print(f"✅ ICA 透传 cache_control（cc={cc1}, cr={cr2}）→ P1.1 可做")
        sys.exit(0)
    else:
        print(f"❌ ICA 不透传或忽略（cc={cc1}, cr={cr2}）→ P1.1 跳过")
        sys.exit(1)


if __name__ == "__main__":
    main()
