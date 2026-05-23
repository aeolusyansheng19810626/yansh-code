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

GEMINI_3_FLASH  = "google/gemini-2.5-flash"
GEMINI_31_PRO   = "google/gemini-2.5-pro"
GEMINI_API_KEY  = os.getenv("GEMINI_API_KEY")
_gcp_project    = os.getenv("GOOGLE_CLOUD_PROJECT", "yansheng-project")
_gcp_region     = os.getenv("GOOGLE_CLOUD_REGION", "us-central1")
GEMINI_BASE_URL = f"https://{_gcp_region}-aiplatform.googleapis.com/v1beta1/projects/{_gcp_project}/locations/{_gcp_region}/endpoints/openapi/"

DEEPSEEK_FLASH = "deepseek/deepseek-v4-flash"

TIER_TOP = CLAUDE_SONNET

# 主模型 + Haiku 兜底（主模型已是 Haiku 时不重复）
QUALITY_CASCADE = [TIER_TOP] if TIER_TOP == CLAUDE_HAIKU else [TIER_TOP, CLAUDE_HAIKU]

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
# 按 $1M tokens 计算。show_stats 按实际调用的模型分别计费再求和
TOKEN_PRICE_TABLE = {
    "claude-opus-4-7":             {"input": 15.0, "output": 75.0},
    "claude-sonnet-4-6":           {"input": 3.0,  "output": 15.0},
    "claude-haiku-4-5":            {"input": 1.0,  "output": 5.0},
    "google/gemini-2.5-flash":     {"input": 0.30, "output": 2.50},
    "google/gemini-2.5-pro":       {"input": 1.25, "output": 10.0},
    "deepseek/deepseek-v4-flash":  {"input": 0.07, "output": 0.28},
}
_DEFAULT_PRICE = {"input": 1.0, "output": 5.0}  # 未知模型保守按 Haiku 估算


def get_model_price(model: str) -> dict:
    return TOKEN_PRICE_TABLE.get(model, _DEFAULT_PRICE)


# 兼容旧 import：保留两个常量但不再使用
TOKEN_PRICE_INPUT  = _DEFAULT_PRICE["input"]
TOKEN_PRICE_OUTPUT = _DEFAULT_PRICE["output"]

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
    # 重构类任务调度参数：默认覆盖小到中型修改；plan 阶段 LLM 给出 expected_edits 后会动态调高
    "coder_rounds_per_file": 5,            # 单文件 coder loop 工具调用轮次基线
    "coder_edits_per_round": 3,            # 假设 LLM 平均一轮发的 edit 数（用于按 expected_edits 算所需轮次）
    "fix_soft_limit": 12,                  # fix loop 单次 attempt 工具轮次上限（基线）
    "fix_mechanical_error_bonus": 12,      # 检测到机械错（同类 TypeError 缺参等）时再追加的轮次
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
