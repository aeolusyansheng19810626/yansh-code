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
CLAUDE_OPUS   = "claude-opus-4-8"
CLAUDE_SONNET = "claude-sonnet-4-6"
CLAUDE_HAIKU  = "claude-haiku-4-5"

# 通过 ICA gateway 可达的非 Claude 模型（用 OpenAI 兼容 SDK 直调 ICA endpoint）
# 经 scripts/probe_ica_models.py 验证可调通（2026-05-24）
ICA_GEMINI_3_PRO = "gemini-3-pro-preview"   # 推理模型，长 context；thinking 占大量 output token
ICA_GPT_5_4      = "gpt-5.4-gus"            # GPT-5.4，跨 family 容灾

# Vertex AI 直调（已弃用路径，留作历史）
GEMINI_3_FLASH  = "google/gemini-2.5-flash"
GEMINI_31_PRO   = "google/gemini-2.5-pro"
GEMINI_API_KEY  = os.getenv("GEMINI_API_KEY")
_gcp_project    = os.getenv("GOOGLE_CLOUD_PROJECT", "yansheng-project")
_gcp_region     = os.getenv("GOOGLE_CLOUD_REGION", "us-central1")
GEMINI_BASE_URL = f"https://{_gcp_region}-aiplatform.googleapis.com/v1beta1/projects/{_gcp_project}/locations/{_gcp_region}/endpoints/openapi/"

DEEPSEEK_FLASH = "deepseek/deepseek-v4-flash"

TIER_TOP = CLAUDE_SONNET  # 默认 sonnet（opus 太贵）；需 opus 时显式 --model claude-opus-4-8（solo 复杂任务）

# 主模型 + Haiku 兜底（主模型已是 Haiku 时不重复）
QUALITY_CASCADE = [TIER_TOP] if TIER_TOP == CLAUDE_HAIKU else [TIER_TOP, CLAUDE_HAIKU]

MAX_ATTEMPTS = 6
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
    "claude-opus-4-8":             {"input": 15.0, "output": 75.0},
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
    "mode": "solo",
    "max_attempts": MAX_ATTEMPTS,
    "test_command": None,
    "safe_mode": True,
    "compress_threshold": 6000,
    "keep_recent_turns": 3,
    "human_in_loop": os.getenv("HUMAN_IN_LOOP", "false").lower() == "true",
    # 重构类任务调度参数：默认覆盖小到中型修改；plan 阶段 LLM 给出 expected_edits 后会动态调高
    "coder_rounds_per_file": 8,            # 单文件 coder loop 工具调用轮次基线
    "coder_max_rounds_per_file": 40,       # 单文件轮次硬上限（已放宽；真正兜底交给无进展熔断+费用熔断）
    "coder_edits_per_round": 3,            # 假设 LLM 平均一轮发的 edit 数（保留兼容，调度已改为按 expected_edits 计）
    "coder_no_progress_rounds": 4,         # 连续 N 轮无有效编辑则熔断本文件（sonnet 直接梭哈写，4 够用；opus 探索多需调 8）
    "parallel_max_workers": 4,             # worktree 并行编排最大并发子进程数（防 ICA 限速）
    "fix_soft_limit": 12,                  # fix loop 单次 attempt 工具轮次上限（基线）
    "fix_mechanical_error_bonus": 12,      # 检测到机械错（同类 TypeError 缺参等）时再追加的轮次
    "test_gate_timeout_sec": 300,          # solo gate 外部测试超时（秒）；普通 execute_command 仍用 30s
    # 实验1：solo 强制 agent 留下正规测试的策略。off=现状；role=强化提示词（概率）；gate=硬判定拦截（确定性）；pre=写实现前内建 PreToolUse 拦截（最前移，不依赖 agent 收尾）
    "solo_test_enforcement": os.getenv("SOLO_TEST_ENFORCEMENT", "off"),
    # pre 模式：同一实现文件连续被拦截 N 次仍不补测试时放行兜底，防死循环烧轮次
    "solo_pretool_max_block": int(os.getenv("SOLO_PRETOOL_MAX_BLOCK", "3")),
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
