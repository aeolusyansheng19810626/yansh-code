"""探测 ICA gateway 上 3 个新模型是否可调用：
  - claude-opus-4-7
  - gemini-3-pro-preview
  - gpt-5.4-gus

行为：每个模型发一条简短 user message，验证：
  1. ICA 接受该模型 ID
  2. 返回非空 content
  3. 返回 usage 字段（input/output token）

跑法：python scripts/probe_ica_models.py

注意：直接用 OpenAI SDK + ICA base_url，绕过 llm_client.py 的
_is_gemini/_is_claude 路由（那个会把 gemini 打到 Vertex AI）。
"""
import os
import sys
import time

# Windows cp932 兼容：强制 stdout/stderr 用 UTF-8
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

MODELS = [
    "claude-haiku-4-5",
    "claude-sonnet-4-6",
    "claude-opus-4-7",
    "gemini-3-pro-preview",
    "gpt-5.4-gus",
]

USER_MSG = "Reply with the single word OK."


def get_ica_client() -> OpenAI:
    api_key = (
        os.getenv("CLAUDE_API_KEY")
        or os.getenv("ANTHROPIC_AUTH_TOKEN")
        or os.getenv("ANTHROPIC_API_KEY")
    )
    base_url = (
        os.getenv("CLAUDE_BASE_URL")
        or os.getenv("ANTHROPIC_BASE_URL")
        or "https://api.nextgen-beta.ica.ibm.com/ica/v1"
    )
    if not base_url.rstrip("/").endswith("/v1"):
        base_url = base_url.rstrip("/") + "/v1"
    if not api_key:
        raise SystemExit("ERROR: 未找到 API key（请设置 CLAUDE_API_KEY 或 ANTHROPIC_AUTH_TOKEN）")
    return OpenAI(api_key=api_key, base_url=base_url, timeout=60.0)


def probe_one(client: OpenAI, model: str) -> dict:
    print(f"\n=== {model} ===")
    started = time.time()
    # gemini 3 pro 是推理模型，thinking token 会占用 max_tokens 预算 → 给宽松预算
    max_tok = 512 if "gemini" in model else 20
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": USER_MSG}],
            max_tokens=max_tok,
        )
        elapsed = time.time() - started
        choice = resp.choices[0] if resp.choices else None
        content = choice.message.content if choice and choice.message else ""
        finish_reason = getattr(choice, "finish_reason", None) if choice else None
        usage = resp.usage
        in_tok = getattr(usage, "prompt_tokens", None)
        out_tok = getattr(usage, "completion_tokens", None)
        print(f"  [OK] 调用成功 ({elapsed:.2f}s)")
        print(f"  content       : {content!r}")
        print(f"  finish_reason : {finish_reason}")
        print(f"  usage         : input={in_tok} output={out_tok}")
        # content 为空时打印 message 详情（gemini thinking token 走 reasoning 字段）
        if not content and choice and choice.message:
            msg_dict = choice.message.model_dump() if hasattr(choice.message, "model_dump") else dict(choice.message)
            print(f"  message_dump  : {msg_dict}")
        return {"model": model, "ok": True, "content": content,
                "elapsed": round(elapsed, 2),
                "input_tokens": in_tok, "output_tokens": out_tok}
    except Exception as e:
        elapsed = time.time() - started
        err = f"{type(e).__name__}: {e}"
        print(f"  [FAIL] 调用失败 ({elapsed:.2f}s)")
        print(f"  error   : {err}")
        return {"model": model, "ok": False, "error": err,
                "elapsed": round(elapsed, 2)}


def main():
    cl = get_ica_client()
    print(f"ICA endpoint: {cl.base_url}")
    results = [probe_one(cl, m) for m in MODELS]

    print("\n" + "=" * 60)
    print("汇总")
    print("=" * 60)
    for r in results:
        status = "OK" if r["ok"] else "FAIL"
        line = f"  [{status:4s}] {r['model']:30s} ({r['elapsed']}s)"
        if not r["ok"]:
            line += f"  ← {r['error'][:80]}"
        print(line)

    failed = [r for r in results if not r["ok"]]
    sys.exit(0 if not failed else 1)


if __name__ == "__main__":
    main()
