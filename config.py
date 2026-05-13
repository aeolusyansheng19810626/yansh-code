import os
import json
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ---------- [已弃用] DeepSeek / OpenRouter 配置 ----------
# OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
# OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
# DEEPSEEK_MODEL = "deepseek/deepseek-v4-flash"
# TIER_TOP       = DEEPSEEK_MODEL
# TIER_UPPER_MID = DEEPSEEK_MODEL
# TIER_MID       = DEEPSEEK_MODEL
# TIER_LOW       = DEEPSEEK_MODEL
# TIER_DEBUG     = DEEPSEEK_MODEL
# QUALITY_CASCADE = [TIER_TOP, TIER_UPPER_MID, TIER_MID, TIER_LOW, TIER_DEBUG]

# ---------- API 配置（LLM_API_KEY）----------
# DeepSeek 走 OpenRouter；Claude 走 IBM ICA 网关
# 优先读 OPENROUTER_API_KEY（DeepSeek），回落到 CLAUDE_API_KEY（Claude/ICA）
# 注：变量名沿用 OPENROUTER_API_KEY 以减少 import 改动，语义上等同于 LLM_API_KEY
OPENROUTER_API_KEY = (
    os.getenv("OPENROUTER_API_KEY")
    or os.getenv("CLAUDE_API_KEY")
    or os.getenv("ANTHROPIC_AUTH_TOKEN")
    or os.getenv("ANTHROPIC_API_KEY")
)
# 若用 OpenRouter（DeepSeek），base_url 为 openrouter.ai；否则用 ICA 端点
_using_openrouter = bool(os.getenv("OPENROUTER_API_KEY"))
OPENROUTER_BASE_URL = (
    "https://openrouter.ai/api/v1" if _using_openrouter
    else (
        os.getenv("CLAUDE_BASE_URL")
        or os.getenv("ANTHROPIC_BASE_URL")
        or "https://api.nextgen-beta.ica.ibm.com/ica/v1"
    )
)

# 模型 ID（按 ICA 网关接受的格式填写，示例供参考；ID 形如 claude-opus-4-7 / claude-sonnet-4-6 / claude-haiku-4-5）
CLAUDE_OPUS   = "claude-opus-4-7"
CLAUDE_SONNET = "claude-sonnet-4-6"
CLAUDE_HAIKU  = "claude-haiku-4-5"

GEMINI_3_FLASH  = "gemini-3-flash-preview"
GEMINI_31_PRO   = "gemini-3.1-pro-preview"
GEMINI_API_KEY  = os.getenv("GEMINI_API_KEY")
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"

# 5-tier 级联：顶端用 Opus，其次 Sonnet，失败兜底 Haiku
DEEPSEEK_FLASH = "deepseek/deepseek-v4-flash"

TIER_TOP       = DEEPSEEK_FLASH
TIER_UPPER_MID = DEEPSEEK_FLASH
TIER_MID       = DEEPSEEK_FLASH
TIER_LOW       = DEEPSEEK_FLASH
TIER_DEBUG     = DEEPSEEK_FLASH

QUALITY_CASCADE = [TIER_TOP, TIER_UPPER_MID, TIER_MID, TIER_LOW, TIER_DEBUG]

MAX_ATTEMPTS = 3
WORKSPACE_DIR = "workspace"


def set_workspace_dir(path: str):
    """在 main() 解析 --cwd 后调用，更新 WORKSPACE_DIR 及依赖它的路径。
    必须在 agent.py / tools.py 的模块级路径初始化完成后再调用
    agent._reinit_paths() 和 tools._reinit_paths() 使变更生效。"""
    global WORKSPACE_DIR, _CONFIG_FILE
    WORKSPACE_DIR = path
    _CONFIG_FILE = Path(path) / ".yansh" / "config.json"


# ---------- #62 Token 价格配置 ----------
# 按 $1M Tokens 计算；当前默认模型：DeepSeek V4 Flash (via OpenRouter)
# DeepSeek V4 Flash: $0.07/1M input, $0.28/1M output（参考 openrouter.ai，以实际账单为准）
TOKEN_PRICE_INPUT  = 0.07  # $0.07 / 1M input tokens
TOKEN_PRICE_OUTPUT = 0.28  # $0.28 / 1M output tokens

# ---------- #43 项目级配置文件 ----------

_CONFIG_FILE = Path(WORKSPACE_DIR) / ".yansh" / "config.json"

_DEFAULTS = {
    "model": TIER_TOP,
    "mode": "auto",
    "max_attempts": MAX_ATTEMPTS,
    "test_command": None,
    "safe_mode": True,
    "compress_threshold": 6000,
    "keep_recent_turns": 3,
    "human_in_loop": os.getenv("HUMAN_IN_LOOP", "false").lower() == "true",
}

_effective_config: dict = dict(_DEFAULTS)


def load_project_config() -> dict:
    """加载 .yansh/config.json，不存在则使用默认值，返回生效配置"""
    global _effective_config
    _effective_config = dict(_DEFAULTS)
    if _CONFIG_FILE.exists():
        try:
            data = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
            for k, v in data.items():
                if k in _DEFAULTS:
                    _effective_config[k] = v
        except Exception:
            pass
    return dict(_effective_config)


def get_config() -> dict:
    return dict(_effective_config)


def override_config(**kwargs):
    """CLI 参数优先级高于配置文件，覆盖对应键"""
    for k, v in kwargs.items():
        if v is not None and k in _effective_config:
            _effective_config[k] = v
