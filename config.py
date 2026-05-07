import os
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
