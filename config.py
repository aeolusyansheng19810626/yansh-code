import os
import json
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

# 通过OpenRouter调用DeepSeek
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEEPSEEK_MODEL = "deepseek/deepseek-v4-flash"

# 5-tier 模型配置（统一使用DeepSeek）
TIER_TOP       = DEEPSEEK_MODEL
TIER_UPPER_MID = DEEPSEEK_MODEL
TIER_MID       = DEEPSEEK_MODEL
TIER_LOW       = DEEPSEEK_MODEL
TIER_DEBUG     = DEEPSEEK_MODEL

QUALITY_CASCADE = [TIER_TOP, TIER_UPPER_MID, TIER_MID, TIER_LOW, TIER_DEBUG]

MAX_ATTEMPTS = 3
WORKSPACE_DIR = "workspace"

# ---------- #43 项目级配置文件 ----------

_CONFIG_FILE = Path(WORKSPACE_DIR) / ".yansh" / "config.json"

_DEFAULTS = {
    "model": DEEPSEEK_MODEL,
    "mode": "auto",
    "max_attempts": MAX_ATTEMPTS,
    "test_command": None,
    "safe_mode": True,
    "compress_threshold": 6000,
    "keep_recent_turns": 3,
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
