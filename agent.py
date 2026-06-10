import hashlib
import json
import os
import re
import shutil
import threading
from datetime import datetime
from console_shared import console, set_json_mode as _set_json_mode
from pathlib import Path
import config as _config_mod
from config import (
    WORKSPACE_DIR, get_config,
    get_model_price,
)
from tools import (
    write_file, read_file, execute_command, list_files, replace_in_file,
    get_symbol_definition, search_in_files, move_file, apply_patch,
    list_symbols, replace_symbol, fetch_webpage, search_docs, append_to_file,
    find_references, glob_files, git_diff, git_log, workspace_symbols,
    directory_summary, delete_file, task_complete,
    update_plan_draft, exit_plan_mode_signal, dispatch_subagent,
    save_memory, recall_memory,
)
import interrupt
import tools as _tools_mod
import mcp_client as _mcp_mod
import hooks as _hooks_mod
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
from linter import detect_project_type, _detect_python_test_cmd
import llm_client as _llm_mod
from tools_schema import TOOLS, READONLY_TOOL_NAMES
from llm_client import (
    client, _ica_client, _get_ica_client, _get_gemini_client,
    _is_gemini, _is_claude, _client_for, _call_single_model,
    _is_transient_error, call_llm, _StreamToolCall, _handle_stream,
    show_stats, LLM_TIMEOUT_SEC, LLM_MAX_RETRIES_PER_MODEL,
    get_session_total_tokens, CostExceededError,
)


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

# P2 #8 Skills：当前任务/对话激活的 skill prompt 片段。run() 入口算一次写入；
# plan/code/audit/fix 各自的 system prompt 末尾拼这个变量。空字符串=无命中。
_ACTIVE_SKILLS_PROMPT: str = ""


def _append_active_prompts(sys_prompt: str) -> str:
    """P4-2：把全局激活的 skill prompt 和 memory index 追加到 system prompt 末尾。

    6 处 plan/code/audit/fix/plan_chat/subagent prompt 拼接都走这里——
    后续要加新注入点（git context / project rules）只改这一个函数。
    """
    if _ACTIVE_SKILLS_PROMPT:
        sys_prompt += _ACTIVE_SKILLS_PROMPT
    if _ACTIVE_MEMORY_INDEX:
        sys_prompt += _ACTIVE_MEMORY_INDEX
    return sys_prompt

# P2 #12 Memory 索引（每次 run 入口刷新；plan/code/audit/fix system prompt 拼接）
_ACTIVE_MEMORY_INDEX: str = ""

# P2 #9 子 Agent：递归防护 + 进程级累计 stats（用于 /subagent stats 命令）。
# stats 是 process 生命周期的累加，不参与 Session.snapshot/restore（重启清零无所谓）。
# P2-4 安全：保护 TOOLS 列表的并发读写。init_mcp 用 TOOLS[:]=... 原地修改
# 时，并发跑的子 agent 在 _subagent_tools_for_role 里迭代 TOOLS——会触发
# RuntimeError: list changed size during iteration。所有写 + 读迭代都走此锁。
_TOOLS_LOCK = threading.Lock()

# P4-5 重构：subagent 相关的状态 + 函数搬到 subagent.py，这里保留 re-export
# 让既有 caller（含 state.Session、test_subagent 等）无需改动。
from subagent import (
    _subagent_state,
    _SUBAGENT_STATS,
    _SUBAGENT_STATS_LOCK,
    _SUBAGENT_HARD_CAP,
    _SUBAGENT_CONCURRENCY_CAP,
    _is_in_subagent,
    _set_in_subagent,
)

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

# solo mode：单一连续 context 端到端 agent（自主规划→读写跑修→自测）
# 与逐文件 code() 并存，验证「架构 vs 模型」命题。详见 plan / notes shadow R9。
_SOLO_SOFT_LIMIT     = 120       # 主 loop 工具调用轮次上限（连续 context，复杂多文件任务需大轮数）
_SOLO_TOKEN_BUDGET   = 600_000   # token 增量软提醒阈值（超过注入一次收敛提示；硬熔断交给 --max-cost）
_SOLO_NO_PROGRESS_CAP = 6        # 连续 N 轮无写编辑先注提醒，2N 轮熔断（agent 级，区别于逐文件 no_progress）
_SOLO_GATE_MAX_ROUNDS = 8        # 外部 test gate 回灌最大轮数
_SOLO_GATE_DRIVE_LIMIT = 15      # 每次 gate 回灌最多给 agent 15 轮修复机会

# backlog #1: fix loop baseline 失败识别
# 进入 code() 前跑一次 test_command 捕获 baseline failures
# test+fix 循环里 returncode!=0 但 current\baseline 为空时视为通过
_BASELINE_FAILURES: set = set()

# [Debt 3] 任务开始前（code() 运行前）的 tests/ 快照，用于检测 coder 越权新建的测试文件
_PRE_TASK_TEST_FILES: set = set()

# [P1 #3] 机械错 detector：fix loop 触发追加预算的错误模式
# 三类共用同一 bonus（signature 改 / 重命名 / 改属性时常批量出现）
_MECH_ERROR_PATTERNS: list = [
    (r"TypeError:.+?missing\s+\d+\s+required\s+(?:positional|keyword)\s+argument",
     "TypeError missing argument"),
    (r"NameError:\s+name\s+'[^']+'\s+is\s+not\s+defined",
     "NameError"),
    (r"AttributeError:\s+'[^']+'\s+object\s+has\s+no\s+attribute\s+'[^']+'",
     "AttributeError"),
]

# [P1 #6] baseline 识别用户请求过滤
# 用户明确要求修测试 / 修 bug 时不能用 "current failures ⊆ baseline → 视为通过" 逻辑
# 否则会静默漏修（task #4 暴露：注入 bug 后 1 failed → yansh 把它视为 pre-existing 跳过）
_TEST_FIX_KEYWORDS_ZH: list = [
    "测试失败", "失败的测试", "失败测试", "修测试", "修复测试", "修 测试",
    "测试错误", "测试不通过", "测试不过", "测试报错",
    "单测失败", "单测不过", "单测不通过", "单测报错", "单测错误",
    "修 bug", "修复 bug", "修bug", "修复bug", "改 bug", "改bug", "解决 bug", "解决bug",
]
_TEST_FIX_KEYWORDS_EN: list = [
    "fix test", "fix the test", "fix tests", "fix the tests",
    "failing test", "failing tests", "test failure", "tests fail",
    "unit test fail", "pytest fail", "pytest failure",
    "make test pass", "make the test pass", "make tests pass", "make the tests pass",
    "fix bug", "fix the bug", "fix bugs", "resolve bug", "fix failing",
]


def _prompt_requests_test_fix(prompt: str) -> bool:
    """[P1 #6] 检查用户 prompt 是否明确要求修测试 / 修 bug。
    命中 → fix loop 跳过 baseline 子集判定，强制走完整 fix（不能把当前失败当 pre-existing 视为通过）。
    """
    if not prompt:
        return False
    if any(k in prompt for k in _TEST_FIX_KEYWORDS_ZH):
        return True
    p_low = prompt.lower()
    return any(k in p_low for k in _TEST_FIX_KEYWORDS_EN)




# [P1 #4] coder summary 是否在说"本任务无需改动"
# 命中 → multi-file 循环短路：剩余 expected_edits 不再处理（避免 task #2 烧 91 万 token 改 task 范围外）
# 只保留"强信号"——单独成立时几乎不可能歧义；移除"已存在"/"已经实现"等弱信号
# （它们经常出现在"X 已经实现，但还要改 Y"这类局部描述里 → 误吞剩余文件）。
# 实测误报样本（不应命中）：
#   "已经实现了 X，但还需要 Y"  / "Already implemented X but need Y"
#   "都已经" / "X 已存在" 单独
_NO_CHANGES_KEYWORDS_ZH: list = [
    "无需修改", "无需改动", "无需更改", "无需调整",
    "不需要修改", "不需要改动", "不需修改", "不需改动",
]
_NO_CHANGES_KEYWORDS_EN: list = [
    "no changes needed", "no change needed",
    "no modifications needed", "no modification needed",
    "no edits needed", "no edit needed",
    "nothing to change", "nothing to modify", "nothing to fix",
]


def _summary_says_no_changes(summary: str) -> bool:
    """[P1 #4] coder task_complete 的 summary 是否明确表示"本任务无需修改"。
    只匹配强信号关键词，避免"X 已经实现但还要改 Y"这类局部描述误吞 multi-file 短路。
    """
    if not summary:
        return False
    if any(k in summary for k in _NO_CHANGES_KEYWORDS_ZH):
        return True
    s_low = summary.lower()
    return any(k in s_low for k in _NO_CHANGES_KEYWORDS_EN)


# [P1 #5] plan 阶段是否需要先探索代码
# 只保留**强信号组合词**——单字"兼容性"/"现有实现"/"compatibility"/"code details"
# 在普通 feature 需求里太常见，会误吞导致每次多一次 explorer 调用（5-10K token 浪费）。
_PLAN_NEEDS_EXPLORATION_KEYWORDS_ZH: list = [
    "改动范围", "改造方案",
    "具体行号", "对应行号",
    "兼容分析", "兼容性分析",
    "影响范围", "影响分析",
    "调用关系", "调用链",
]
_PLAN_NEEDS_EXPLORATION_KEYWORDS_EN: list = [
    "specific lines", "line numbers",
    "affected files", "scope of changes", "change scope",
    "compatibility analysis", "impact analysis", "impact scope",
    "call graph", "call chain",
    "existing implementation",
]


def _plan_needs_exploration(requirement: str) -> bool:
    """[P1 #5] 用户需求是否要求 plan 输出"含代码细节"的文档/方案。
    命中 → plan() 先派 explorer 扫码，避免凭概要写错。
    """
    if not requirement:
        return False
    if any(k in requirement for k in _PLAN_NEEDS_EXPLORATION_KEYWORDS_ZH):
        return True
    r_low = requirement.lower()
    return any(k in r_low for k in _PLAN_NEEDS_EXPLORATION_KEYWORDS_EN)


# ── 任务复杂度路由：关键词表 ──────────────────────────────────────────────────

_READONLY_KEYWORDS_ZH: list[str] = [
    "分析", "解释", "说明", "描述", "理解", "梳理",
    "总结", "概括", "归纳",
    "在哪里", "在哪", "哪里定义", "定义在哪", "哪个文件", "找一下", "找找",
    "调用链", "调用关系", "依赖关系", "引用关系",
    "列出", "列举", "枚举",
    "审查", "审阅", "检查", "排查", "诊断",
    "有没有bug", "有没有 bug", "有没有问题", "是否有问题",
    "代码质量", "潜在问题", "安全问题",
    "并发条件", "竞态", "线程安全",
    "文档", "注释",
    "评估", "评价", "比较", "对比", "权衡",
    "可行性",
]
_READONLY_KEYWORDS_EN: list[str] = [
    "analyze", "analyse", "explain", "describe", "understand",
    "summarize", "summarise", "overview",
    "where is", "where are", "find", "locate", "list all",
    "call chain", "call graph", "dependency",
    "review", "audit", "inspect", "diagnose",
    "is there a bug", "any issues", "any problems",
    "code quality", "potential issue", "security issue",
    "concurrency", "race condition", "thread safety",
    "document", "comment",
    "compare", "contrast", "trade-off",
    "feasibility",
]

# 命中否定词 → 不走 readonly（用户明确要改代码）
_WRITE_NEGATION_KEYWORDS_ZH: list[str] = [
    "修改", "修复", "改一下", "改掉", "更新", "重构", "重命名",
    "创建", "新建", "添加", "增加", "删除", "移除",
    "生成", "补充", "加上", "加一个", "加个",
    "帮我实现", "帮你实现", "来实现", "去实现",
    # 裸"实现"不加：会误匹配"实现方式"等名词形式
    # 动宾结构型写入信号（任务9误判补全；避免裸名词如"功能/接口"误匹配readonly描述）
    "新增", "字段", "单测", "单元测试", "子命令",
]
_WRITE_NEGATION_KEYWORDS_EN: list[str] = [
    "fix", "modify", "change", "update", "rename",
    "implement", "create", "add", "remove", "delete",
    "generate", "build", "make",
    # "write" 不加：会误匹配函数名 write_file 等
    # 领域写入信号（去掉 cli/--/raise 等易误匹配的宽泛词）
    "unit test", "field", "exception", "flag",
]

# complex 信号，优先级最高
_COMPLEX_KEYWORDS_ZH: list[str] = [
    "重构", "架构", "整个项目", "全部文件", "所有文件",
    "迁移", "升级", "大规模", "从头", "全新实现",
    "改动范围", "影响范围", "影响分析", "兼容性分析",
]
_COMPLEX_KEYWORDS_EN: list[str] = [
    "refactor", "redesign", "migrate", "migration",
    "all files", "entire project", "whole codebase",
    "impact analysis", "compatibility analysis",
    "architecture", "large scale", "large-scale",
    "from scratch",
]

_CLASSIFY_SYSTEM = """\
Classify the coding task into exactly one of three categories. Reply with only the single word — no explanation.

  readonly  — pure analysis/review/explanation, no code changes required
  simple    — single-file or small change, scope is clear
  complex   — multi-file, architectural, or large-scale change

Rules:
1. Contains write actions (modify/fix/implement/create/add/remove/raise/fix bug) → never readonly
2. Pure analysis/explanation/review/investigation with no code change required → readonly
3. Multi-file or architectural change → complex
4. Contains concrete add/modify actions (add field, add test, add param, add branch, raise exception, new interface) even if phrased analytically → never readonly
5. Otherwise → simple"""


def _llm_classify_task(requirement: str) -> str:
    """LLM 兜底分类器（Haiku）。失败时抛异常由调用方处理。"""
    from llm_client import call_llm
    messages = [
        {"role": "user", "content": _CLASSIFY_SYSTEM + "\n\n任务：" + requirement[:500]},
    ]
    resp = call_llm(messages, tools=None, stream=False,
                    model_override="claude-haiku-4-5")
    text = (resp.choices[0].message.content or "").strip().lower()
    if "readonly" in text:
        return "readonly"
    if "complex" in text:
        return "complex"
    return "simple"


def _classify_task(requirement: str) -> str:
    """[路由] 判断任务复杂度：readonly / simple / complex。

    优先级：complex 关键词 > 写入否定词（覆盖 readonly）> readonly 关键词 > LLM 兜底 > simple。

    - readonly: 走 audit() 路径，跳过 plan/code/test/fix
    - simple:   走 code 路径，注入 hint 压缩 plan 规模
    - complex:  走完整 pipeline，不变
    """
    if not requirement:
        return "simple"
    req_low = requirement.lower()

    # 1. complex 优先
    if any(k in requirement for k in _COMPLEX_KEYWORDS_ZH) or \
       any(k in req_low for k in _COMPLEX_KEYWORDS_EN):
        return "complex"

    # 2. 写入否定词检测——先剥离"不要修改X"这类否定短语，
    #    否则其中的"修改"会被 _WRITE_NEGATION 误判为写入信号（原设计正因此把
    #    禁止修改判断前置；改为剥离后即可让两者正确共存）。
    _NO_MODIFY_ZH = ["不要修改", "不修改", "不能修改", "禁止修改", "不要改代码",
                     "不要动代码", "不要编辑", "请勿修改", "勿改"]
    _NO_MODIFY_EN = ["do not modify", "don't modify", "do not change", "don't change",
                     "no modifications", "read only", "read-only"]
    _stripped = requirement
    for _neg in _NO_MODIFY_ZH:
        _stripped = _stripped.replace(_neg, "")
    _stripped_low = _stripped.lower()
    for _neg in _NO_MODIFY_EN:
        _stripped_low = _stripped_low.replace(_neg, "")
    has_write = (
        any(k in _stripped for k in _WRITE_NEGATION_KEYWORDS_ZH) or
        any(k in _stripped_low for k in _WRITE_NEGATION_KEYWORDS_EN)
    )

    # 2.5 明确禁止修改 → readonly，但仅当（剥离否定短语后）没有任何写入动词时。
    #   "分析X，不要修改任何代码"（剥离后无写入动词）→ readonly
    #   "修复bug，但不要修改函数签名"（剥离后仍含"修复"）→ 编辑任务，不算 readonly
    _has_no_modify = (
        any(k in requirement for k in _NO_MODIFY_ZH) or
        any(k in req_low for k in _NO_MODIFY_EN)
    )
    if _has_no_modify and not has_write:
        return "readonly"

    # 3. readonly 关键词（无写入信号时）
    if not has_write:
        if any(k in requirement for k in _READONLY_KEYWORDS_ZH) or \
           any(k in req_low for k in _READONLY_KEYWORDS_EN):
            return "readonly"

    # 4. LLM 兜底（≥20 字的模糊输入）
    if len(requirement.strip()) >= 20:
        try:
            result = _llm_classify_task(requirement)
            if result == "readonly" and not has_write:
                return "readonly"
            if result == "complex":
                return "complex"
        except Exception:
            pass  # 失败降级为 simple

    return "simple"


# ── simple-fast 路径：从 requirement 提取文件名 + 资格判断 ─────────────────────

_FILENAME_RE = re.compile(
    r'`([^`]+\.(?:py|md|txt|json|yaml|yml|toml|cfg|ini|sh))`'  # 反引号包裹优先
    r'|(?<!\w)([\w/\\.-]+\.(?:py|md|txt|json|yaml|yml|toml|cfg|ini|sh))(?![\w.])',  # 裸文件名（m2修复：排除.bak等后缀）
    re.IGNORECASE,
)


def _extract_filename_from_requirement(requirement: str) -> str | None:
    """从 requirement 中提取第一个目标文件名（相对路径）。找不到返回 None。"""
    for m in _FILENAME_RE.finditer(requirement):
        fname = (m.group(1) or m.group(2)).replace("\\", "/").strip("./")
        if not fname:
            continue
        # m1 修复：跳过 URL（匹配起点前有 :// 或 / 说明是 URL 路径的一部分）
        start = m.start(2) if m.group(2) else -1
        if start > 0 and requirement[max(0, start - 3):start] in ("://", "//"):
            continue
        if "://" in requirement[max(0, start - 10):start + len(fname)]:
            continue
        return fname
    return None


def _simple_fast_eligible(requirement: str) -> bool:
    """simple-fast 路径资格检查：requirement 明确点名文件 + 无 complex/exploration 信号。

    False → 回退现有 hint 注入路径（仍走完整 plan）。
    """
    if not _extract_filename_from_requirement(requirement):
        return False
    # m5 修复：多文件 → 不走 fast，回退 plan
    all_matches = [
        (m.group(1) or m.group(2)).replace("\\", "/").strip("./")
        for m in _FILENAME_RE.finditer(requirement)
        if (m.group(1) or m.group(2)) and "://" not in (m.group(1) or m.group(2))
    ]
    if len(all_matches) > 1:
        return False
    req_low = requirement.lower()
    if any(k in requirement for k in _COMPLEX_KEYWORDS_ZH) or \
       any(k in req_low for k in _COMPLEX_KEYWORDS_EN):
        return False
    if _plan_needs_exploration(requirement):
        return False
    # 只读/探索信号 → 不走 fast（task51 实测：含"不要修改"被写入否定词误匹配绕过了 readonly 路由）
    _READONLY_SIGNALS = ["不要修改", "不修改", "不能修改", "禁止修改", "不要改代码", "不要动代码"]
    if any(k in requirement for k in _READONLY_SIGNALS):
        return False
    return True


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
    global _BATCH_MODE
    _BATCH_MODE = enabled
    _tools_mod.set_batch_mode(enabled, strict=strict)
    if json_output:
        _set_json_mode(True)


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
    expected_edits: int = 0  # plan-driven 调度：本文件预计 edit 数；coder 据此动态调高每文件轮次上限


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
    if not content.strip():
        # 空内容：直接重发原 prompt（不带空 assistant 消息），省 tokens 也避免 ICA 拒绝空 assistant
        retry_messages = list(messages)
    else:
        fix_prompt = (
            f"上一轮输出存在问题，请修正后重新输出完整合法 JSON（无多余说明、无 markdown 围栏）。\n"
            f"问题：{err}\n"
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
    """从 TOOLS 中筛选出名字在 allowed_names 集合内的工具。审计/只读人格用。

    P2-4 并发：迭代 TOOLS 必须持 _TOOLS_LOCK，否则与 init_mcp 的 TOOLS[:]=...
    原地修改并发时会 RuntimeError。
    """
    with _TOOLS_LOCK:
        return [t for t in TOOLS if t["function"]["name"] in allowed_names]


# ============================================================
# P2.1: read_file 命中检测 — 同一 agent 实例内重复 read_file (filename, offset, limit)
# 不重新调真工具、不把 content 再塞进 messages，返回短 error+hint 让 LLM 复用
# 历史 messages 中已有的 content。
#
# 关键：cache 必须 per-thread（thread-local），否则并发子 agent（_run_subagent
# 在独立线程）会互相命中——子 agent 的 messages 不含父 agent 的 read 结果，
# 命中拒绝会让子 agent 拿不到 content。每个 thread 独立 set，互不污染。
# ============================================================
_read_cache_state = threading.local()


def _get_read_cache() -> set:
    """返回当前线程的 read cache set（不存在就创建）"""
    s = getattr(_read_cache_state, "cache", None)
    if s is None:
        s = set()
        _read_cache_state.cache = s
    return s


def _read_cache_clear():
    """清当前线程的 cache 和计数器。每次 init_task_log（主线程任务边界）调用。"""
    _read_cache_state.cache = set()
    _read_cache_state.total = 0  # P2.1 命中率：总调用次数
    _read_cache_state.hits = 0   # P2.1 命中率：命中次数


_CACHE_SENTINEL = "__ALL__"  # limit/max_bytes 缺省 = 读到底，统一规范化


def _read_cache_key(args: dict) -> tuple:
    """规范化 cache key，提升同文件不同默认参数读取的命中率。
    offset None/0/1 → 1（均表示从首行起）；limit/max_bytes None → sentinel（读到底）。
    """
    _off = args.get("offset")
    try:
        _off = int(_off) if _off is not None else 1
    except (TypeError, ValueError):
        _off = 1
    if _off in (0, 1):
        _off = 1

    def _norm(v):
        if v is None:
            return _CACHE_SENTINEL
        try:
            return int(v)
        except (TypeError, ValueError):
            return _CACHE_SENTINEL

    return (
        str(args.get("filename") or ""),
        _off,
        _norm(args.get("limit")),
        _norm(args.get("max_bytes")),
    )


def _read_cache_hit_or_record(args: dict) -> bool:
    """命中返回 True；未命中记录后返回 False。每个线程独立 set。"""
    key = _read_cache_key(args)
    if not key[0]:
        return False  # 空 filename 直接放行让 read_file 自己报错
    # P2.1 命中率：每次调用 total +1
    _read_cache_state.total = getattr(_read_cache_state, "total", 0) + 1
    cache = _get_read_cache()
    if key in cache:
        _read_cache_state.hits = getattr(_read_cache_state, "hits", 0) + 1
        return True
    cache.add(key)
    return False


def _read_cache_stats() -> tuple:
    """返回 (total, hits, hit_rate)。hit_rate 为 0.0 当 total=0。"""
    total = getattr(_read_cache_state, "total", 0)
    hits = getattr(_read_cache_state, "hits", 0)
    hit_rate = hits / total if total > 0 else 0.0
    return total, hits, hit_rate


def _read_cache_merge(delta_total: int, delta_hits: int) -> None:
    """把子 agent worker 线程的 read_cache delta 合并到当前线程（通常是主线程）。

    用途：dispatch_subagent 并发跑在 ThreadPoolExecutor worker 线程时，
    worker 的 read_cache_state 独立于主线程；并发分支跑完后由 _dispatch_tool_calls
    主线程调本函数把 delta 累加上来，确保 _print_read_cache_summary 汇总完整。

    串行分支（subagent 跑在调用线程）不要调本函数——delta 已直接计入。
    """
    if delta_total <= 0:
        return
    _read_cache_state.total = getattr(_read_cache_state, "total", 0) + int(delta_total)
    _read_cache_state.hits = getattr(_read_cache_state, "hits", 0) + int(delta_hits)


def _estimate_messages_tokens(messages) -> int:
    """P2 #4-B1：粗估 messages 序列的 token 数。

    用 len(json.dumps(...)) // 4 近似（cc 文档 "4 chars ≈ 1 token"，Sonnet/Haiku 都准）。
    不依赖外部 tokenizer 库，足够给 auto-compact 触发条件用。
    无法序列化的字段（如 MagicMock）走 default=str 兜底。
    """
    if not messages:
        return 0
    try:
        s = json.dumps(messages, ensure_ascii=False, default=str)
    except Exception:
        s = str(messages)
    return len(s) // 4


def _msg_role(m):
    """统一从 dict 或 SDK message 对象取 role"""
    if isinstance(m, dict):
        return m.get("role")
    return getattr(m, "role", None)


def _split_messages_into_pairs(rest):
    """P2 #4-B2：把 head 之后的 messages 按 assistant 边界切成 pair。

    每个 pair = 一条 assistant message + 紧随的 tool messages（配对完整）。
    若 rest 起始处有零散 user message（无 assistant 在前），单独成 pair。
    保证不破坏 tool_use/tool_result 配对。
    """
    pairs = []
    current = []
    for m in rest:
        role = _msg_role(m)
        if role == "assistant":
            if current:
                pairs.append(current)
            current = [m]
        else:
            if current:
                current.append(m)
            else:
                pairs.append([m])
    if current:
        pairs.append(current)
    return pairs


def _flatten_pairs_for_summary(pairs) -> str:
    """把若干 pair 拼成一段对话文本给 summarizer LLM 看"""
    chunks = []
    for p in pairs:
        for m in p:
            role = _msg_role(m) or "?"
            if isinstance(m, dict):
                content = m.get("content", "")
                tool_calls = m.get("tool_calls")
            else:
                content = getattr(m, "content", "") or ""
                tool_calls = getattr(m, "tool_calls", None)
            chunks.append(f"[{role}] {content}")
            if tool_calls:
                for tc in tool_calls:
                    if isinstance(tc, dict):
                        fn = tc.get("function", {}).get("name", "?")
                        args = tc.get("function", {}).get("arguments", "")
                    else:
                        fn = getattr(getattr(tc, "function", None), "name", "?")
                        args = getattr(getattr(tc, "function", None), "arguments", "")
                    chunks.append(f"  [tool_call] {fn}({args})")
    return "\n".join(chunks)


_SUMMARIZE_SYSTEM = (
    "你是对话历史压缩助手。下面是一段 coder agent 的历史对话（含工具调用与结果）。"
    "请用简洁中文概括：①已调过哪些工具/读了哪些文件 ②关键发现/读到的内容要点 "
    "③已经做了哪些代码改动 ④当前进展和未完成的任务 ⑤遇到的报错/卡点 "
    "⑥已验证可用的运行环境事实（如：哪个 python/pytest 命令可用、工作目录路径、"
    "哪些命令前缀会失败）。"
    "**强制项：第 ③ 点必须列出每个被改动的文件名 + 改动函数/区域**（如 'tools.py: read_file 函数'），"
    "不能泛化为'修改了 X.py 的某些函数'。"
    "**强制项：第 ⑥ 点必须逐字保留已验证成功的 shell 命令原文**（如 `py -3.11 -X utf8 -m pytest`），"
    "不可泛化或省略，这是后续轮次直接复用的关键信息。"
    "控制在 900 字内，重要事实不要丢。不要加客套话，直接给结构化要点。"
)


def _summarize_old_history(text: str) -> str:
    """调一次轻量 LLM 把旧对话压缩成摘要。

    review M3 修：stream=False 防止 summarize 内容流式打印到终端污染 UX。
    """
    summarize_msgs = [
        {"role": "system", "content": _SUMMARIZE_SYSTEM},
        {"role": "user", "content": text},
    ]
    resp = call_llm(summarize_msgs, tools=None, stream=False)
    return (resp.choices[0].message.content or "").strip()


def _compact_messages(msgs, keep_recent_pairs: int = 2, plan_anchor: str | None = None):
    """P2 #4-B2：把旧 message 历史压缩成单条 system 摘要，保留最近 N 个 pair 原文。

    切分：[system, user_initial] + [old_pairs...] + [recent_N_pairs]
    保留头部（system + user_initial）+ 最近 N 个 pair 原文，旧 pair 走 LLM summarize。

    若 pair 总数 <= keep_recent_pairs，直接返回原 msgs（不必 compact）。

    review minor m3 修：keep=0 禁止（会丢光最新一轮 LLM 状态导致跑偏）。
    review M1 修：拼接前确保 recent_pairs 起始合法（不是孤立 user 直挂 summary 后）。
    P0-3：plan_anchor 非空时在 summary_msg 前注入锚点 system 消息，确保规划零漂移。
    """
    # review m3：keep=0 是退化路径，禁止使用
    if keep_recent_pairs <= 0:
        raise ValueError("keep_recent_pairs 必须 >= 1，否则会丢光最新一轮 LLM 状态")

    if not msgs or len(msgs) < 4:
        return msgs

    head_count = 0
    if _msg_role(msgs[0]) == "system":
        head_count = 1
    if len(msgs) > head_count and _msg_role(msgs[head_count]) == "user":
        head_count += 1
    if head_count == 0:
        return msgs  # 形态异常，保守不压

    head = list(msgs[:head_count])
    rest = list(msgs[head_count:])

    pairs = _split_messages_into_pairs(rest)
    if len(pairs) <= keep_recent_pairs:
        return msgs

    old_pairs = pairs[:-keep_recent_pairs]
    recent_pairs = pairs[-keep_recent_pairs:]

    # review M1：recent_pairs 第一个 pair 起始角色必须是 assistant 或 user
    # （否则 [summary_system, tool_result, ...] 序列违反 OpenAI tool_result 必紧跟 assistant 约束）
    # 若不合法，把 old_pairs 末尾的 pair 推回 recent_pairs，直到 recent 起始合法
    while recent_pairs and old_pairs:
        first_role = _msg_role(recent_pairs[0][0])
        if first_role in ("assistant", "user"):
            break
        recent_pairs.insert(0, old_pairs.pop())

    old_text = _flatten_pairs_for_summary(old_pairs)
    try:
        summary = _summarize_old_history(old_text)
    except Exception as e:
        # summarize 失败 → 不压（不能丢 history 让 LLM 跑偏）
        console.print(f"[auto-compact] summarize 失败，保留原 messages：{e}", style="yellow")
        return msgs

    if not summary:
        return msgs

    summary_content = f"[历史摘要 - 旧对话已压缩] {summary}"
    # 注入状态文件（若存在）：框架自动维护的环境知识（python/pytest 命令白/黑名单）
    _state_path = Path(_get_workspace()) / ".yansh" / "agent_state.md"
    try:
        _state_content = _state_path.read_text(encoding="utf-8").strip()
        if _state_content:
            _MAX_STATE = 4000  # 硬截断：防止超大状态文件抵消 compact 收益
            if len(_state_content) > _MAX_STATE:
                _state_content = _state_content[:_MAX_STATE] + "\n[...已截断]"
            summary_content += f"\n\n[持久环境知识 .yansh/agent_state.md — 框架自动维护，跨 run 有效]\n{_state_content}"
    except Exception:
        pass
    summary_msg = {"role": "system", "content": summary_content}

    # P0-3：plan_anchor 非空则在 summary 前插入锚点 system 消息，跨 compact 保持规划零漂移。
    # 用"独立文本重注入"而非 pair 位置 pin——首次 compact 后结构变化，第二次需再注入。
    new_msgs = list(head)
    if plan_anchor:
        new_msgs.append({
            "role": "system",
            "content": f"[开场规划锚点 — 不可丢弃，跨文件接口契约以此为准]\n{plan_anchor}",
        })
    new_msgs.append(summary_msg)
    for p in recent_pairs:
        new_msgs.extend(p)
    return new_msgs


def _make_compact_state() -> dict:
    """auto-compact 跨轮状态（threshold/keep/thrashing 计数/disabled）。
    code() 与 fix() 各自的 LLM loop 共用，避免重复内联逻辑。
    P0-3：新增 plan_anchor 字段，solo 首轮捕获开场规划，跨 compact 持久保留。"""
    return {
        "threshold": int(_cfg("compact_threshold_tokens") or 40_000),
        "keep_pairs": int(_cfg("compact_keep_recent_pairs") or 2),
        "consecutive_over": 0,
        "max_consecutive": int(_cfg("compact_max_consecutive_over") or 4),
        "disabled": False,
        "plan_anchor": None,   # P0-3：solo 首个 assistant 规划，compact 时重注入
    }


def _maybe_compact_messages(msgs, state: dict, label: str = "auto-compact"):
    """每轮 call_llm 前检测并压缩 messages。原地更新 state（thrashing 计数/disabled）。
    返回压缩后的 msgs（可能原样返回）。含 thrashing 保护：连续 N 次压缩无效则禁用本任务后续 compact。
    P0-3：透传 state.plan_anchor 给 _compact_messages，确保规划锚点每次重注入。"""
    if state.get("disabled"):
        return msgs
    _est = _estimate_messages_tokens(msgs)
    if _est <= state["threshold"]:
        return msgs
    console.print(f"[{label}] msgs ~{_est} tokens > {state['threshold']}，触发压缩...", style="cyan")
    _new_msgs = _compact_messages(msgs, keep_recent_pairs=state["keep_pairs"],
                                  plan_anchor=state.get("plan_anchor"))
    _new_est = _estimate_messages_tokens(_new_msgs)
    if _new_est < _est:
        msgs = _new_msgs
        console.print(f"[{label}] 已压缩 {_est} → {_new_est} tokens", style="green")
        if (_est - _new_est) / _est < 0.15:
            state["consecutive_over"] += 1
        else:
            state["consecutive_over"] = 0
    else:
        # 估值未降低/反增 —— 计入 thrash 连续计数（不写回较大的 _new_msgs）
        state["consecutive_over"] += 1
        console.print(
            f"[{label}] 未降低（{_est} → {_new_est}），计入 thrash "
            f"({state['consecutive_over']}/{state['max_consecutive']})",
            style="yellow",
        )
    if state["consecutive_over"] >= state["max_consecutive"]:
        console.print(
            f"[{label}] thrashing 保护：连续 {state['max_consecutive']} 次压缩无效，"
            f"禁用后续 compact 继续执行（est={_est}）",
            style="yellow",
        )
        state["disabled"] = True
    return msgs


def _infer_test_scope(plan_files, exclude: set | None = None) -> list[str]:
    """P1.3：根据 plan 列出的修改文件推断本次任务相关的测试文件路径列表。

    规则：
      - 修改的源文件 X.py（非 test_*.py）→ 找 tests/ 全树下同名 test_<basename>.py
      - 修改的文件本身就是 test_*.py / *_test.py → 直接加进 scope
      - 找不到任何对应测试 → 返回 []（调用方应回退到全套）
      - exclude：越权新建的测试文件集合（posix 相对路径），从 scope 中排除

    返回的路径相对 workspace（pytest 原样接受）。
    """
    if not plan_files:
        return []
    ws = Path(_get_workspace())
    tests_root = ws / "tests"

    # 收集 plan 里所有非空 filename
    filenames = []
    for f in plan_files:
        fn = f.get("filename") if isinstance(f, dict) else f
        if fn and isinstance(fn, str):
            filenames.append(fn)
    if not filenames:
        return []

    scope: list[str] = []
    seen: set[str] = set()

    # 预扫 tests/ 下的所有 test_*.py 和 *_test.py 加速查找（按 stem 索引）
    test_files_by_stem: dict[str, list[str]] = {}
    if tests_root.is_dir():
        for p in tests_root.rglob("test_*.py"):
            rel = p.relative_to(ws).as_posix()
            test_files_by_stem.setdefault(p.stem, []).append(rel)
        # P1-7：同时收集 *_test.py，按 p.stem（即 "foo_test"）为键
        for p in tests_root.rglob("*_test.py"):
            rel = p.relative_to(ws).as_posix()
            test_files_by_stem.setdefault(p.stem, []).append(rel)

    for fn in filenames:
        bn = Path(fn).name  # e.g. "tools.py", "test_tools.py"
        stem = Path(fn).stem
        # 1. 已经是测试文件 → 直接加
        if bn.startswith("test_") or bn.endswith("_test.py"):
            rel = Path(fn).as_posix()
            if rel not in seen:
                scope.append(rel)
                seen.add(rel)
            continue
        # 2. 源文件 → 找同名 test_<stem>.py 或 <stem>_test.py（越权新建的测试文件在此处排除）
        for target_stem in (f"test_{stem}", f"{stem}_test"):
            for rel in test_files_by_stem.get(target_stem, []):
                if exclude and rel in exclude:
                    continue  # 越权新建，跳过
                if rel not in seen:
                    scope.append(rel)
                    seen.add(rel)
    return scope


def _force_include_smoke(scope: list, ws) -> list:
    """P0-4：若 ws 存在 tests/test_smoke.py 且 scope 未含，强制并入。
    两路共用（plan 路径 _apply_test_scope_override + solo gate）。
    返回新列表（不原地改传入参数）。"""
    smoke = "tests/test_smoke.py"
    if (Path(ws) / "tests" / "test_smoke.py").is_file() and smoke not in scope:
        scope = list(scope) + [smoke]
    return scope


# 方案a #6：CLI 入口文件惯例名（console_scripts 入口暂不解析 pyproject/setup.py 声明）
_SOLO_ENTRY_BASENAMES = {"__main__.py", "cli.py"}


def _entry_modified(modified) -> bool:
    """方案a #6：本次改动是否触及 CLI 入口文件（__main__.py / cli.py）。
    用于判定 targeted 测试虽绿但可能漏测跨文件 CLI 调用链断裂。"""
    return any(Path(f).name in _SOLO_ENTRY_BASENAMES for f in (modified or []))


def _smoke_exists(ws) -> bool:
    """方案a #6：ws 是否存在 tests/test_smoke.py。"""
    return (Path(ws) / "tests" / "test_smoke.py").is_file()


def _no_tests_collected(tr: dict) -> bool:
    """pytest 退出码 5 或输出含 'no tests ran' / 'collected 0 items' → 没有可收集的测试。
    这不是测试失败（rc=1），而是没有有效测试可复核——应判 no_command 而非 failed。
    文本信号为 pytest 专属措辞，node 路径不会误命中。"""
    if not tr:
        return False
    if tr.get("returncode") == 5:
        return True
    out = (tr.get("stdout") or "") + (tr.get("stderr") or "")
    return ("no tests ran" in out) or ("collected 0 items" in out)


def _has_impl_files(modified) -> bool:
    """实验1 解法B：modified 是否含「实现文件」——非 tests/ 下、非 test_*.py/_test_*.py 的 .py。
    用于判定「改了实现却没留正规测试」。数据文件/草稿脚本不算实现。"""
    for f in modified or []:
        if not f.endswith(".py"):
            continue
        parts = Path(f).parts
        if "tests" in parts:
            continue
        name = Path(f).name
        if name.startswith("test_") or name.endswith("_test.py") or name.startswith("_test") or name == "conftest.py":
            continue
        return True
    return False


def _final_gate_verdict(ws_path, timeout_gate):
    """轮次耗尽兜底：烧光 soft_limit ≠ 失败，做一次最终裁定。
    跑一次测试，按与正常 gate pass 分支**完全相同**的语义判 gate_status，
    但不再回灌、不做确认 drive（没有剩余轮次）。返回 (gate_status, test_result)。
    救回「功能其实满分却因 agent 啰嗦烧光轮次被无条件判 failed」的假阴性（见 R19）。"""
    modified = _task_log_mod.snapshot_files_modified()
    scope = _force_include_smoke(_infer_test_scope([{"filename": f} for f in modified]), ws_path)
    _ptype = (_PROJECT_TYPE or "python").lower()
    if _ptype in ("node.js", "node"):
        from linter import _detect_node_test_cmd as _node_test_cmd
        test_cmd = _node_test_cmd(ws_path)
        coverage = "full" if test_cmd else None
    else:
        targeted = _detect_python_test_cmd(ws_path, scope=scope) if scope else None
        if targeted:
            test_cmd, coverage = targeted, "targeted"
        else:
            test_cmd = _detect_python_test_cmd(ws_path)
            coverage = "full" if test_cmd else None
    if not test_cmd:
        return "no_command", {"returncode": 0, "stdout": "", "stderr": ""}
    tr = test(test_cmd, timeout_sec=timeout_gate)
    if not judge(tr):
        # collected-0 边界：没有可复核的测试 → no_command，非失败
        if _no_tests_collected(tr):
            # 解法B v2：有 CLI 入口却没 smoke → 不算「没测试命令」，是缺关键端到端测试
            if _entry_modified(modified) and not _smoke_exists(ws_path):
                return "no_smoke", tr
            return "no_command", tr
        return "failed", tr
    # 通过：targeted → passed（但改入口且无 smoke → no_smoke，暴露真实缺陷）；全量兜底 → coverage_unknown
    if coverage == "targeted":
        if _entry_modified(modified) and not _smoke_exists(ws_path):
            return "no_smoke", tr
        return "passed", tr
    return "coverage_unknown", tr


def _apply_test_scope_override(plan_result: dict, exclude: set | None = None) -> None:
    """P1.3：原地重写 plan_result['test_command']——基于 plan_result['files'] 推断
    相关测试 scope，命中后用 _detect_python_test_cmd(scope=...) 重新构造命令。

    exclude：越权新建的测试文件集合，传入后从 scope 中排除。

    跳过覆盖的情况：
      - LLM 给的 test_command 不是 pytest 系（如 make test / tox / 自定义脚本）
      - scope 推断为空（找不到任何对应测试 → 保留 LLM 原命令以免误删）
    """
    scope = _infer_test_scope(plan_result.get("files", []), exclude=exclude)
    orig_cmd = (plan_result.get("test_command") or "").strip()
    # R6 保险丝：端到端 smoke test 是发现 CLI 调用链断裂的唯一信号，必须每轮运行。
    # 若 ws 存在 tests/test_smoke.py 但 scope 未含（architect 漏列 / scope 收窄），强制并入。
    scope = _force_include_smoke(scope, _get_workspace())
    if not scope or not orig_cmd or "pytest" not in orig_cmd:
        return
    scoped_cmd = _detect_python_test_cmd(Path(_get_workspace()), scope=scope)
    if "pytest" not in scoped_cmd:
        return  # 防御：detect 落到了 tox / make 路径
    console.print(
        f"[scope] 推断 {len(scope)} 个相关测试，覆盖 test_command: {scoped_cmd}",
        highlight=False,
    )
    plan_result["test_command"] = scoped_cmd


def _dispatch_tool_call(tool_call, *, mode="auto", allow_hil=True, allow_confirm=True, snap=None) -> dict:
    """[P2 #11 wrapper] PreToolUse hook → 内部 dispatch → PostToolUse hook。

    跳过 hook 的场景：
    - 子 agent 内部（_is_in_subagent()）—— 父 agent 派工具时已触发，子 agent 不重复
    - hooks 模块全局禁用（批处理 --json 由 main.py 设置）
    """
    return _dispatch_tool_call_with_hooks(
        tool_call, mode=mode, allow_hil=allow_hil,
        allow_confirm=allow_confirm, snap=snap,
    )


def _dispatch_tool_call_with_hooks(tool_call, *, mode="auto", allow_hil=True,
                                   allow_confirm=True, snap=None) -> dict:
    """实际的 hook 包装实现。和 _dispatch_tool_call 是同一个东西，命名给单测能直接调。"""
    name = tool_call.function.name
    raw_args = tool_call.function.arguments or "{}"
    try:
        args_initial = json.loads(raw_args)
    except json.JSONDecodeError as e:
        return {"name": name, "args": {}, "id": tool_call.id,
                "result": {"error": f"Invalid JSON in arguments: {e}"}}

    # PreToolUse hook（子 agent 内 / 全局禁用时跳过）
    skip_hooks = _is_in_subagent() or _hooks_mod.is_disabled()
    if not skip_hooks:
        try:
            ws = _get_workspace()
            hr = _hooks_mod.run_hook_event(
                "PreToolUse",
                {"event": "PreToolUse", "tool_name": name,
                 "tool_input": args_initial, "cwd": ws},
                match_target=name,
                workspace_dir=ws,
            )
            if hr.get("ran", 0) > 0:
                if hr["decision"] == "block":
                    console.print(f"[hook] PreToolUse 阻止 {name}：{hr.get('reason', '')}",
                                  style="yellow", highlight=False)
                    return {"name": name, "args": args_initial, "id": tool_call.id,
                            "result": {"error": f"Hook 阻止 {name}：{hr.get('reason', '')}"}}
                # modify tool_input
                mod = hr.get("modify", {})
                if isinstance(mod.get("tool_input"), dict):
                    args_initial = mod["tool_input"]
                    console.print(f"[hook] PreToolUse 修改 {name} 输入", highlight=False)
                # hook 错误打印一次（不阻断）
                for err in hr.get("errors", []):
                    console.print(f"[hook] {err}", style="yellow", highlight=False)
        except Exception as e:
            console.print(f"[hook] PreToolUse 异常忽略：{e}", style="yellow", highlight=False)

    # 调内部 dispatch
    out = _dispatch_tool_call_inner(
        tool_call, args_initial, mode=mode, allow_hil=allow_hil,
        allow_confirm=allow_confirm, snap=snap,
    )

    # PostToolUse hook（同样跳过场景）
    if not skip_hooks:
        try:
            ws = _get_workspace()
            hr = _hooks_mod.run_hook_event(
                "PostToolUse",
                {"event": "PostToolUse", "tool_name": out["name"],
                 "tool_input": out["args"], "tool_output": out["result"], "cwd": ws},
                match_target=out["name"],
                workspace_dir=ws,
            )
            if hr.get("ran", 0) > 0:
                mod = hr.get("modify", {})
                if isinstance(mod.get("tool_output"), dict):
                    out["result"] = mod["tool_output"]
                    console.print(f"[hook] PostToolUse 修改 {out['name']} 输出", highlight=False)
                for err in hr.get("errors", []):
                    console.print(f"[hook] {err}", style="yellow", highlight=False)
        except Exception as e:
            console.print(f"[hook] PostToolUse 异常忽略：{e}", style="yellow", highlight=False)

    return out


def _dispatch_tool_call_inner(tool_call, args, *, mode="auto", allow_hil=True,
                              allow_confirm=True, snap=None) -> dict:
    """统一处理 LLM 返回的单个 tool_call，code()/fix() 共用。
    返回 {"name": str, "args": dict, "id": str, "result": dict}
    - mode: "auto" 时对覆盖/移动等弹用户确认；"code" 跳过确认
    - allow_hil: 是否启用 HIL 编辑确认（fix 阶段也可启用）
    - allow_confirm: 是否在 mode=auto 时弹覆盖/移动确认
    - snap: 当前快照，用于增量备份
    - args: 已解析的参数（PreToolUse hook 可能改过；wrapper 传入）"""
    name = tool_call.function.name

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
                # P2 #4-A2 review M2: HIL 取整文件 old_content 用于 diff 显示，绕过默认 limit
                _r = read_file(fname, limit=10**9, max_bytes=10**9)
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
            _task_log_mod.record_file_modified(fname)
        return {"name": name, "args": args, "id": tool_call.id, "result": result}

    if name == "replace_in_file":
        rfname = args.get("filename", "")
        old_str = args.get("old_str", "")
        new_str = args.get("new_str", "")
        if hil_on:
            # P2 #4-A2 review M2: HIL diff 需要整文件 old_content，绕过默认 limit
            _r = read_file(rfname, limit=10**9, max_bytes=10**9)
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
            _task_log_mod.record_file_modified(rfname)
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
        if result.get("success"):
            src_f = args.get("src", "")
            dst_f = args.get("dst", "")
            if src_f:
                _task_log_mod.record_file_modified(src_f)
            if dst_f:
                _task_log_mod.record_file_modified(dst_f)
        return {"name": name, "args": args, "id": tool_call.id, "result": result}

    if name == "apply_patch":
        _backup_file_if_needed(snap, args.get("file_path", ""))
        result = apply_patch(**args)
        if "success" in result:
            _task_log_mod.record_file_modified(args.get("file_path", ""))
        return {"name": name, "args": args, "id": tool_call.id, "result": result}

    if name == "replace_symbol":
        _backup_file_if_needed(snap, args.get("file_path", ""))
        result = replace_symbol(**args)
        if "success" in result:
            _task_log_mod.record_file_modified(args.get("file_path", ""))
        return {"name": name, "args": args, "id": tool_call.id, "result": result}

    if name == "append_to_file":
        _backup_file_if_needed(snap, args.get("filename", ""))
        result = append_to_file(**args)
        if "success" in result:
            _task_log_mod.record_file_modified(args.get("filename", ""))
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
            _task_log_mod.record_file_modified(del_fname)
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
        # P2 #9 子 Agent：handler 内嵌套跑独立 LLM loop，递归被 _IN_SUBAGENT 锁住
        "dispatch_subagent": _subagent_handler,
        # P2 #12 跨 Session 记忆：写读 .yansh/memory/ 不动 workspace 代码
        "save_memory": save_memory,
        "recall_memory": recall_memory,
    }
    if name == "list_files":
        return {"name": name, "args": args, "id": tool_call.id, "result": list_files()}
    if name in readonly_handlers:
        # P2.1 read_file 命中检测：同任务内相同 (filename, offset, limit) 重读时
        # 不调真工具、不返回 content，让 messages 只 append 短 error+hint。
        # LLM 看到 hint 应当从历史 messages 里复用 content，避免冗余 token。
        if name == "read_file" and _read_cache_hit_or_record(args):
            console.print(
                f"[read_cache] 命中 {args.get('filename')}"
                f"@offset={args.get('offset')}/limit={args.get('limit')}/max_bytes={args.get('max_bytes')}，跳过实读",
                style="cyan", highlight=False,
            )
            return {
                "name": name, "args": args, "id": tool_call.id,
                "result": {
                    "error": "duplicate_read",
                    "filename": args.get("filename"),
                    "offset": args.get("offset"),
                    "limit": args.get("limit"),
                    "hint": ("This (filename, offset, limit, max_bytes) was already read earlier in the same task; "
                             "the content is in your message history. Reuse it instead of reading again. "
                             "If you genuinely need a fresh read, call with a different range or note it in your reply."),
                },
            }
        # P0 安全：audit/plan 上下文下，dispatch_subagent 强制降级为只读 role——
        # 否则 LLM 调 dispatch_subagent(role="general") 派出的子 agent 会用 dispatch_mode="auto"
        # 拿到全工具集（含 write_file / execute_command），绕过 audit 的"只读承诺"。
        if name == "dispatch_subagent" and mode == "audit":
            req_role = args.get("role", "explorer")
            if req_role not in ("explorer", "auditor"):
                console.print(
                    f"[security] audit 上下文：子 agent role '{req_role}' 强制降级为 'auditor'",
                    style="yellow", highlight=False,
                )
                args["role"] = "auditor"
        # P3 #6.1: dispatch_subagent 入口前测当前线程 read_cache 累计；出口算 delta
        # 塞到 out **顶层**（不进 result，LLM 看不到，节省 ~25 token/次）。
        # 并发分支由 _dispatch_tool_calls 读 out["_read_cache_delta"] 合并到主线程。
        is_subagent_call = (name == "dispatch_subagent")
        if is_subagent_call:
            _entry_total, _entry_hits, _ = _read_cache_stats()
        try:
            result = readonly_handlers[name](**args)
        except Exception as e:
            result = _tools_mod._err("internal", f"工具调用异常: {e}", name)
        out = {"name": name, "args": args, "id": tool_call.id, "result": result}
        if is_subagent_call:
            _exit_total, _exit_hits, _ = _read_cache_stats()
            out["_read_cache_delta"] = (_exit_total - _entry_total, _exit_hits - _entry_hits)
        return out

    # P2 #10 MCP 路由：mcp__<server>__<tool> 转发到对应 server
    if name.startswith("mcp__"):
        try:
            result = _mcp_mod.call_tool(name, args)
        except Exception as e:
            result = _tools_mod._err("internal", f"MCP 工具调用异常: {e}", name)
        return {"name": name, "args": args, "id": tool_call.id, "result": result}

    return {"name": name, "args": args, "id": tool_call.id,
            "result": _tools_mod._err("invalid_args", f"未预期的工具: {name}", name)}


def _record_dispatch(out: dict, msgs: list):
    """把分发结果挂回 messages，并写入 task tool_calls 日志（敏感字段除外）"""
    args = out["args"]
    safe_args = {k: v for k, v in args.items() if k not in ("content", "new_str", "new_code")}
    _task_log_mod.record_tool_call(out["name"], safe_args)
    msgs.append({
        "tool_call_id": out["id"],
        "role": "tool",
        "name": out["name"],
        "content": json.dumps(out["result"]),
    })


def _dispatch_tool_calls(tool_calls, *, mode, allow_hil, allow_confirm, snap, messages,
                         console_label: str = "") -> list:
    """[P2 #9b] 批跑一次 LLM 返回的多个 tool_calls。

    精准并发策略：**只对 dispatch_subagent 用 ThreadPoolExecutor 并发**——
    本地工具（read/grep/list_files）几毫秒，并发开销得不偿失；
    写工具必须串行（HIL/confirm 顺序依赖、console 输出可读）；
    子 agent 是唯一长耗时（多轮 LLM call），并发收益最大。

    返回 outs 列表（按原 tool_calls 顺序），并已按顺序拼回 messages——
    OpenAI tool_calls 协议要求 tool result 顺序与 tool_calls 顺序一致。
    """
    n = len(tool_calls)
    if n == 0:
        return []
    outs: list = [None] * n

    # 找出 dispatch_subagent 的 index 集合
    sub_indices = [i for i, tc in enumerate(tool_calls)
                   if tc.function.name == "dispatch_subagent"]

    # ≥2 个子 agent：并发跑（用 thread pool）
    if len(sub_indices) >= 2:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        prefix = f"{console_label} " if console_label else ""
        console.print(
            f"{prefix}[subagent 并发] {len(sub_indices)} 个子 agent 同时启动",
            highlight=False,
        )
        max_workers = min(len(sub_indices), _SUBAGENT_CONCURRENCY_CAP)
        with ThreadPoolExecutor(max_workers=max_workers,
                                thread_name_prefix="yansh-subagent") as ex:
            futs = {
                ex.submit(_dispatch_tool_call, tool_calls[i],
                          mode=mode, allow_hil=allow_hil,
                          allow_confirm=allow_confirm, snap=snap): i
                for i in sub_indices
            }
            for fut in as_completed(futs):
                idx = futs[fut]
                try:
                    outs[idx] = fut.result()
                except Exception as e:
                    outs[idx] = {
                        "name": tool_calls[idx].function.name,
                        "args": {}, "id": tool_calls[idx].id,
                        "result": _tools_mod._err("internal", f"并发 dispatch 异常: {e}", tool_calls[idx].function.name),
                    }
        # P3 #6.1: 并发 worker 跑完后合并子线程的 read_cache delta 到主线程
        # （worker 的 threading.local cache 独立，不合并会让 _print_read_cache_summary 漏算）
        # delta 在 out **顶层**而非 out["result"]——避免序列化给 LLM。
        # 串行分支不在这里——单个 subagent 跑在调用线程，delta 已直接计入。
        for idx in sub_indices:
            out = outs[idx]
            if not isinstance(out, dict):
                continue
            d = out.get("_read_cache_delta")
            if d and isinstance(d, (list, tuple)) and len(d) == 2:
                _read_cache_merge(int(d[0]), int(d[1]))

    # 串行处理剩余（含单个 subagent 的情况、所有非 subagent 工具）
    for i, tc in enumerate(tool_calls):
        if outs[i] is None:
            outs[i] = _dispatch_tool_call(tc, mode=mode, allow_hil=allow_hil,
                                          allow_confirm=allow_confirm, snap=snap)

    # 按原顺序拼回 messages（OpenAI 协议要求 tool_call_id 与顺序一致）
    for out in outs:
        _record_dispatch(out, messages)

    return outs

# ---------- #26 Linter / #27 项目类型检测：已迁移至 linter.py ----------

def run_linter(files=None):
    return _linter_mod.run_linter_for(_PROJECT_TYPE, files=files)

_ARCHITECT_ROLE = """[Role: Architect Agent]
You focus on analyzing requirements and producing implementation plans.
Responsibility: output the plan only — no code; weigh risks and file dependency order; deliver a complete and executable plan.
Principles:
- When the requirement is ambiguous or has multiple reasonable readings, **state your interpretation and tradeoffs in one sentence first**, then output the plan — do not guess
- The plan must call out "which files change, which don't" — LLMs tend to over-expand the change scope
- **End-to-end awareness (critical)**: when the task involves changing a function signature (adding/removing parameters, changing return shape),
  renaming an identifier, or altering module exports, **the plan must include a step "use search_in_files or
  list_symbols to enumerate all call sites"**, and fold every potentially affected file into the change list.
  Typical trap: the user only mentioned "change tools.py + tools_schema.py", but call sites (e.g. agent.py
  dispatch) need updating too — otherwise the new parameter is silently swallowed. **Files the user didn't
  list aren't necessarily out of scope** — your job is to surface these hidden dependencies.

- **Multi-file new projects (≥3 new files, critical)**: before listing files, FIRST establish a cross-file symbol contract. Use the **member-level dict format** for the `symbol_contract` field. Two layers MUST both be covered:
  1. **Export names** (class/function/variable names imported by other modules) — prevents ImportError. **函数与类都必须列**：真实失败——`analyzer.py` 仅定义 `class Analyzer`，调用方写 `from analyzer import analyze`（函数），ImportError。凡被跨文件 import 的顶层函数，按确切函数名列入 exports；import 名须与实现端定义种类（函数/类）完全一致，不可 import 一个不存在的函数名。
  2. **Class-internal member names** (enum members like `TokenType.IDENTIFIER`, dataclass fields like `node.cond`, `node.then_block`) — prevents AttributeError at runtime. Import works fine but execution crashes silently if these are misnamed. For every Enum class and every dataclass whose fields are accessed by another file, enumerate ALL accessed member/field names in the contract.
  3. **Cross-file method names** — for any class whose methods are *called from another file* (e.g. `executor.py` calls `evaluator.evaluate()`), enumerate the exact public method name(s) under `methods`. Use `{"name": "method_name", "params": ["arg1", "arg2"]}` dict format (params excludes self) to also fix parameter-count mismatches. Real failures: (a) `expression.py` defined `ExprEvaluator.evaluate()` but `executor.py` called `.eval()` — AttributeError; (b) `catalog.py` defined `register_csv(self, path)` (1 param) but `__main__.py` called `register_csv(table_name, csv_path)` (2 params) — TypeError. Both failures are prevented by listing the exact method signature in the contract.
  Real failure examples: (a) `token.py` defined `TokenType.IDENTIFIER` but `parser.py` wrote `TokenType.IDENT` — import succeeded, runtime crashed. (b) `analyzer.py` defined `class Analyzer` but `main.py` wrote `from analyzer import analyze` — ImportError. These are the #1 and #2 failure modes for multi-file generated packages.

- **End-to-end smoke test (MANDATORY when the requirement specifies a real entry point + expected output)**: if the requirement gives BOTH (1) an explicit end-to-end run command (e.g. `python -m <pkg> --flag ...`, `python <entry>.py ...`, a CLI invocation) AND (2) expected output or error-format examples (a table, `(N rows)`, `Error[CATEGORY]:`, exit codes), the plan **MUST** include a `tests/test_smoke.py` file that drives the REAL entry point via `subprocess.run([sys.executable, "-m", "<pkg>", ...], capture_output=True, text=True)` and asserts on stdout/exit code — and `test_command` MUST include it.
  - **Why this is critical**: internal unit tests and the modules they test share the SAME (possibly wrong) signature assumptions, so they pass together while the real entry point (`__main__.py`) can independently drift to a wrong signature/call — unit tests stay green but `python -m pkg` crashes on every invocation. The smoke test is the ONLY signal that catches a broken CLI call chain. Real failure: every unit test green (200 passed) but `python -m miniql` crashed 9/10 because `__main__.py` called `build_logical_plan(resolved)` while the function needed `(resolved, catalog)` — no test ever executed the CLI path.
  - The smoke test MUST go through `python -m` (subprocess), NEVER by importing internal functions — importing internals reproduces the same blind spot.

Always respond in Chinese (用户的项目规则要求中文回复).
"""

_CODER_ROLE = """[Role: Coder Agent]
You focus on producing high-quality code according to the plan.
Responsibility: execute the plan strictly, do not invent extra features; emphasize code quality and boundary cases; for existing files use replace_in_file for precise edits — unless the file has 20+ scattered edit points, in which case write_file the whole file in one shot is preferred.
Tool-call efficiency (critical):
- **Locate before modifying**: use search_in_files / list_symbols / get_symbol_definition to pinpoint the change site — don't read_file the whole file then decide
- **Parallelize independent tool calls**: in one turn, fire multiple read_file / search_in_files / list_symbols at once — don't serialize
- **Combine shell queries**: when checking several env vars or running several independent commands, chain them with `;` in a single execute_command
- **Don't re-read after editing**: write_file / replace_in_file / replace_symbol return an error directly on failure — no need to read_file to confirm
- **Don't dispatch_subagent for small tasks**: a subagent costs 1k+ tokens of cascade overhead before doing anything. Use it only for genuinely large explorations (mapping a module's call sites across many files) or parallelizable independent branches (analyze A/B/C modules at once). For "read 3 functions in 1 file" or "read 1 file + 1 test", call read_file / get_symbol_definition / search_in_files directly — single tool call, no overhead.
  - ❌ Anti-pattern: dispatch_subagent("read find_memory + save_memory + _slugify in memory.py, plus test_X in test_memory.py") — that's 4 read_file calls or 1 list_symbols + 2 read_file, NOT a subagent task.
  - ✓ Correct usage: dispatch_subagent("map all callers of `tools.read_file` across the repo and classify by call pattern") — genuine cross-file exploration.
- **Batch dense edits aggressively** (critical for refactor tasks): the coder loop has a per-file round budget. If you edit one site at a time, you'll hit the limit before finishing.
  - **Same pattern repeating**: when N sites match exactly the same `old_str → new_str` (e.g. adding a new required argument to every call of `_err`, renaming an import across a file), use **`replace_in_file(filename, old_str, new_str, replace_all=True)`** — one call replaces all N sites. Only fall back to per-site edits when each site has different surrounding context.
  - **Multiple edits per turn, parallel**: when a file needs multiple distinct edits (different `old_str` values), fire **all `replace_in_file` tool calls in the same turn** — the harness dispatches them in parallel. Don't serialize one-edit-per-turn.
  - **Whole-file rewrite for >20 sites with varied context**: if `replace_all` doesn't apply (each site differs) and there are 20+ sites, `write_file` the whole new content in one shot beats 20 round-trips of `replace_in_file`.
  - ❌ Anti-pattern: 20 turns of `replace_in_file(file, "_err(\"a\", \"x\")", "_err(\"a\", \"x\", tool=\"foo\")")` for 20 different call sites — burns the round budget; either parallelize them in one turn or batch-edit by `replace_all`/`write_file`.
Task pattern recognition (identify which category before acting, follow the matching rule):

1. **Changing an existing function signature / return shape**
   - First search_in_files for the function name (e.g. `\\blist_files\\b`), enumerate callers
   - Update all callers that need to change in the same turn — don't split into batches
   - Typical hidden deps: dispatch tables (the `if name == "X"` branches in agent.py),
     import statements (`from X import Y` lines), docs/README examples
   - The user's file list may be incomplete — fill in the gaps after grep

2. **Adding a new tool / command / handler (≠ implementation only)**
   - All three pieces are required: **implementation** (function in tools.py) + **schema** (entry in tools_schema.py
     TOOLS list) + **dispatch** (`if name == "X"` branch in agent.py +
     `from tools import X`)
   - After writing the implementation, proactively verify the other two pieces are in place — don't wait for unit tests to fail
   - Analogy: adding a dish requires updating the menu and the kitchen workflow, not just cooking it

3. **Recursive / pruning control flow (max_depth, depth-limit class)**
   - Prefer the simple "compute depth then continue" filter, e.g.:
     ```
     for root, dirs, fnames in os.walk(base):
         rel = os.path.relpath(root, base)
         depth = 0 if rel == "." else len(rel.split(os.sep))
         if depth >= max_depth: continue
         for f in fnames: ...
     ```
   - Don't use clever pruning like `dirs.clear() if depth >= max_depth`:
     when root depth=0 and max_depth=1 it never triggers, sub-directory files are already in the list — off-by-one
   - max_depth=1 semantics: include root's direct children, not grandchildren; mentally walk through depth=0/1/2 before committing

4. **Scope discipline (critical)**
   - The diff should cover only the functionality described in the task; "while we're at it" refactors are forbidden:
     - Don't swap path separators (`rel.replace("\\\\", "/")` — has broken test_list_files before)
     - Don't rename existing variables, don't add type annotations, don't "beautify" formatting
   - If you want to change something outside scope, stop and report — don't act on your own
   - **Failing tests aren't necessarily yours**: when tests go red, first check whether the symbols referenced in the failing
     assert are in the files this plan listed — unrelated ones (e.g. you changed list_files but test_execute_command_timeout
     failed) are most likely pre-existing failures. **Record them in your report but don't touch production code to "fix" them**
   - **Same applies to linter errors (ruff/flake8/pyright/mypy)**: unused import (F401), unused variable,
     formatting issues — if they're **outside the scope of this plan** (e.g. you changed tools.py but ruff complains about agent.py
     F401), treat them as pre-existing — record but don't tidy up; only fix linter errors inside this plan's files
5. **Benchmark / profile-then-optimize tasks**
   - "先 benchmark 再优化 X" 中，benchmark 是一次性度量手段，**不消耗目标文件 X 的编辑预算**。
   - 流程：一轮内完成度量（跑 timeit / 写临时脚本），立即把剩余所有轮次用于 `replace_in_file` 改 X。
   - 多目标文件时，每个目标文件独立使用自己的轮次；benchmark 只做一次，不重复跑。
   - benchmark 临时脚本用完即弃，任务结束前删除，不留在 workspace（影响 files_modified 统计）。
   - `python -c` 被安全策略拦截属预期，**不要绕道反复写临时脚本**——确认是瓶颈后直接优化。
   - ❌ 反模式：为 search.py 这个目标文件花光 5 轮在写/跑 benchmark 脚本上，一次 replace_in_file 都不发。
   - 若目标文件一次未改，必须 `task_complete(success=False, summary="未完成 X 的优化")`，不能静默退出。

6. **Writing tests with numeric / range assertions on a function you just implemented or modified**
   - Do NOT guess what the output will be. The function's exact behavior (floating-point formulas, regex, rounding) is hard to predict mentally.
   - Before hardcoding any numeric bound in an assertion (e.g. `assert 50.0 <= score < 70.0`), use execute_command to run the function on the candidate input and capture the real output:
     ```
     execute_command("python -c \"from module import fn; print(fn(input))\"")
     ```
   - Only write the assertion after you have confirmed the actual value falls in the intended range.
   - If the value doesn't land where intended, pick a different input — never adjust the implementation just to make a test pass.

7. **"Only add tests" tasks — add to existing test file, not a new standalone file**
   - When the task says "只加测试" / "仅加测试" / "不修改 X.py", the tests belong in the **existing** test file (`tests/unit/test_X.py`), importing from the actual package.
   - ❌ Anti-pattern: create a new file at workspace root (e.g. `retry.py`, `cache.py`) that copies the function and tests it — this tests your copy, not the installed package.
   - ✅ Correct: append new `def test_...` functions to `tests/unit/test_X.py`, with `from packagename import func` at the top.
   - If the plan says target file is the source module (e.g. `retrykit/retry.py`) but the task asks to "only add tests", the real target is `tests/unit/test_retry.py`. Adjust the plan accordingly.

8. **Bug-fix tasks — fix ONLY the named defect, never touch adjacent code**
   - Bug 修复的 diff 必须**只覆盖造成该缺陷的那几行**，连带、"顺手"、"既然在这儿了"的改动一律禁止。
   - 真实翻车案例（务必规避）：
     - ❌ 修 `frontmatter.py` 的 `meta[key]=_strip_quotes(val)` 时，顺手给 `tools.py` 的 `_err` 加了强制 `tool` 参数 → 5 个 test_tools.py 回归失败。
     - ❌ 修 `search_in_files` 的 `re.error` 崩溃时，顺手重构了路径安全逻辑 → 无关测试回归失败。
   - 判断标准：**"不改这行，原 bug 就修不好"？** 是 → 改；否（哪怕看起来有问题）→ **不准碰**。
   - 函数签名（增删参数）、错误处理/安全校验逻辑、被多处调用的辅助函数（如 `_err`、路径校验函数）属于**高危外溢区**：改这类代码前先确认"任务有没有点名要改它"，没点名就停手。
   - 在 task_complete 的 summary 里记录"发现了 X 问题但未改，建议人工处理"。

9. **只读 / 探索 / 评估类任务 — 禁止任何写操作**
   - 当任务出现"不要修改任何代码"/"只读"/"探索"/"评估可行性"/"给出分析"/"不写代码、不改 repo、不跑测试"这类措辞时，
     **绝对不允许** 调用 write_file / replace_in_file / replace_symbol / execute_command 改动任何文件（含源码、配置、临时脚本）。
   - 只准用 read_file / search_in_files / list_symbols / get_symbol_definition 收集信息，然后把结论写进 task_complete 的 summary。
   - ❌ 真实翻车案例：任务说"在 agent.py 里找到 X 函数，告诉我它的行为，不要修改任何代码"，结果却 replace_in_file 改了 agent.py → 违反只读约束，任务判负。
   - 唯一例外：任务**明确要求**产出一个文档文件（如"输出 markdown 文档到 notes/xxx.md"）——此时只写那一个指定的产物文件，绝不碰任何源码或测试。
   - **任务没有点名要产出文件时，写任何文件（含 .md 笔记、分析文档、临时脚本）都算违规。**
   - 判断标准：**"任务有没有让我改/产出文件"？** 没有 → 一律只读，结论进 summary；有且仅指定某产物文件 → 只写那个文件。

10. **禁止用 write_file 整体重写已有大文件（critical，会造成灾难性回归）**
   - **永远不要** 对已有的大文件（>100 行）使用 write_file 整体替换——它会静默丢失你没有看到的代码（re-export、辅助函数、常量）。
   - ❌ 真实翻车：用 write_file 重写 agent.py 时，丢失了 `from subagent import _is_in_subagent, _SUBAGENT_CONCURRENCY_CAP` 等 re-export 语句 → 58 个测试全部 NameError 崩溃。文件里的注释"这里保留 re-export"还在，对应的 import 却被删了。
   - ✅ 正确做法：**只用 replace_in_file** 做精确的局部替换（要加的函数/参数/导入，找准锚点替换）。
   - 新增功能到已有文件时，用 `replace_in_file` 在正确位置插入，不要整文件重写。
   - 如果文件不存在，才可以用 write_file 创建新文件。

11. **新增 API 前必须用精确名称验证是否已存在**
   - 当 search_in_files 命中了"相近"内容，**不能**认为目标 API 已存在——必须搜索**精确**的 API 名称。
   - 判断标准：命中的符号名与 requirement 要求新增的名称**完全一致**（含大小写、下划线前缀）？
     - 完全一致 → 继续 read_file 确认函数体是否已实现。
     - 仅相似 → **目标 API 不存在**，必须继续新增实现。
   - ❌ 错误：requirement 要加 `LOG_DIR`，search 命中 `_LOG_DIR`，就认为"已完成"跳过。
   - ❌ 错误：requirement 要加 `get_recent_logs()`，search 命中 `show_recent_logs()`，就认为"已完成"跳过。
   - ✅ 正确：明确搜索 `"def get_recent_logs"` 或 `"^LOG_DIR "` 验证精确名称是否存在，不存在则实现它。

12. **修改 requirement 声称"已有"的目标前，必须验证它真实存在**
   - 当 requirement 说"X 里有一个 foo() 函数，给它加 Y"或"修改现有的 foo"时，**先用精确名称搜索 foo 是否真的存在**（search_in_files / get_symbol_definition）。
   - 若 get_symbol_definition / search 返回"未找到" → 说明 **requirement 的前提是错的**（目标根本不存在）。此时：
     - **必须** 调用 task_complete(success=False, summary="目标 `foo` 在仓库中不存在，需求前提有误，无法对其修改") 如实报告。
     - **绝对禁止**用 write_file / append_to_file 凭空造一个新的 foo 来冒充"修改"——这是幻觉，等于偷换任务。
   - ❌ 真实翻车：requirement 说"tools.py 有个 batch_read_files()，给它加并发"，实际该函数不存在，coder 直接 append 新建了一个 → 把"修改已有"偷换成"凭空新增"。
   - ✅ 正确：验证 batch_read_files 不存在 → task_complete(success=False, "该函数不存在") ，不硬造。

13. **rename / 删除 / 替换类任务：task_complete(success=True) 前必须验证目标串已消失**
   - 当 requirement 要求"把 A 改名为 B"/"删除 X"/"移除 Y"时，在 task_complete(success=True) 之前，**必须用 search_in_files 精确搜索旧名 A / 目标串 X**，确认在所有应改文件中已 0 命中。
   - replace_in_file 返回 error（如"未找到要替换的字符串"）时：**绝不允许**当成功，**绝不允许**据此 task_complete(success=True) 或谎称"已在文档中完成 / 无需修改"。
   - replace_in_file 报"未找到"时，**不要**用同一个 old_str 反复重试——先 read_file 读取目标行附近的**精确原文**（连空格、引号、转义都按文件里的真实字节），再用读到的原文作为 old_str 重试。
   - ❌ 真实翻车：rename directory_summary→summarize_directory，agent.py / subagent.py 的 prompt 文本里残留 `directory_summary(path=...)`，replace 因多写转义一直失败，coder 却 task_complete(success=True) 谎称"只在文档、无需改"。
   - ✅ 正确：search_in_files("directory_summary") 仍有命中 → 说明没改完 → 继续修，直到 0 命中才能 success=True。

14. **否定约束识别——"不要做 X"/"不得修改 Y"/"不要新建 Z"：执行前提取，全程遵守**
   - 动手前先扫一遍 requirement，把所有否定约束提取成清单（如"不要新建测试文件"/"不得修改 config.py"/"只改 X 不碰 Y"）。
   - 否定约束**优先级高于一切默认习惯**：你默认会顺手补测试，但若 prompt 写"不要新建测试文件"，则**绝不** write_file 任何 `test_*.py` / `*_test.py`。
   - ❌ 真实翻车：prompt 写"在 calc.py 新增 sub(a,b)，不要新建测试文件"，coder 仍建了 `tests/test_calc.py` → 违反否定约束，且自建测试失败会拖累整体 success 判定。
   - task_complete 的 summary 里复述你遵守了哪些否定约束。

Test file rule: a test file (test_*.py / *_test.py) located in a subdirectory (e.g. tests/) must include these two lines at the very top to import parent modules:
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

Always respond in Chinese (用户的项目规则要求中文回复); 工具调用 task_complete 的 summary 字段也用中文.
"""

_REVIEWER_ROLE = """[Role: Code Review Agent]
You focus on reviewing already-generated code.
Responsibility: check whether the code meets the original requirement, whether boundary holes exist, and whether project rules are followed.
Input: the files changed in this round and the original requirement.
Principles:
- **Distinguish bug from style preference**: the former must be reported (functional errors, boundary crashes, security issues); the latter restrained (naming, comment density, subjective readability)
- **Sort issues by severity**: critical (directly broken functionality) → major (boundary/reliability) → minor (code smell); don't flatten
- **Before flagging, rule out "this was intentional"**: when unsure, list under suggestions, don't outright reject
- **Don't speculate about code you haven't read**: cite evidence with file:line
Output: must return strict JSON with keys "approved" (bool), "issues" (array of strings), "suggestions" (array of strings).

Always respond in Chinese (用户的项目规则要求中文回复); JSON 内字符串值也用中文表达, 但 keys 保持英文.
"""

_TESTER_ROLE = """[Role: Tester Agent]
You focus on analyzing test failures and guiding fixes.

[Termination requirement - must read]
**At the end of every task you must call `task_complete(success, summary)` to signal explicitly** — this is a protocol requirement, not a suggestion.
- Whether the fix is done, pre-existing failures are inferred to be skippable, or you confirm you cannot continue, express it via task_complete:
  - `task_complete(success=True, summary="修复了 X，跳过 Y/Z 两条 pre-existing 失败")`
  - `task_complete(success=False, summary="错误归属本次任务但缺少 Z 上下文，建议人工介入")`
- **Do not exit silently** (no tool call this turn, just stop) — the loop will re-ask "are you done", wasting a round.

[Few-shot examples]
Example 1 (fixed task-relevant + skipped pre-existing):
  → replace_in_file(...)
  → task_complete(success=True, summary="修复 list_files max_depth=1 边界 bug；test_execute_command_timeout 等 5 条断言不属于本次范围，已跳过")

Example 2 (cannot fix):
  → read_file(...)
  → task_complete(success=False, summary="测试期望 result['error'] 含'超时'但工具返回 security——这是 pre-existing 失败，本次任务不应改测试期望也不该改工具行为")

Example 3 — ANTI-PATTERN (DO NOT DO THIS):
  ❌ 测试 `assert "超出" in result["error"]` 失败 → replace_in_file 改成 `assert "越界" in ... or "超出" in ... or "workspace" in ...`
  ❌ 测试 `assert "超时" in result["error"]` 失败 → replace_in_file 改成 `assert "超时" in ... or "安全" in ... or "拦截" in ...`
  ❌ 测试 `assert not any("截断" in l for l in lines)` 失败 → 删掉 assert 或加 `or True`
  这是把 bug 藏起来，不是修复。正确做法：按 Investigation order 第 1 条做归属判断 — 失败符号不在本次 plan 文件范围 → `task_complete(success=true, summary="...不属本次范围, 已跳过")` 跳过。

Responsibility: focus only on test results and error info; give precise, minimal fix suggestions; avoid introducing unrelated changes.
Investigation order:
1. **First identify attribution**: are the symbols referenced in the failing assert in the files this plan listed?
   - Yes → failure introduced by this task, continue the workflow
   - No (e.g. this task changed list_files but test_execute_command_timeout failed) → most likely a pre-existing failure, **skip without fixing**, list it in the task_complete summary for user judgement
2. **Read the test code first**: find the failing assert and understand what it expected
3. **Then read the code under test**: locate where actual behavior deviates from expectation
4. **Don't change production code first**: confirm whether it's a product bug or a test bug — the test expectation itself might be wrong
5. When reporting, cite file:line; don't speculate "it must be X that's wrong"
Error-info usage rules:
- The `error_kind` field is only an error **classification tag** (so you can decide retry vs give up),
  **not a reason to change a test expectation** — when a pre-existing test expected "超时" but the tool returned security,
  apply the attribution rule (item 1) and skip it; **do not edit the test assert to match error_kind**.

Oscillation guard:
- 若本轮失败数比上一轮**上升**，说明你的修改引入了 regression。**先复查你上一轮改的文件**，不要直接在当前报错方向继续改。
- 同一处代码若连续两轮被你反向改动（A→B→A），这是震荡——立即 task_complete(success=false, summary="X 与 Y 存在约束冲突，单方向修复会引入对方回归，需人工裁决")。
- **契约是唯一真值源**：只改引用端，不改定义端（枚举定义、dataclass 字段定义）去迁就引用端。

Always respond in Chinese (用户的项目规则要求中文回复); 工具调用 task_complete 的 summary 字段必须中文.
"""

_AUDITOR_ROLE = """[Role: Auditor Agent]
You focus on auditing existing code and produce a readable Markdown report — never modify any file.

[Termination requirement - must read]
**At the end of every audit you must call `task_complete(success, summary)` to signal explicitly** — this is a protocol requirement, not a suggestion.
- The report goes into the assistant message body; task_complete is the final tool call:
  - `task_complete(success=True, summary="审计完成：发现 3 处 critical / 5 处 minor")`
  - `task_complete(success=False, summary="workspace 为空 / 目标符号不存在 / 无法继续")`
- **Do not exit silently** — the loop will re-ask "are you done", wasting a round.

Workflow: first inspect the workspace_symbols top-level summary pre-injected in system to lock in the target directory/file; then use read_file / get_symbol_definition / search_in_files / find_references to drill in as needed; finally output report + task_complete.
**Hierarchical index usage**: what's injected is the top-level structure — sub-directories only have counts. To drill into a directory, use `workspace_symbols(path="<dir>")` to see top-level symbols there, or `directory_summary(path="<dir>")` for the file list / extension distribution. **Don't fetch the full tree at once** (recursive=true blows up context in large projects).
Tool-call efficiency (critical):
- **Locate before reading carefully**: use search_in_files / list_symbols / get_symbol_definition to pinpoint a specific line or symbol — don't read_file the whole file then filter
- **Parallelize independent tool calls**: in one turn fire multiple read/search/list_symbols at once
- **Whole-file read is the last resort**: only when you genuinely need full context (< 200 lines) read the whole file; for large files use offset+limit ranges
Task-scale awareness (key):
- **Simple question, simple answer**: when the user asks "how many X", "where is X" (counting/locating), reply directly with a number + list — **don't apply the audit-report template** (no overview/summary/severity tiers needed)
- **The audit-report template is only for**: open-ended tasks like "audit this / find potential issues / assess code quality"
- Match output length to input complexity — don't pile up 5 sections when one sentence suffices
Report structure (open-ended audit only):
## 总览 (project type, scale, focus area)
## 重要发现
- Classified as critical / major / minor
- Each item annotated with `file:line` + current state + suggestion
## 总评 (overall health, top 1-3 things worth prioritizing)
Discipline principles:
- Distinguish bug from style preference; before flagging, rule out "this was an intentional design choice"
- Don't speculate about code you haven't read; cite evidence with file:line
- Keep proportion for small issues — don't stack to fill quota

Always respond in Chinese (用户的项目规则要求中文回复); 报告正文与 task_complete summary 必须中文, 仅 file:line 与符号名保持英文.
"""

_SOLO_ROLE = """[Role: Solo Agent — 单一连续 context 端到端工程师]
You hold ONE continuous conversation context from start to finish. Unlike a per-file pipeline, you can see every file you have already written — so cross-file consistency is your own responsibility and is naturally achievable: read your own real code instead of guessing names.

You autonomously: plan → create/edit files → run real entry points → read tracebacks → fix → write & run tests → repeat, all in this same context. No separate architect hands you a symbol contract; YOU are the architect, coder, and tester.

[开场规划 — 动手前必做，固化在本轮]
Before writing any code, in your FIRST assistant turn lay out (in the message body, concisely):
- 文件清单：每个要创建的文件 + 一句话职责
- 模块边界与依赖方向（谁 import 谁，避免环）
- 关键跨文件接口签名：函数名/类名/方法签名/数据类字段名 —— 这就是你自产的 symbol_contract，存在于这段连续 context 里。后续每写一个文件都以此为准；若中途调整了某个签名，必须同步更新所有引用端。
This plan is your anchor. Refer back to it; keep names consistent with it.

[实现节奏 — 边写边验证，禁止盲写一大批再回头]
- 按依赖顺序自底向上写（先无依赖的 errors/types/tokens，再 lexer/parser，最后 executor 这类重依赖文件）。
- **每完成一个可运行单元，立即用 execute_command 跑真实入口**看 traceback：`python -c "import pkg.mod"`、`python -m pkg ...`、或跑你刚写的小测试。报错就在同一 context 内 read→fix→re-run，直到该单元干净。
- 写重依赖文件（如 executor）前，**直接 read_file 你自己刚写的依赖模块**确认真实签名 —— 你看得到它们，不要凭记忆。
- 写新文件就用 write_file 输出完整文件内容（见下方「新建文件例外」）；改已有文件用 replace_in_file 精确编辑。

[自测 — 必须留下可运行的测试]
- 覆盖 requirement 的关键能力路径，写进 tests/（pytest 风格，子目录测试文件顶部加 sys.path 注入三行）。
- 自己先把测试跑绿（execute_command 跑 pytest），再 task_complete。外部还有一道 test gate 会复核，**不要弱化断言来骗过测试**——那是把 bug 藏起来。
- 数值/范围断言：先 execute_command 跑出真实值再写断言，不要猜。
- **运行 pytest 时默认加 `-q`**（减少 PASSED 行噪音，节省 context）；需要完整 traceback 定位时再加 `-v`。
- **端到端 smoke 必写（硬性判据）**：若 requirement 涉及命令行 / CLI 入口（`__main__.py` / `cli.py` / console_scripts），**必须**新建 `tests/test_smoke.py`，用 subprocess 调真实入口（如 `subprocess.run([sys.executable, "-m", "<pkg>", ...], capture_output=True)`），断言退出码为 0 且关键输出正确。单元测试与实现共享同一套模块假设，**测不出跨文件 CLI 调用链断裂**——smoke 是唯一出口。外部 gate 会强制要求此文件，缺它不算完成。

[新建文件例外 — 覆盖 _CODER_ROLE 第 10 条]
_CODER_ROLE 的「禁止 write_file 整体重写」只适用于**已存在的 >100 行大文件**。**从零创建新文件就该用 write_file 一次写出完整内容**——这是正常且推荐的。只有在已存在的大文件上做局部修改时，才必须改用 replace_in_file。

[工具效率]
- 定位优先：search_in_files / list_symbols / get_symbol_definition 精确定位，别整文件 read 再筛。
- 并行无依赖调用：一轮内同时 fire 多个 read/search。
- 写工具失败会直接返回 error，不必再 read_file 确认。
- dispatch_subagent 仅用于真正大规模独立探索；小事直接做。

[环境知识 — 框架自动维护]
`.yansh/agent_state.md` 由框架在每次 execute_command 后自动更新（python/pytest 命令白/黑名单）。
任务开始及 context 压缩时框架会自动注入此文件，**无需手动读取或写入**。

[终止协议 — 必读]
- 完成判据：真实入口跑通 + 自测全绿 + 覆盖 requirement 全部能力 → `task_complete(success=true, summary="...")`。
- 卡死/约束冲突无解 → `task_complete(success=false, summary="卡在 X，需人工")`，**不要烧光剩余轮次**。
- **必须显式 task_complete 终止**，不要沉默退出（loop 会再追问一轮，浪费）。

Always respond in Chinese (用户的项目规则要求中文回复); task_complete 的 summary 字段必须中文，仅文件名/符号名/代码保持英文。
"""

_SOLO_ROLE_TEST_FIRST = """

[强制：测试优先工作流 — 不可跳过（本任务已启用）]
你必须严格按以下顺序，否则任务判定为未完成：
1. **动手写任何实现代码之前**，先在 tests/ 目录建好正规 pytest 测试骨架（按需求能力点分文件，如 tests/test_<模块>.py），每个关键能力至少写一个具体断言用例（初期可红/import 失败，但必须是 pytest 能收集的正规用例）。
2. 涉及 CLI / 命令行入口的，必须含 tests/test_smoke.py：用 subprocess 跑真实入口，断言退出码与关键输出。
3. 然后才实现功能，边实现边把对应测试跑绿。
**禁止用根目录 _test_*.py 这类临时草稿脚本代替正规 tests/ 测试**——框架只认 tests/ 下 pytest 能收集的用例。task_complete 前必须确认 tests/ 下有可被 pytest 收集的测试且全绿。
"""

_PLANNER_ROLE = """[Role: Planner Agent (Plan Mode)]
You are in Plan Mode — **all write tools are disabled**; you can only use read-only tools to explore code and think through approaches.
Your task: through multi-turn dialogue with the user, produce a clear, executable implementation plan (plan draft); the user decides via /approve whether to implement.

[Termination requirement - must read]
- After each turn of work, call `exit_plan_mode_signal(reason)` to signal "waiting for user review" — don't be silent
- To persist/modify the plan, call `update_plan_draft(content)` — **full replacement** of the latest draft (not append). Always provide the complete version
- **Do not** call `task_complete` — Plan Mode's exit is triggered by the user's /approve, not by you

[Dialogue rhythm]
- User raises a new requirement / extra info → first do necessary exploration (read key files, grep, look at symbols), then update_plan_draft (if the plan changes), finally exit_plan_mode_signal
- User says they're satisfied but hasn't /approve'd → a brief acknowledgement (one sentence), don't keep overhauling the draft
- User requests plan modifications → update_plan_draft directly, then exit_plan_mode_signal

[Suggested plan-draft structure]
## 目标
(one sentence: what problem to solve / outcome to achieve)
## 改动文件
- file_a.py: what / why
- file_b.py: ...
## 步骤
1. ...
2. ...
## 风险与权衡
- ...

[Anti-patterns to avoid]
- Jumping straight to a plan without exploration — read 1-3 key files first
- Overhauling the plan for a one-word change — update incrementally, reuse the prior structure
- Writing code or suggesting commands to execute — that's implementation phase; Plan Mode only outputs the plan

Always respond in Chinese (用户的项目规则要求中文回复); plan 草稿正文必须中文, 仅文件名/符号名保持英文.
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
        return "Current project structure:\n" + "\n".join(walk(ws)) + "\n"

    # 检测系统并生成命令提示
    system_name = platform.system()
    if system_name == "Windows":
        cmd_hint = "Runtime is Windows. Use Windows commands: view files with `type`, list directories with `dir`. Do NOT use cat/ls/grep."
    else:
        cmd_hint = "Runtime is Linux/Mac. Use Unix commands: view files with `cat`, list directories with `ls`."

    # 先获取当前 workspace 文件结构，注入到 LLM 上下文中避免重复创建
    tree_output = _generate_tree()
    project_rules = _get_project_rules()
    ws_files = list_files()
    files_list = "\n".join(f"- {f}" for f in ws_files.get("files", []))
    project_hint = (
        f"\nProject type: {_PROJECT_TYPE}. Default test command: {_PROJECT_TEST_CMD}."
        if _PROJECT_TYPE else ""
    )
    console.print("[Agent: Architect]", highlight=False)

    # [P1 #5] 用户要求"行号 / 兼容分析 / 改动范围"等需扫码才能写准的内容
    # → plan 入 LLM 前先派 explorer 子 agent 扫码，把代码事实作为额外上下文
    exploration_block = ""
    if _plan_needs_exploration(requirement):
        console.print(
            "[Architect] 检测到任务要求『代码细节级输出』 → 先派 explorer 扫码",
            style="cyan", highlight=False,
        )
        explorer_task = (
            f"用户任务：\n{requirement}\n\n"
            "你的任务（只读探索，不修改任何文件）：\n"
            "1. 用 search_in_files / get_symbol_definition 精准定位涉及的关键文件、函数、行号\n"
            "2. 优先用 search_in_files 搜符号/关键词确认调用点数量，避免整文件逐行读取\n"
            "3. 总结：涉及哪些文件、每个文件大致需要多少处修改（用于 expected_edits 估算）\n"
            "4. 通过 task_complete(success=True, summary=<报告>) 返回结果，"
            "summary 必须含 file:line 引用和每文件预估修改次数。"
        )
        try:
            sub_result = _run_subagent(explorer_task, role="explorer",
                                       max_steps=8, token_budget=60_000)
            sub_summary = (sub_result or {}).get("summary", "").strip()
            if sub_summary:
                exploration_block = (
                    "\n\n# Code exploration results (from explorer subagent)\n"
                    "Use the following code facts (file:line references) when generating the plan, "
                    "especially when the plan output needs to describe specific lines, change scopes, or compatibility:\n\n"
                    f"{sub_summary}\n"
                )
        except Exception as _e:
            console.print(f"[Architect] explorer 失败（继续走原路径）：{_e}",
                          style="yellow", highlight=False)

    system_prompt = f"""{_ARCHITECT_ROLE}{project_rules}
You are a code-planning assistant. Given the user requirement, return a plan strictly conforming to the JSON schema below:

{{
  "symbol_contract": {{
    "<module_filename>": {{
      "<ClassName>": {{"fields": ["<field1>", "<field2>"], "members": ["<MEMBER1>", "<MEMBER2>"]}},
      "<func_or_var>": {{}}
    }}
  }},
  "files": [
    {{"filename": "<relative path>", "description": "<change intent>", "expected_edits": <int>}}
  ],
  "test_command": "<command to run tests>"
}}

symbol_contract value 支持两种格式（向后兼容）：
- 扁平列表：`["Foo", "bar"]`（仅记录导出名，适合无内部成员引用的简单场景）
- 成员级 dict（推荐，≥3 文件的新建项目必须用此格式）：`{{"ClassName": {{"fields": [...], "members": [...], "methods": [{{"name": "do", "params": ["x", "y"]}}]}}}}`（methods 项也可为纯字符串，向后兼容）
  - `fields`：其他文件会以 `obj.field` 访问的 dataclass/类实例属性名
  - `members`：其他文件会以 `EnumType.MEMBER` 访问的枚举成员名
  - `methods`：跨文件调用的方法。每项为 `{{"name": "register_csv", "params": ["table_name", "path"]}}` dict（推荐，params 不含 self）或纯字符串（向后兼容）。列出 params 可防止调用端参数数量不对齐。
  - 纯函数/变量用 `{{}}` 作为占位值，例：`{{"analyze": {{}}}}`（被其他文件 import 的顶层函数也必须作为 key 出现，否则易漏列函数型导出 → ImportError）
  - **被多处调用的模块级函数强烈建议声明签名**：`{{"build_logical_plan": {{"params": ["resolved", "catalog"]}}}}`（params 不含 self，**只列必填参数——有默认值的省略**，否则会误报 arity）。系统据此检测各调用点 arity 是否一致——真实失败：`build_logical_plan` 定义 2 参，`__main__.py` 调 1 参、测试调 2 参，三处不一致致 fixer 反复横跳改不好。声明 params 后所有调用点会被强制统一到此签名。

Full example（多文件语言解释器）：
{{"symbol_contract": {{"mini/ast_nodes.py": {{"LetDecl": {{"fields": ["name", "value"]}}, "IfStmt": {{"fields": ["cond", "then_block", "else_block"]}}, "BinaryOp": {{"fields": ["left", "op", "right"]}}}}, "mini/token.py": {{"TokenType": {{"members": ["IDENTIFIER", "INT", "FLOAT", "STRING", "PLUS", "MINUS", "STAR", "SLASH", "EOF"]}}}}}}, "files": [{{"filename": "mini/parser.py", "description": "recursive descent parser", "expected_edits": 8}}], "test_command": "pytest tests/"}}

Field constraints:
- Each files entry requires filename; description describes the change intent (no full code)
- For existing files, only describe what to append/modify — do not recreate
- **symbol_contract** (MANDATORY when ≥3 new files are being created; **系统会硬校验，缺失将被自动拒绝并要求重新生成**): authoritative source of truth for all cross-file symbol names.
  - **Must include class-internal members** when another file accesses them: enum members (`TokenType.IDENTIFIER`), dataclass fields (`node.value`, `node.cond`), and **cross-file method calls** (`ExprEvaluator.evaluate`). Omitting these causes AttributeError at runtime — import works but execution crashes.
  - Coder and fixer MUST use exact names from this contract — never inventing synonyms (e.g. if contract says `value`, never write `initializer`; if contract says `IDENTIFIER`, never write `IDENT`).
  - Single-file or pure-modification tasks may omit this field.
- **expected_edits**: estimated number of edit/replace operations this file needs (1 for new file write, 1-3 for tweaks, 5-20 for medium refactor, 30+ for sweeping signature changes). Used by the coder loop to allocate per-file round budget — undercount → coder hits round limit and task fails halfway. When in doubt, **overestimate by 50%**.
- **End-to-end smoke test** (MANDATORY when the requirement gives a real run command + expected output, see Architect role above): add a `tests/test_smoke.py` entry (`expected_edits: 1`) that drives the real entry point via subprocess, and make sure `test_command` includes it. Skeleton:
  `import subprocess, sys` → `r = subprocess.run([sys.executable, "-m", "<pkg>", "--data", d, "--sql", q], capture_output=True, text=True)` → `assert r.returncode == 0` + `assert "<expected stdout fragment>" in r.stdout`, plus one error case asserting the `Error[` prefix on stderr with exit code 1. NEVER import internal functions in the smoke test — it must go through `python -m`.

Directory layout: implementation files at workspace/ root (e.g. add.py); test files must go in workspace/tests/ (e.g. tests/test_add.py).
filename takes a relative path only; do NOT prepend "workspace/". Correct: hello.py, tests/test_hello.py. Wrong: workspace/hello.py.
test_command must NOT use `python -c` inline (blocked by security policy); use `python filename.py` instead.

{cmd_hint}{project_hint}

{tree_output}

Existing files in workspace:
{files_list if files_list else "(empty)"}

Note: do not recreate existing files; prefer incremental changes. For existing files, only describe what to append/modify.{exploration_block}"""
    system_prompt = _append_active_prompts(system_prompt)
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
    from functools import partial as _partial
    _plan_parser = _partial(_parse_plan_with_status,
                            existing_files=ws_files.get("files", []))
    plan_result = _call_with_json_retry(
        "plan", messages, _plan_parser,
        response_format={"type": "json_object"},
    )
    # [P1 #5] 把 explorer summary 持久化到 plan_result，让 coder 阶段也能读到
    # （否则 plan() 内部消费完就丢了，coder 写文档时仍凭训练知识猜）
    if exploration_block and isinstance(plan_result, dict):
        plan_result["_exploration"] = exploration_block
    return plan_result


def _parse_plan_with_status(content: str, existing_files=None):
    """返回 (ok, plan_dict, err_msg)。retry 包装用此版本；旧 _parse_plan_response 委托到这里。

    existing_files: 当前 workspace 已有文件的相对路径列表（来自 list_files()）。
    传入时对 ≥3 新文件的 plan 强制校验 symbol_contract 是否存在；None 时跳过（向后兼容）。
    """
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
    out = {"files": files_out, "test_command": validated.test_command}
    sc = getattr(validated, "symbol_contract", None) or raw.get("symbol_contract")
    if isinstance(sc, dict):
        out["symbol_contract"] = sc

    # 后置校验：≥3 新文件时 symbol_contract 为 MANDATORY（系统硬校验）
    if existing_files is not None:
        existing_norm = {os.path.normpath(f) for f in existing_files}
        new_files = [
            f for f in files_out
            if os.path.normpath(
                f.get("filename", f) if isinstance(f, dict) else f
            ) not in existing_norm
        ]
        sc_val = out.get("symbol_contract")
        if len(new_files) >= 3 and not (isinstance(sc_val, dict) and sc_val):
            return False, out, (
                f"缺失 symbol_contract：本计划新建 {len(new_files)} 个文件（≥3），"
                "symbol_contract 为 MANDATORY 字段，请重新输出包含 symbol_contract 的完整 plan。"
                "用成员级 dict 格式列出所有跨文件 import 的类名、函数名、枚举成员、dataclass 字段、跨文件方法名。"
                "示例：{\"analyzer.py\": {\"analyze\": {}, \"Analyzer\": {\"methods\": [{\"name\": \"run\", \"params\": [\"path\"]}]}}}"
            )

    return True, out, None


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
    # [P1 #5] plan() 派 explorer 时存的代码事实——coder 写文档/重构时拼到 system_prompt
    _exploration = plan.get("_exploration", "") if isinstance(plan, dict) else ""
    # 多文件符号契约：architect 在 plan 里钉死的跨模块符号名，注入每个文件的 sys_prompt 防命名分叉
    _contract = plan.get("symbol_contract", {}) if isinstance(plan, dict) else {}

    _contract_block = _render_contract(_contract)  # 调用模块级 _render_contract
    console.print("[Agent: Coder]", highlight=False)
    console.print(f"计划处理 {len(files)} 个文件...")
    coder_signal = None  # 多文件循环结束时上送给 run()

    for file_entry in files:
        if interrupt.is_interrupted():
            raise interrupt.Interrupted()

        if isinstance(file_entry, dict):
            filename = file_entry.get("filename", "")
            intent = file_entry.get("intent", file_entry.get("description", ""))
            expected_edits = int(file_entry.get("expected_edits", 0) or 0)
        else:
            filename = file_entry
            intent = ""
            expected_edits = 0

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
            # P2 #4-A1: 不再 read_file 取 existing_content（user message 不再内联）
            # LLM 主动调 read_file 拿内容；这里只确认 file_exists 走 edit 分支
            console.print(f"{filename} 已存在，进入增量修改流程（LLM 自己 read_file）...")

            req_block = f"\nOriginal requirement (must be followed strictly, including variable names, library names, API key names, etc.):\n{requirement}\n" if requirement else ""
            # P2 #4-C: >=20 处批量改动时允许 write_file 整文件重写，避免与 edit_strategy_hint 矛盾
            _write_rule = (
                f"- For large batch changes ({expected_edits} edit points on this file): "
                f"**prefer write_file to rewrite the whole file in one shot** — read the full file first, "
                f"apply all changes in memory, then write_file once. "
                f"This is faster and less error-prone than {expected_edits} replace_in_file calls."
                if expected_edits >= 20 else
                "- For existing files use replace_in_file for precise edits; "
                "write_file is only allowed for creating new files"
            )
            sys_prompt = f"""{_CODER_ROLE}
{_get_project_rules()}{req_block}
You are a code-editing assistant. Make precise modifications to existing files.

Available operations:
1. replace_in_file(filename, old_str, new_str) — precise replacement on an existing file
2. write_file(filename, content) — create new files or rewrite existing files for large batch changes

Rules:
- **Pre-flight check (MANDATORY first step)**: Your FIRST tool call MUST be search_in_files for the exact identifier/string the intent introduces (e.g. the new function name, new param, new key). Then decide by the result:
  - **Found (total >= 1) — NEW code task**: if the intent is to ADD something new (new function, new param, new key), the change is ALREADY applied → task_complete(success=True, "No changes needed") IMMEDIATELY.
  - **Found (total >= 1) — FIX/MODIFY task**: if the intent is to FIX a bug or MODIFY existing code, the match is the code that needs fixing (NOT proof it's done) → MUST read_file and apply the fix.
  - **Not found (total == 0)**: the change is missing → proceed to read_file and apply the edit.
- Never read_file before the pre-flight search. Never re-read a file you have already read this task (results are cached).
- **Read first**: if the pre-flight check is inconclusive or changes are needed, call read_file (or get_symbol_definition / search_in_files for targeted lookup) on `{filename}` to see the current content. The user message NO LONGER inlines the file body — you must fetch it via tools.
{_write_rule}
- Each replace_in_file call modifies one place; for multiple changes call it multiple times{_exploration}{_contract_block}"""
            sys_prompt = _append_active_prompts(sys_prompt)
        else:
            console.print(f"{filename} 是新建文件...")
            req_block = f"\nOriginal requirement (must be followed strictly, including variable names, library names, API key names, etc.):\n{requirement}\n" if requirement else ""
            sys_prompt = f"""{_CODER_ROLE}{req_block}
You are a code-generation assistant. Produce the complete code for file `{filename}`.

Available operations:
1. write_file(filename, content) — write a new file

Requirement / change intent: {intent}

Note: you must use write_file to write the file; the filename must be exactly `{filename}` — do not change the path or add a directory prefix.{_exploration}{_contract_block}"""
            sys_prompt = _append_active_prompts(sys_prompt)

        # 构建消息：expected_edits 显式告诉 LLM 本文件改动规模 → 选合适策略
        # >=15 处提示首选 write_file 整文件重写（变化散乱时）；中等用并行 replace_in_file；<5 单点改
        if expected_edits >= 20:
            edit_strategy_hint = (
                f"\n\n【改动规模提示】expected_edits={expected_edits}。大批量改动 — "
                f"如果各 edit 点 old_str 各不相同（无法 replace_all），**必须用 write_file 一次重写整个文件**（read 全文 → 内存应用所有改动 → write_file），"
                f"禁止用 {expected_edits}+ 次 replace_in_file 逐点替换（会耗尽 round budget）。"
            )
        elif expected_edits >= 5:
            edit_strategy_hint = (
                f"\n\n【改动规模提示】expected_edits={expected_edits}。中等改动 — "
                f"一轮内并行发多个 replace_in_file（不同 old_str），不要一次只改一处。"
                f"重复完全相同的 old_str→new_str 用 replace_all=True。"
            )
        else:
            edit_strategy_hint = ""
        user_content = f"当前文件：{filename}\n修改意图：{intent}{edit_strategy_hint}"
        # P2 #4-A1: 不再内联 existing_content（避免每轮重发整文件）—— 改让 LLM 主动 read_file
        # 节省每轮 ~20K input token（对应 1500 行文件场景），靠 read_cache 防重复实读

        msgs = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_content}
        ]

        # 多轮工具调用循环；新文件第一轮强制调用 write_file
        # plan-driven 调度：基线 N 轮 与 expected_edits + 4 取大（不再除以 edits_per_round）
        # 每个 edit 前需先 read（Pattern 要求），现实约 1 edit/round；按 expected_edits + buffer 给轮次。
        # 上限放宽到 _hard_cap(40)，真正的浪费兜底交给「无进展熔断」+「费用熔断」。
        _base_rounds = int(_cfg("coder_rounds_per_file") or 5)
        _expected_rounds = expected_edits if expected_edits > 0 else 0
        _hard_cap = int(_cfg("coder_max_rounds_per_file") or 12)
        attempts_left = max(_base_rounds, _expected_rounds + 4)
        attempts_left = min(attempts_left, _hard_cap)
        attempts_left = max(attempts_left, min(_base_rounds, _hard_cap))  # 防 hard_cap < base_rounds 饿死小任务
        _round_budget = attempts_left  # 警告打印时显示真实上限
        first_call = True
        _signaled_complete_this_file = False  # [P1 #2] 本文件是否已 task_complete(success=True)
        _no_progress = 0
        _no_progress_cap = int(_cfg("coder_no_progress_rounds") or 4)
        _edit_tool_names = ("write_file", "replace_in_file", "append_to_file", "replace_symbol")

        # P2 #4-B2: auto-compact 跨轮状态
        _compact_state = _make_compact_state()

        while attempts_left > 0:
            attempts_left -= 1

            # P2 #4-B2: 每轮 call_llm 前检测是否需 compact
            msgs = _maybe_compact_messages(msgs, _compact_state)

            if first_call and not file_exists:
                tc = {"type": "function", "function": {"name": "write_file"}}
            else:
                tc = "auto"
            first_call = False
            response = call_llm(msgs, tools=TOOLS, tool_choice=tc)
            response_message = response.choices[0].message
            msgs.append(response_message)

            if response_message.tool_calls:
                outs = _dispatch_tool_calls(
                    response_message.tool_calls, mode=mode,
                    allow_hil=True, allow_confirm=True, snap=_CURRENT_SNAPSHOT,
                    messages=msgs, console_label="",
                )
                # P0b: search_in_files 命中后注入判断引导
                # fix/修复类任务禁用：命中代表 bug 仍在，不是已完成
                _req_lc = (requirement or "").lower()
                _is_fix_task = any(k in (requirement or "") for k in (
                    "修复", "修改", "崩溃", "bug", "错误", "报错", "问题", "失败", "异常",
                )) or any(k in _req_lc for k in ("fix", "crash", "bug", "error", "broken", "fail", "exception"))
                if not _is_fix_task:
                    for out in outs:
                        if out.get("name") == "search_in_files":
                            _sif_res = out.get("result", {})
                            if isinstance(_sif_res, dict) and _sif_res.get("total", 0) >= 1:
                                msgs.append({
                                    "role": "user",
                                    "content": "[pre-flight] search_in_files 命中已有匹配。"
                                               "请判断：命中的是 intent 要新增的代码（→ 可 task_complete 完成）"
                                               "还是 intent 要修改的现有代码（→ 必须继续读文件并修改）。"
                                               "重要：只有命中的符号名与 requirement 要求新增的名称**完全一致**时才能认为已存在。"
                                               "私有变量（如 `_LOG_DIR`）≠ 公开常量（`LOG_DIR`）；"
                                               "相似函数名（如 `show_recent_logs`）≠ 目标函数（`get_recent_logs`）。"
                                               "只有确认是新增代码已存在（精确名称匹配）时才 task_complete。",
                                })
                                console.print(
                                    "[P0b pre-flight] search_in_files 命中，注入判断引导",
                                    style="cyan", highlight=False,
                                )
                                break

                _early_exit_inner = False  # 收到 task_complete sentinel 时用来跳出 inner while
                for out in outs:
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
                        _signaled_complete_this_file = True
                        # [P1 #4] coder 报"无需修改" → multi-file 循环 short-circuit
                        # 任务实际已完成（baseline 已实现），不必再处理剩余 expected_edits 文件
                        if _summary_says_no_changes(_summary):
                            coder_signal["no_changes_needed"] = True
                            console.print(
                                f"[Coder task_complete] 检测到 '无需修改' 信号 → 跳过剩余 {len(files) - files.index(file_entry) - 1} 个文件",
                                style="cyan", highlight=False,
                            )
                        _early_exit_inner = True
                        break
                if _early_exit_inner:
                    break
                # 无进展熔断：本轮无任何成功编辑则累计，连续 N 轮停止本文件
                _did_edit = any(
                    o.get("name") in _edit_tool_names
                    and isinstance(o.get("result"), dict) and "success" in o["result"]
                    for o in outs
                )
                if _did_edit:
                    _no_progress = 0
                else:
                    _no_progress += 1
                    if _no_progress >= _no_progress_cap:
                        _w = f"[无进展熔断] {filename} 连续 {_no_progress_cap} 轮无有效编辑，停止本文件"
                        console.print(_w, style="yellow", highlight=False)
                        _task_log_mod._current_task_log.setdefault("warnings", []).append(_w)
                        break
            else:
                break

        if attempts_left <= 0 and response_message.tool_calls and not _signaled_complete_this_file:
            # #8 上限耗尽仍在调工具，提示并记录（上限已 plan-driven 动态调整）
            # [P1 #2] task_complete(success=True) 跟用尽同轮触发时不警告 — coder 已主动收尾
            warn = f"[警告] {filename} 已用尽 {_round_budget} 轮工具调用上限（expected_edits={expected_edits}）"
            console.print(warn, style="yellow", highlight=False)
            _task_log_mod._current_task_log.setdefault("warnings", []).append(warn)

        # [P1 #4] coder 报"无需修改" → 直接退出 multi-file 循环
        if coder_signal and coder_signal.get("no_changes_needed"):
            break

    console.print("代码生成/修改完成")

    # pyproject.toml 有变更时自动重装包，确保新增模块立即可用
    if any("pyproject.toml" in (f or "") for f in _task_log_mod.snapshot_files_modified()):
        import subprocess as _sp
        console.print("[自动] 检测到 pyproject.toml 变更，执行 pip install -e . ...", highlight=False)
        r = _sp.run(["pip", "install", "-e", "."], capture_output=True, text=True)
        if r.returncode == 0:
            console.print("[自动] pip install -e . 完成", highlight=False)
        else:
            console.print(f"[警告] pip install -e . 失败: {r.stderr[:200]}", style="yellow", highlight=False)

    return coder_signal

def audit(requirement, model_override: str | None = None):
    """审计现有代码，只读多轮工具调用，最终输出 markdown 报告。
    返回 {"success": bool, "report": str}。
    model_override: 可指定模型（如 "claude-haiku-4-5" 用于轻量只读任务）。"""
    console.print("[Agent: Auditor]", highlight=False)

    # P0 #1：默认只注入顶层结构，子目录用 path 参数按需深挖（避免大项目撑爆 context）
    ws_symbols_result = workspace_symbols()  # default top
    if "error" in ws_symbols_result:
        symbols_brief = f"(workspace_symbols failed: {ws_symbols_result['error']})"
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
            lines.append("Sub-directories (drill in with workspace_symbols(path='<dir>') or directory_summary(path='<dir>')):")
            for d, info in sorted(subdirs_map.items()):
                lines.append(f"  {d}/  ({info['py_files']} .py files / {info['total_symbols']} symbols)")
        symbols_brief = (
            f"Workspace top-level symbol index ({ws_symbols_result['total_files']} top-level files / "
            f"{ws_symbols_result['total_symbols']} top-level symbols; drill into sub-dirs as needed):\n"
            + "\n".join(lines)
        )

    sys_prompt = f"{_AUDITOR_ROLE}{_get_project_rules()}\n\n{symbols_brief}"
    sys_prompt = _append_active_prompts(sys_prompt)
    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": f"Audit requirement: {requirement}"}
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
                        f"This audit has used {used} tokens (budget {_AUDIT_TOKEN_BUDGET}). "
                        "Wrap up with task_complete(success, summary) soon — do not start new exploratory tool calls."
                    ),
                })
                budget_warned = True

        response = call_llm(messages, tools=audit_tools, tool_choice="auto", stream=False,
                            model_override=model_override)
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
            outs = _dispatch_tool_calls(
                msg.tool_calls, mode="audit", allow_hil=False, allow_confirm=False,
                snap=None, messages=messages, console_label=f"审计轮 {rounds_used}",
            )
            # P0 #3 sentinel：LLM 主动声明任务结束（多个 tool_call 并发后统一扫一遍）
            for out in outs:
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
                        "You did not call any tool this turn — by protocol you must terminate explicitly with task_complete(success, summary). "
                        "If the report is done, call task_complete(success=true, summary=...); "
                        "if you cannot continue, call task_complete(success=false, summary=...)."
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
    # P2 #8 Skills：plan_mode 独立扫描（mode='plan'）
    try:
        import skills as _skills_mod
        skill_frag, matched = _skills_mod.load_and_format(
            user_input, _get_workspace(), mode="plan"
        )
        if skill_frag:
            sys_prompt += skill_frag
            if matched:
                console.print(f"[skills] 命中 {len(matched)} 个：{', '.join(s.name for s in matched)}",
                              highlight=False)
    except Exception:
        pass
    # P2 #12 Memory：plan_chat 独立扫索引
    try:
        import memory as _mem_mod
        mem_idx = _mem_mod.load_memory_index(_get_workspace())
        if mem_idx:
            sys_prompt += mem_idx
    except Exception:
        pass
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
            outs = _dispatch_tool_calls(
                msg.tool_calls, mode="audit", allow_hil=False, allow_confirm=False,
                snap=None, messages=messages, console_label=f"plan 轮 {rounds_used}",
            )
            done = False
            for out in outs:
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


# ============================================================
# P2 #9 子 Agent / 任务分派：context 隔离 + role 切工具集 + 禁递归
# ============================================================

# P4-5：以下 _SUBAGENT_ROLE / _subagent_tools_for_role / _build_subagent_system_prompt /
# _run_subagent / _subagent_handler / get_subagent_stats 已搬到 subagent.py，
# 这里只 re-export 保持向后兼容。
from subagent import (
    _SUBAGENT_ROLE,
    _subagent_tools_for_role,
    _build_subagent_system_prompt,
    _run_subagent,
    _subagent_handler,
    get_subagent_stats,
)




# ============================================================
# P2 #10 MCP 协议：启动 server + 把工具注入 TOOLS + atexit 关闭
# ============================================================

def init_mcp(verbose: bool = True) -> dict:
    """启动 mcp.json 里配置的所有 server，把发现的工具注入 TOOLS 列表。
    main.py 启动时调一次。返回 {"started": {name: tool_count}, "errors": [...]}。

    实现细节：直接 extend TOOLS 列表（动态扩展），子 agent / audit 各路径都读到同一份。
    """
    started, errors = _mcp_mod.start_all_servers(_get_workspace(), verbose=verbose)
    if started:
        new_schemas = _mcp_mod.discover_tools_as_schemas()
        # P2-4：TOOLS 修改 + 子 agent 迭代必须互斥，否则 hot reload mcp 时
        # 子 agent 的 _subagent_tools_for_role 会撞 RuntimeError
        with _TOOLS_LOCK:
            existing_mcp = {t["function"]["name"] for t in TOOLS
                            if t["function"]["name"].startswith("mcp__")}
            if existing_mcp:
                TOOLS[:] = [t for t in TOOLS
                            if not t["function"]["name"].startswith("mcp__")]
            TOOLS.extend(new_schemas)
        if verbose:
            for n, cnt in started.items():
                console.print(f"[mcp] {n} 启动（{cnt} 工具）", highlight=False)
    if errors and verbose:
        for n, err in errors:
            console.print(f"[mcp] {n} 启动失败：{err}", style="yellow", highlight=False)
    return {"started": started, "errors": errors}


def shutdown_mcp() -> None:
    """atexit 钩子用：关掉所有 mcp server 子进程"""
    try:
        _mcp_mod.shutdown_all()
    except Exception:
        pass


def review(requirement, modified_files):
    """代码审查阶段"""
    console.print("[Agent: Reviewer]", highlight=False)
    console.print("开始审查代码...")
    
    file_contents = []
    for filename in dict.fromkeys(modified_files):
        if not filename:
            continue
        # P2 #4-A2 review M2: review 阶段需要整文件给 reviewer LLM 看，绕过默认 limit
        content = read_file(filename, limit=10**9, max_bytes=10**9).get("content", "")
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

def _scan_import_mismatches(plan, workspace):
    """扫描 plan 中各 .py 文件的相对 import，返回目标模块中不存在的导入名列表。
    结果格式：[(importer_file, target_rel_path, missing_name, asname_or_none), ...]
    解析失败时安全返回空列表，不影响主流程。

    模块解析策略：用相对路径推导目标文件（而非 basename），避免子目录/同名模块碰撞。
    保守策略：含 star-import 的模块标记为 exports_unknown，不对其发起缺失判定（宁可漏报不误报）。
    """
    import ast as _ast_mod
    plan_items = plan.get("files", []) if isinstance(plan, dict) else (plan or [])
    plan_files = [
        os.path.normpath(p.get("filename", ""))
        for p in plan_items
        if isinstance(p, dict) and p.get("filename", "").endswith(".py")
    ]
    plan_files_set = set(plan_files)

    def _collect_exports(fpath):
        """返回 (names: set, unknown: bool)。unknown=True 表示含 star-import，不可静态枚举。
        收集范围：顶层 + if/try/with 块下一层（处理兜底实现、条件定义、re-export 聚合）。
        不进入 Function/Class 体（避免把局部名当导出）。
        """
        try:
            with open(fpath, encoding="utf-8", errors="replace") as _fh:
                tree = _ast_mod.parse(_fh.read())
        except Exception:
            return set(), True  # 解析失败，保守标记 unknown
        names: set = set()
        unknown = False

        def _scan_stmts(stmts):
            nonlocal unknown
            for node in stmts:
                if isinstance(node, (_ast_mod.ClassDef, _ast_mod.FunctionDef, _ast_mod.AsyncFunctionDef)):
                    names.add(node.name)
                elif isinstance(node, _ast_mod.Assign):
                    for t in node.targets:
                        if isinstance(t, _ast_mod.Name):
                            names.add(t.id)
                        elif isinstance(t, (_ast_mod.Tuple, _ast_mod.List)):
                            for elt in t.elts:
                                if isinstance(elt, _ast_mod.Name):
                                    names.add(elt.id)
                elif isinstance(node, _ast_mod.AugAssign):
                    if isinstance(node.target, _ast_mod.Name):
                        names.add(node.target.id)
                elif isinstance(node, _ast_mod.ImportFrom):
                    if any(a.name == "*" for a in node.names):
                        unknown = True  # star-import：导出集合不可静态确定
                    else:
                        # re-export：from .x import Foo / from .x import Foo as Bar
                        for a in node.names:
                            names.add(a.asname if a.asname else a.name)
                elif isinstance(node, _ast_mod.Import):
                    # import mod / import mod as alias
                    for a in node.names:
                        names.add(a.asname if a.asname else a.name.split(".")[0])
                elif isinstance(node, (_ast_mod.If, _ast_mod.With)):
                    # 下钻一层收集 if/with 体（条件定义、上下文管理器内的定义）
                    _scan_stmts(getattr(node, "body", []))
                    _scan_stmts(getattr(node, "orelse", []))
                elif isinstance(node, _ast_mod.Try):
                    # 下钻一层收集 try/except/else/finally（兜底实现）
                    _scan_stmts(node.body)
                    for handler in node.handlers:
                        _scan_stmts(handler.body)
                    _scan_stmts(getattr(node, "orelse", []))
                    _scan_stmts(getattr(node, "finalbody", []))

        _scan_stmts(tree.body)
        return names, unknown

    def _resolve_target(importer_fname, level, module):
        """将 import 解析为相对于 workspace 的路径（含 .py 后缀）。
        level>=1：相对 import，按父目录回退解析。
        level==0：包内绝对 import，尝试全路径 + 去顶层包名两种候选，命中 plan_files_set 才返回，否则返回 None（外部库，跳过）。
        """
        if level == 0:
            parts = module.split(".")
            candidates = [os.path.normpath(os.sep.join(parts) + ".py")]
            if len(parts) > 1:
                candidates.append(os.path.normpath(os.sep.join(parts[1:]) + ".py"))
            for cand in candidates:
                if cand in plan_files_set:
                    return cand
            return None  # 外部库或不在 plan，跳过
        base_dir = os.path.dirname(importer_fname)
        for _ in range(level - 1):
            base_dir = os.path.dirname(base_dir) or ""
        mod_path = module.replace(".", os.sep) if module else ""
        target = os.path.normpath(os.path.join(base_dir, mod_path) + ".py") if mod_path else None
        return target

    # 收集各模块导出（key = normpath 相对路径）
    module_exports: dict = {}      # path → set[str]
    module_unknown: set = set()    # 含 star-import 的模块路径集合（exports 不可枚举）
    for fname in plan_files:
        fpath = os.path.join(workspace, fname)
        if not os.path.exists(fpath):
            continue
        names, unknown = _collect_exports(fpath)
        module_exports[fname] = names
        if unknown:
            module_unknown.add(fname)

    # 扫描各文件的 from .x import Y 检查是否存在
    missing = []
    for fname in plan_files:
        fpath = os.path.join(workspace, fname)
        if not os.path.exists(fpath):
            continue
        try:
            with open(fpath, encoding="utf-8", errors="replace") as _fh:
                tree = _ast_mod.parse(_fh.read())
        except Exception:
            continue
        for node in _ast_mod.walk(tree):
            if not (isinstance(node, _ast_mod.ImportFrom) and node.module):
                continue
            target = _resolve_target(fname, node.level, node.module)
            if target is None or target not in plan_files_set:
                continue  # 不在 plan 内的模块跳过
            if target in module_unknown:
                continue  # 含 star-import，保守跳过
            exports = module_exports.get(target, set())
            for alias in node.names:
                if alias.name != "*" and alias.name not in exports:
                    missing.append((fname, target, alias.name, alias.asname))
    return missing


def _scan_contract_export_mismatches(plan, workspace):
    """反向校验：symbol_contract 声明的顶层导出名在实际文件中是否真的存在。
    返回 [(module_rel_path, declared_name), ...]
    仅报 plan 内文件、且文件已存在、且 exports 不含 star-import（unknown=False）时才判定。
    """
    import ast as _ast_mod
    contract = plan.get("symbol_contract") if isinstance(plan, dict) else None
    if not contract or not isinstance(contract, dict):
        return []

    plan_items = plan.get("files", []) if isinstance(plan, dict) else []
    plan_files_set = {
        os.path.normpath(p.get("filename", ""))
        for p in plan_items
        if isinstance(p, dict) and p.get("filename", "").endswith(".py")
    }

    def _collect_exports(fpath):
        """返回 (names: set, unknown: bool)，与 _scan_import_mismatches 内同名函数逻辑一致。"""
        try:
            with open(fpath, encoding="utf-8", errors="replace") as _fh:
                tree = _ast_mod.parse(_fh.read())
        except Exception:
            return set(), True
        names: set = set()
        unknown = False

        def _scan_stmts(stmts):
            nonlocal unknown
            for node in stmts:
                if isinstance(node, (_ast_mod.ClassDef, _ast_mod.FunctionDef, _ast_mod.AsyncFunctionDef)):
                    names.add(node.name)
                elif isinstance(node, _ast_mod.Assign):
                    for t in node.targets:
                        if isinstance(t, _ast_mod.Name):
                            names.add(t.id)
                        elif isinstance(t, (_ast_mod.Tuple, _ast_mod.List)):
                            for elt in t.elts:
                                if isinstance(elt, _ast_mod.Name):
                                    names.add(elt.id)
                elif isinstance(node, _ast_mod.ImportFrom):
                    if any(a.name == "*" for a in node.names):
                        unknown = True
                    else:
                        for a in node.names:
                            names.add(a.asname if a.asname else a.name)
                elif isinstance(node, (_ast_mod.If, _ast_mod.With)):
                    _scan_stmts(getattr(node, "body", []))
                    _scan_stmts(getattr(node, "orelse", []))
                elif isinstance(node, _ast_mod.Try):
                    _scan_stmts(node.body)
                    for handler in node.handlers:
                        _scan_stmts(handler.body)
                    _scan_stmts(getattr(node, "orelse", []))
                    _scan_stmts(getattr(node, "finalbody", []))

        _scan_stmts(tree.body)
        return names, unknown

    mismatches = []
    for mod_key, value in contract.items():
        mod_path = os.path.normpath(mod_key)
        if mod_path not in plan_files_set:
            continue  # 不在 plan 内，跳过
        fpath = os.path.join(workspace, mod_path)
        if not os.path.exists(fpath):
            continue
        actual_names, unknown = _collect_exports(fpath)
        if unknown:
            continue  # star-import，保守跳过

        # 提取契约声明的顶层导出名
        if isinstance(value, list):
            declared = list(value)
        elif isinstance(value, dict):
            declared = list(value.keys())
        else:
            continue

        for name in declared:
            if name not in actual_names:
                mismatches.append((mod_path, name))
    return mismatches


def _method_entry(m):
    """归一化 methods 列表项：dict 返回 (name, params|None)，字符串返回 (name, None)。"""
    if isinstance(m, dict):
        return m.get("name", ""), m.get("params") if isinstance(m.get("params"), list) else None
    return str(m), None


def _render_contract(contract):
    """渲染 symbol_contract 为人类可读的 sys_prompt 注入块（模块级，便于测试）。
    兼容旧扁平列表格式和新成员级 dict 格式。"""
    if not contract:
        return ""
    lines = ["", "# GLOBAL SYMBOL CONTRACT (authoritative — use EXACT names below, NO synonyms)"]
    for mod, value in contract.items():
        if isinstance(value, list):
            lines.append(f"- {mod} exports: {', '.join(sorted(str(n) for n in value))}")
        elif isinstance(value, dict):
            lines.append(f"- {mod}:")
            for cls_name, details in value.items():
                if not isinstance(details, dict) or (not details.get("fields") and not details.get("members") and not details.get("methods")):
                    lines.append(f"    exports: {cls_name}")
                else:
                    parts = []
                    if details.get("fields"):
                        parts.append(f"fields={', '.join(details['fields'])}")
                    if details.get("members"):
                        parts.append(f"members={', '.join(details['members'])}")
                    if details.get("methods"):
                        rendered = []
                        for m in details["methods"]:
                            name, params = _method_entry(m)
                            rendered.append(f"{name}({', '.join(params)})" if params is not None else name)
                        parts.append(f"methods={', '.join(rendered)}")
                    lines.append(f"    {cls_name}({'; '.join(parts)})")
    lines.append("使用跨模块符号时必须严格引用上表名字；禁止同义词（如契约写 value 则不能写 initializer，写 IDENTIFIER 则不能写 IDENT，写 evaluate 则不能写 eval）。")
    return "\n".join(lines)


def _scan_member_mismatches(plan, workspace, error_info=""):
    """扫描 plan 中各文件对枚举成员/dataclass 字段的引用，与 symbol_contract 对比，
    返回不匹配列表：[(file, lineno, access_expr, authority_name), ...]
    authority_name 是契约里的正确名字（有即返回，无则为 None）。
    解析失败安全返回空列表。

    检查两类引用：
    1. 枚举成员（精确）：`TokenType.IDENT` 且 IDENT 不在契约 members 里 → 报缺口
       绑定到具体 Enum 类型名（node.value.id），不会跨对象误报。
    2. dataclass 字段（严格保守）：只在 error_info 中已出现该属性名的 AttributeError 时才激活。
       不做类型绑定，通过 error_info 过滤把误报范围压到"已确认在错误日志里出现过"。
    """
    import ast as _ast_mod

    contract = plan.get("symbol_contract", {}) if isinstance(plan, dict) else {}
    if not contract:
        return []

    # 从契约中提取：枚举类名 → 合法成员集；所有 dataclass 合法字段集 + 同义词映射
    enum_members: dict = {}    # class_name → set of valid member names
    field_names: set = set()   # 所有契约 dataclass 字段的合法名字集合（跨类合并）
    # 已知同义对（错误名 → 权威名），用于 dataclass 字段保守检查
    _KNOWN_SYNONYMS = {
        "initializer": "value", "condition": "cond",
        "then_branch": "then_block", "else_branch": "else_block",
        "consequent": "then_block", "alternate": "else_block",
        "body": "then_block", "test": "cond", "init": "value",
        "identifier": "name", "token": "name", "expression": "value",
        "left_operand": "left", "right_operand": "right", "operator": "op",
    }

    method_pool: set = set()    # 所有契约 methods 名集合（跨类合并），用于 error_info 激活
    method_arity: dict = {}     # {method_name: param_count}（仅 dict 格式项有值）
    func_arity: dict = {}       # {func_name: param_count} 模块级裸函数签名（顶层项含 params）
    for mod_val in contract.values():
        if not isinstance(mod_val, dict):
            continue
        for cls_name, details in mod_val.items():
            if not isinstance(details, dict):
                continue
            if details.get("members"):
                enum_members[cls_name] = set(details["members"])
            if details.get("fields"):
                field_names.update(details["fields"])
            # 顶层裸函数签名：details 含 params 且无 class 成员（methods/fields/members）→ cls_name 是函数名
            if (isinstance(details.get("params"), list)
                    and not details.get("methods")
                    and not details.get("fields")
                    and not details.get("members")):
                func_arity[cls_name] = len(details["params"])
            for m in details.get("methods", []):
                name, params = _method_entry(m)
                if name:
                    method_pool.add(name)
                    if params is not None:
                        method_arity[name] = len(params)

    if not enum_members and not field_names and not method_pool and not func_arity:
        return []

    # dataclass 字段检查的触发集合：只含在 error_info AttributeError 中出现过的属性名
    # 这样保守同义词检查只对「已确认出现在错误日志里」的属性名生效，避免误报无关代码
    import re as _re
    _active_synonyms: set = set()
    # 从 traceback "has no attribute 'xxx'" 格式精确提取属性名
    for _m in _re.finditer(r"has no attribute '(\w+)'", error_info):
        _active_synonyms.add(_m.group(1))

    plan_items = plan.get("files", []) if isinstance(plan, dict) else (plan or [])
    plan_files = [
        os.path.normpath(p.get("filename", ""))
        for p in plan_items
        if isinstance(p, dict) and p.get("filename", "").endswith(".py")
    ]

    missing = []
    for fname in plan_files:
        fpath = os.path.join(workspace, fname)
        if not os.path.exists(fpath):
            continue
        try:
            with open(fpath, encoding="utf-8", errors="replace") as _fh:
                src = _fh.read()
            tree = _ast_mod.parse(src)
        except Exception:
            continue

        for node in _ast_mod.walk(tree):
            if not isinstance(node, _ast_mod.Attribute):
                continue
            attr = node.attr
            lineno = getattr(node, "lineno", 0)

            # 1. 枚举成员检查：`EnumClass.MEMBER`
            if isinstance(node.value, _ast_mod.Name):
                cls_name = node.value.id
                if cls_name in enum_members:
                    valid = enum_members[cls_name]
                    if attr not in valid:
                        # 找最近的权威名（精确匹配或大小写不敏感）
                        authority = next(
                            (v for v in valid if v.upper() == attr.upper()), None
                        )
                        missing.append((fname, lineno, f"{cls_name}.{attr}", authority))

            # 2. dataclass 字段检查（严格保守：仅在 error_info 中出现该属性名的 AttributeError 时激活）
            # 不绑定对象类型无法可靠判断字段归属，全文件扫描会大量误报无关对象上的常见属性名。
            # 只在测试报错已确认出现了该属性名才将其列为缺口，其余情况一律跳过。
            if attr in _KNOWN_SYNONYMS and field_names and _active_synonyms and attr in _active_synonyms:
                authority = _KNOWN_SYNONYMS[attr]
                if authority in field_names:
                    # 额外守卫：只报 Load 上下文（不报赋值左值）
                    if isinstance(node.ctx, _ast_mod.Load):
                        missing.append((fname, lineno, f"<obj>.{attr}", authority))

        # 3. 方法/函数调用检查
        # 3a. 方法名错误：仅在 error_info AttributeError 中出现且不在 method_pool 时报（保守）
        # 3b. 方法参数数量错误：契约有 arity 信息时主动扫描，不依赖 error_info（签名级问题不先抛 AttributeError）
        # 3c. 模块级裸函数 f(args) 参数数量错误：func_arity 有声明时主动扫描（多调用点 arity 不一致根因）
        if (method_pool and (_active_synonyms or method_arity)) or func_arity:
            for node in _ast_mod.walk(tree):
                if not isinstance(node, _ast_mod.Call):
                    continue

                # 3c. 裸函数调用 f(args)：node.func 是 Name
                if isinstance(node.func, _ast_mod.Name):
                    fn = node.func.id
                    if (fn in func_arity
                            and not any(isinstance(a, _ast_mod.Starred) for a in node.args)
                            and not any(k.arg is None for k in node.keywords)):
                        n_call = len(node.args) + len(node.keywords)
                        n_def = func_arity[fn]
                        if n_call != n_def:
                            lineno = getattr(node.func, "lineno", 0)
                            sig = f"{fn}({', '.join(['...'] * n_def)})"
                            missing.append((
                                fname, lineno, f"{fn}({n_call} args)",
                                f"[arity] 函数签名应为 {n_def} 参 {sig} — **把所有调用点统一到此签名，"
                                f"勿改函数定义去迁就单个调用点**；此处传了 {n_call} 个"
                            ))
                    continue

                if not isinstance(node.func, _ast_mod.Attribute):
                    continue
                attr = node.func.attr
                lineno = getattr(node.func, "lineno", 0)

                if attr in method_pool:
                    # 3b. 参数数量检测：契约声明了 arity 且调用端实参数不符时报
                    if attr in method_arity and not any(
                        isinstance(a, _ast_mod.Starred) for a in node.args
                    ) and not any(k.arg is None for k in node.keywords):
                        # *args 守卫：Starred in node.args；**kwargs 守卫：keyword.arg is None
                        n_call = len(node.args) + len(node.keywords)
                        n_contract = method_arity[attr]
                        if n_call != n_contract:
                            params_str = f"{attr}({', '.join(['...'] * n_contract)})"
                            missing.append((
                                fname, lineno,
                                f"<obj>.{attr}({n_call} args)",
                                f"[arity] 契约声明 {n_contract} 参：{params_str}，调用端传了 {n_call} 个"
                            ))
                    continue  # 方法名正确，跳过名称检查

                # 3a. 方法名错误（依赖 error_info 激活）
                if attr not in _active_synonyms:
                    continue
                authority = next(
                    (m for m in method_pool if m.lower() == attr.lower()), None
                ) or next(
                    (m for m in method_pool if m.startswith(attr[:3]) or attr.startswith(m[:3])), None
                )
                if authority is None and method_pool:
                    authority = f"（候选：{', '.join(sorted(method_pool))}）"
                missing.append((fname, lineno, f"<obj>.{attr}()", f"[疑似] → 契约方法名：{authority}"))

    return missing


def fix(test_result, plan, reason="test_failure", baseline_failures=None,
        disable_baseline_skip=False,
        prev_changed=None, prev_fail_count=None, cur_fail_count=None):
    """根据测试错误或审查意见修复代码（多轮工具调用）
    reason: "test_failure" | "review_rejection"
    baseline_failures: backlog #1，进入任务前已存在的失败 test id 集合。
      LLM 看到后应忽略这些 pre-existing 失败，只修增量失败。
    disable_baseline_skip: [P1 #6] 用户 prompt 明确要求修测试 / 修 bug 时设 True。
      除了不传 baseline_failures（已在调用方处理），还需在 user content 里反向覆盖
      _TESTER_ROLE 的"归属判断 → 跳过 pre-existing"引导，禁止 LLM 主动按归属规则
      task_complete(success=true) 跳过失败。
    prev_changed: [Debt 2] 上一轮 fix 修改的文件列表，用于 regression 警告。
    prev_fail_count: [Debt 2] 上一轮失败数，用于检测失败数上升（震荡信号）。
    cur_fail_count: [Debt 2] 本轮失败数。

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

    # 成员引用缺口专项：枚举成员名/dataclass 字段名不对齐，一次性暴露 + 方向约束
    if reason == "test_failure" and isinstance(plan, dict) and plan.get("symbol_contract"):
        try:
            _mem_issues = _scan_member_mismatches(plan, _get_workspace(), error_info=error_info)
            if _mem_issues:
                _mem_block = (
                    "\n\n**[成员引用错误 — 以下全部引用名与 symbol_contract 不符，本轮一次性全部修正]**\n"
                    "⚠ 契约是唯一真值源：**只允许把引用端改成契约名，严禁修改定义端**（如 token.py 的枚举定义、ast_nodes.py 的 dataclass 字段）去迁就引用端。\n"
                    "本轮内一次性全部 replace_in_file 修完：\n"
                )
                for _f, _ln, _expr, _auth in _mem_issues:
                    _auth_str = f"→ 契约权威名：`{_auth}`" if _auth else "→ 请查 symbol_contract 确认正确名称"
                    _mem_block += f"  - {_f}:{_ln}  `{_expr}`  {_auth_str}\n"
                error_info += _mem_block
        except Exception as _mem_err:
            console.print(f"[debug] _scan_member_mismatches 失败（不影响修复）：{_mem_err}", style="dim", highlight=False)

    # 级联 import 错误专项：每轮无条件主动扫描（不再依赖 error_info 含 ImportError），
    # 一次性暴露所有未定义导入名，让 LLM 单轮修完
    if reason == "test_failure":
        try:
            _imp_issues = _scan_import_mismatches(plan, _get_workspace())
            if _imp_issues:
                _imp_block = (
                    "\n\n**[级联导入错误 — 以下全部缺失导入名，本轮必须一次性全部修正]**\n"
                    "Python 每次只报第一个 ImportError；下表已帮你找出 plan 内所有缺口，本轮内一次性全部 replace_in_file 修完：\n"
                )
                for _f, _tgt, _n, _asname in _imp_issues:
                    _alias_str = f" as {_asname}" if _asname else ""
                    _tgt_mod = os.path.splitext(_tgt.replace(os.sep, "."))[0]
                    _imp_block += f"  - {_f}: `from {_tgt_mod} import {_n}{_alias_str}` — `{_n}` 在 {_tgt} 中不存在，请先 list_symbols({_tgt}) 确认正确名称\n"
                error_info += _imp_block
        except Exception as _scan_err:
            console.print(f"[debug] _scan_import_mismatches 失败（不影响修复）：{_scan_err}", style="dim", highlight=False)

        try:
            _ce_issues = _scan_contract_export_mismatches(plan, _get_workspace())
            if _ce_issues:
                _ce_block = (
                    "\n\n**[契约导出缺口 — 以下名称在 symbol_contract 中声明但实现端无对应顶层定义]**\n"
                    "契约是唯一真值源；若实现端用了不同名称（如 Analyzer 而非 analyze），必须把实现端改成与契约一致：\n"
                )
                for _mod, _declared in _ce_issues:
                    _ce_block += f"  - {_mod}: 契约声明导出 `{_declared}`，但文件中无此顶层定义——确认是函数还是类，按契约名补齐\n"
                error_info += _ce_block
        except Exception as _ce_err:
            console.print(f"[debug] _scan_contract_export_mismatches 失败（不影响修复）：{_ce_err}", style="dim", highlight=False)

    if reason == "review_rejection":
        content = f"Code review failed. Fix the code item-by-item per the review comments below:\n\n{error_info}\n\nPlan: {json.dumps(plan)}"
        sys_role = f"{_TESTER_ROLE}\n{_get_project_rules()}You are a code-review-fix assistant. Fix strictly per the review comments — one replace_in_file precise edit per comment. Do not touch code unrelated to the review comments."
        sys_role = _append_active_prompts(sys_role)
    else:
        # plan 可能是 plan_result 字典（含 "files"）或直接 list of file dicts，两种都兼容
        plan_items = plan.get("files", []) if isinstance(plan, dict) else (plan or [])
        plan_files = [p.get("filename", "") for p in plan_items if isinstance(p, dict)]
        # backlog #1：把 baseline pre-existing failures 直接注进 user content
        # 让 LLM 完全无歧义地知道哪些失败不归本次任务管
        baseline_block = ""
        if baseline_failures:
            _bl_list = sorted(baseline_failures)[:30]  # 30 条上限够人看
            baseline_block = (
                f"\n\n**Pre-existing baseline failures（任务开始前已存在，{len(baseline_failures)} 条）**：\n"
                + "\n".join(f"  - {t}" for t in _bl_list)
                + ("\n  - ...（省略）" if len(baseline_failures) > 30 else "")
                + "\n上面这些 test id **不在本次修复范围**——它们在你 plan 之前就已经红。"
                "如果当前所有失败都来自这个列表，**立即 `task_complete(success=true, summary='所有失败均为 pre-existing baseline，未引入回归')`** 收尾。\n"
                "只针对**不在上述列表**的新失败 fix。"
            )
        # [P1 #6] disable_baseline_skip=True 时反向覆盖 _TESTER_ROLE 的"归属判断跳过"引导
        # 否则 LLM 仍会自主按 system role 第 1 条 Investigation order 把失败归为 pre-existing 后 task_complete 跳过
        if disable_baseline_skip:
            content = (
                f"Tests failed.\nError output:\n{error_info}\n\nPlan files (this task's scope): {plan_files}\n\n"
                "**[强制覆盖 — 用户明确要求修测试 / 修 bug]**\n"
                "本次任务的用户 prompt 已明确要求修复测试失败 / bug。**所有当前失败的测试都必须尝试修复，不允许按归属规则跳过任何一条**。\n"
                "- 不要按 _TESTER_ROLE Investigation order 第 1 条做『plan files 归属』判断后跳过\n"
                "- 不要把失败归为 pre-existing 然后 task_complete(success=true) 收尾\n"
                "- 只有当所有失败都已**真正修复**（pytest 全绿）时才能 task_complete(success=true)\n"
                "- 实在修不动某条，task_complete(success=false, summary='...') 表示放弃，**不要 success=true 跳过**\n\n"
                "**严禁通过弱化测试断言（加 `or` 子句、改字面量、删除关键字）来让失败「过」** —— 这是把 bug 藏起来，不是修。"
            )
        else:
            content = (
                f"Tests failed.\nError output:\n{error_info}\n\nPlan files (this task's scope): {plan_files}"
                f"{baseline_block}\n\n"
                "归属判断（必读 — 走 _TESTER_ROLE 的 Investigation order 第 1 条）：\n"
                "失败断言里的符号 / 函数 / 测试目标对应的源文件，**是否在 Plan files 列表里**？\n"
                "- 在 → 本次任务引入的回归，正常修复\n"
                "- 不在 → 大概率是 pre-existing 失败，**直接 `task_complete(success=true, summary='X/Y/Z 等 N 条不属本次范围, 已跳过')` 收尾**，"
                "由用户判断是否单独立项；不要读测试文件再去揣摩，归属规则已足够定性。\n\n"
                "**严禁通过弱化测试断言（加 `or` 子句、改字面量、删除关键字）来让失败「过」** —— 这是把 bug 藏起来，不是修。"
            )
        # [Debt 2 / R8] 跨轮 regression 警告：失败数上升 → 注入震荡提示。
        # R8 修：注入条件不再硬依赖 prev_changed 非空——震荡时 fixer 反复改同一文件，
        # prev_changed 累计差集恒空会把上升 regression 警告吞掉（R7 build_logical_plan 91↔1 没拦住的主因）。
        # prev_changed 降级为可选文案：有则点名具体文件，无则泛化提示。
        if (prev_fail_count is not None and cur_fail_count is not None
                and cur_fail_count > prev_fail_count):
            _changed_line = (
                f"上一轮你修改了：{', '.join(prev_changed)}\n" if prev_changed
                else "复查你上一轮的改动——\n"
            )
            _reg_block = (
                f"\n\n**[⚠ 震荡警告 — 上一轮修改引入了 regression]**\n"
                f"{_changed_line}"
                f"失败数从 {prev_fail_count} 变为 {cur_fail_count}（**上升了 {cur_fail_count - prev_fail_count}**）。\n"
                "这说明你的修改破坏了别处的逻辑。**先 read_file 仔细复查你上轮改过的文件**，"
                "确认是否把其他代码依赖的接口或逻辑改坏了，再动手修。\n"
                "⚠ 同一处代码来回反复改（A→B→A）是震荡——尤其是『一个函数被多处用不同参数个数调用』时，"
                "**应把所有调用点统一到函数定义的签名，而不是改定义去迁就某一个调用点**（改定义会让其他调用点回归）。\n"
                "若确认是无解的约束冲突，task_complete(success=false, summary='...需人工裁决') 结束，不要烧完剩余轮次。"
            )
            content += _reg_block

        sys_role = f"{_TESTER_ROLE}\n{_get_project_rules()}You are a code-fix assistant. Fix the code precisely based on the error output — prefer replace_in_file for minimal changes, only use write_file to rewrite a whole file when necessary."
        sys_role = _append_active_prompts(sys_role)

    messages = [
        {"role": "system", "content": sys_role},
        {"role": "user", "content": content}
    ]

    # P0 #3 软上限 + token 预算 + sentinel 退出
    rounds_used = 0
    start_tokens = get_session_total_tokens()
    budget_warned = False
    silent_prompted = False  # 沉默退出兜底：LLM 没调工具时追问一次
    _compact_state = _make_compact_state()  # 费用诊断：fix loop 也需 auto-compact，否则 messages O(N²) 膨胀

    # 软上限可配置 + 机械错检测：测试失败如果是同一类 missing argument / TypeError
    # 这种"批量改完之前都过不了"的机械错，把 fix 上限再放一档预算
    fix_soft_limit = int(_cfg("fix_soft_limit") or _FIX_SOFT_LIMIT)
    if reason == "test_failure":
        import re as _re
        # [P1 #3] detector patterns 在 module-level（_MECH_ERROR_PATTERNS）
        _mech_hits = []
        for _pat, _label in _MECH_ERROR_PATTERNS:
            _c = len(_re.findall(_pat, error_info))
            if _c > 0:
                _mech_hits.append((_label, _c))
        # 哪怕只有 1 处同类机械错，也大概率是"signature 改了 / 调用方未全适配 / 属性重命名"——
        # 给追加预算让 fix 有机会扫齐所有遗漏点
        if _mech_hits:
            bonus = int(_cfg("fix_mechanical_error_bonus") or 12)
            fix_soft_limit += bonus
            _summary = ", ".join(f"{c}× {label}" for label, c in _mech_hits)
            console.print(
                f"[fix scheduler] 检测到机械错（{_summary}）→ "
                f"fix 上限提升到 {fix_soft_limit}（base + bonus={bonus}）",
                style="cyan", highlight=False,
            )

    while rounds_used < fix_soft_limit:
        rounds_used += 1
        if interrupt.is_interrupted():
            raise interrupt.Interrupted()

        # 费用诊断修复：每轮 call_llm 前 auto-compact，防 messages 线性累积全量重发
        messages = _maybe_compact_messages(messages, _compact_state)

        # token 预算检查（每轮开头）
        if not budget_warned:
            used = get_session_total_tokens() - start_tokens
            if used > _FIX_TOKEN_BUDGET:
                console.print(f"[预算] fix token 增量 {used} > {_FIX_TOKEN_BUDGET}，提醒 LLM 收尾",
                              style="yellow", highlight=False)
                messages.append({
                    "role": "system",
                    "content": (
                        f"This fix has used {used} tokens (budget {_FIX_TOKEN_BUDGET}). "
                        "Wrap up with task_complete(success, summary) soon — if you cannot continue, set success=false."
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
            # fix 阶段不弹覆盖确认（已经是修复路径，再问无意义），但仍允许 HIL
            outs = _dispatch_tool_calls(
                response_message.tool_calls, mode="code",
                allow_hil=True, allow_confirm=False, snap=_CURRENT_SNAPSHOT,
                messages=messages, console_label="",
            )
            for out in outs:
                # P0 #3 sentinel：LLM 主动声明任务结束（多个 tool_call 并发后统一扫一遍）
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
                        "You did not call any tool this turn — by protocol you must terminate explicitly with task_complete(success, summary). "
                        "If the fix is done, call task_complete(success=true, summary='what was done'); "
                        "if you cannot continue, call task_complete(success=false, summary='why giving up')."
                    ),
                })
                continue
            console.print("修复完成（沉默退出，已追问过一次）")
            return {"early_exit": False, "success": False, "summary": ""}
    console.print(f"[警告] fix 已达 {fix_soft_limit} 轮上限，强制退出", style="yellow", highlight=False)
    return {"early_exit": False, "success": False, "summary": ""}


# ============================================================
# solo mode：单一连续 context 端到端 agent
# ============================================================

def _solo_tools():
    """solo 工具集：全工具去掉 plan-mode 专用工具，保留 dispatch_subagent / 写工具 / execute_command。"""
    with _TOOLS_LOCK:
        all_names = {t["function"]["name"] for t in TOOLS}
    allowed = all_names - {"update_plan_draft", "exit_plan_mode_signal"}
    return _filter_tools(allowed)


def _get_out_result(outs: list, tc) -> dict:
    """按 tool_call_id 从 dispatch 结果列表里取 result dict。"""
    for o in outs:
        if isinstance(o, dict) and o.get("id") == tc.id:
            return o.get("result") or {}
    return {}


def _solo_drive(messages, tools, compact_state, *, soft_limit, start_tokens,
                budget_state, no_progress_state, capture_anchor: bool = False):
    """solo 主驱动循环。原地 append messages；budget_state / no_progress_state 跨 gate 回灌持续累积。
    返回 {"early_exit", "success", "summary", "rounds_used"}.
    """
    from subagent import _WRITE_TOOLS
    rounds_used = 0
    silent_prompted = False  # 沉默退出兜底：本次 drive 内只追问一次

    while no_progress_state["total_rounds"] < soft_limit:
        rounds_used += 1
        no_progress_state["total_rounds"] += 1
        if interrupt.is_interrupted():
            raise interrupt.Interrupted()

        # 每轮 call_llm 前 auto-compact，防连续 context O(N²) 膨胀全量重发
        messages[:] = _maybe_compact_messages(messages, compact_state, label="solo-compact")

        # token 预算软提醒（只一次）
        if not budget_state["warned"]:
            used = get_session_total_tokens() - start_tokens
            if used > _SOLO_TOKEN_BUDGET:
                console.print(f"[预算] solo token 增量 {used} > {_SOLO_TOKEN_BUDGET}，提醒 LLM 收敛",
                              style="yellow", highlight=False)
                messages.append({
                    "role": "system",
                    "content": (
                        f"You have used {used} tokens (budget {_SOLO_TOKEN_BUDGET}). "
                        "Converge: finish remaining files and tests, run them, then task_complete. "
                        "Do not start large new explorations."
                    ),
                })
                budget_state["warned"] = True

        response = call_llm(messages, tools=tools, tool_choice="auto")
        msg = response.choices[0].message
        messages.append({
            "role": "assistant",
            "content": msg.content,
            "tool_calls": [tc.model_dump() for tc in msg.tool_calls] if msg.tool_calls else None,
        })

        # P0-3：初始 drive 期间任意轮，只要 anchor 仍为 None 且本轮有文本就捕获
        if capture_anchor and compact_state.get("plan_anchor") is None:
            if msg.content and msg.content.strip():
                compact_state["plan_anchor"] = msg.content[:2000]

        if msg.tool_calls:
            _rn = no_progress_state["total_rounds"]
            console.print(f"solo 轮 {_rn}: {len(msg.tool_calls)} 次工具调用")
            outs = _dispatch_tool_calls(
                msg.tool_calls, mode="code", allow_hil=True, allow_confirm=False,
                snap=_CURRENT_SNAPSHOT, messages=messages, console_label=f"solo 轮 {_rn}",
            )
            # sentinel：LLM 主动声明任务结束
            for out in outs:
                if out["result"].get("_task_complete"):
                    success = bool(out["result"].get("success"))
                    summary = out["result"].get("summary", "")
                    console.print(f"solo task_complete（{'成功' if success else '放弃'}）：{summary}",
                                  style=None if success else "yellow", highlight=False)
                    return {"early_exit": True, "success": success,
                            "summary": summary, "rounds_used": rounds_used}
            # agent 级 no_progress：本轮是否有「正当进展」（区别于逐文件 no_progress）。
            # 端到端模式下，跑真实入口验证（execute_command）也是进展，不算空转——
            # R10 实测：写完全部模块后用 execute_command 连跑验证 12 轮被误熔断。
            # 真正的空转 = 纯 read/search/list/git 多轮无写无跑（R9 的探索死循环）。
            productive = any(
                (tc.function.name in _WRITE_TOOLS and _get_out_result(outs, tc).get("success"))
                or (tc.function.name == "execute_command"
                    and _get_out_result(outs, tc).get("returncode") not in (-1, None))
                for tc in msg.tool_calls
            )
            if productive:
                no_progress_state["streak"] = 0
            else:
                no_progress_state["streak"] += 1
                if no_progress_state["streak"] >= 2 * _SOLO_NO_PROGRESS_CAP:
                    console.print(f"[solo] 连续 {no_progress_state['streak']} 轮无写编辑/无运行，熔断",
                                  style="yellow", highlight=False)
                    return {"early_exit": False, "success": False,
                            "summary": "连续多轮无写编辑且未运行任何命令，疑似探索死循环，熔断", "rounds_used": rounds_used}
                if no_progress_state["streak"] == _SOLO_NO_PROGRESS_CAP:
                    messages.append({
                        "role": "system",
                        "content": (
                            f"You have gone {_SOLO_NO_PROGRESS_CAP} rounds doing only read/search/list "
                            "(no file edits, no command runs). If you are stuck exploring, commit to writing the "
                            "next file now using write_file, or run your entry point to verify. "
                            "If genuinely blocked, task_complete(success=false, summary=...)."
                        ),
                    })
        else:
            # 沉默退出兜底：第一次没调工具 → 追问一次
            if not silent_prompted:
                silent_prompted = True
                console.print("[兜底] solo 未调工具，追问一次要求显式 task_complete", style="yellow", highlight=False)
                messages.append({
                    "role": "system",
                    "content": (
                        "You did not call any tool this turn — by protocol you must terminate explicitly with task_complete(success, summary). "
                        "If everything is done and tests pass, task_complete(success=true, summary=...); "
                        "if you cannot continue, task_complete(success=false, summary=...)."
                    ),
                })
                continue
            console.print("solo 结束（沉默退出，已追问过一次）")
            return {"early_exit": False, "success": False,
                    "summary": "沉默退出", "rounds_used": rounds_used}

    console.print(f"[警告] solo 已达 {soft_limit} 轮上限，强制退出", style="yellow", highlight=False)
    return {"early_exit": False, "success": False,
            "summary": f"达到 {soft_limit} 轮上限", "rounds_used": rounds_used}


def solo(requirement, model_override=None):
    """单一连续 context 端到端 agent：自主规划→读写跑修→自测，外部 test gate 回灌复核。
    返回 {"success", "test_result", "task_complete_signal"?}（与 audit 分支对齐，保证 batch --json）。
    """
    console.print("[Agent: Solo]", highlight=False)

    # system prompt：role + 项目规则 + workspace 顶层符号索引（借 audit 的注入逻辑）
    ws_symbols_result = workspace_symbols()
    if "error" in ws_symbols_result:
        symbols_brief = f"(workspace_symbols failed: {ws_symbols_result['error']})"
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
            lines.append("Sub-directories (drill in with workspace_symbols(path='<dir>')):")
            for d, info in sorted(subdirs_map.items()):
                lines.append(f"  {d}/  ({info['py_files']} .py files / {info['total_symbols']} symbols)")
        symbols_brief = (
            f"Workspace top-level symbol index ({ws_symbols_result['total_files']} files / "
            f"{ws_symbols_result['total_symbols']} symbols):\n" + "\n".join(lines)
        )

    # 实验1：enforcement==role 时追加「测试优先」强硬段（解法A，概率性约束）
    _enforce = (_cfg("solo_test_enforcement") or "off").lower()
    _role = _SOLO_ROLE + (_SOLO_ROLE_TEST_FIRST if _enforce == "role" else "")
    sys_prompt = f"{_role}{_get_project_rules()}\n\n{symbols_brief}"
    sys_prompt = _append_active_prompts(sys_prompt)

    # 任务开始时注入持久环境知识（框架自动维护，跨 run 复用）
    _state_path = Path(_get_workspace()) / ".yansh" / "agent_state.md"
    try:
        _state_content = _state_path.read_text(encoding="utf-8").strip()
        if _state_content:
            _MAX_STATE = 4000
            if len(_state_content) > _MAX_STATE:
                _state_content = _state_content[:_MAX_STATE] + "\n[...已截断]"
            sys_prompt += f"\n\n[持久环境知识 .yansh/agent_state.md — 框架自动维护，跨 run 有效]\n{_state_content}"
    except Exception:
        pass

    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": f"Task: {requirement}"},
    ]

    tools = _solo_tools()

    # 快照：solo 不预知文件清单，用空快照；写工具按需增量备份（供 /revert）
    global _CURRENT_SNAPSHOT
    _gc_old_snapshots(keep=10)
    _CURRENT_SNAPSHOT = create_snapshot([])

    compact_state = _make_compact_state()
    start_tokens = get_session_total_tokens()
    budget_state = {"warned": False}
    no_progress_state = {"streak": 0, "total_rounds": 0}

    signal = _solo_drive(messages, tools, compact_state, soft_limit=_SOLO_SOFT_LIMIT,
                         start_tokens=start_tokens, budget_state=budget_state,
                         no_progress_state=no_progress_state, capture_anchor=True)

    # P0-1：agent 自述完成 = agent 主动 task_complete(success=true)
    agent_completed = bool(signal.get("early_exit") and signal.get("success"))
    # 发现6：是否「曾经」宣告完成（跨 gate 轮累积，含初始 drive）
    _ever_completed = agent_completed

    # P0-1：gate_status ∈ {"passed", "failed", "no_command", "coverage_unknown"}
    gate_status = "failed"
    # 外部 test gate 回灌：files_modified 推断 scope → 跑测试 → 红则把回灌内容 append 进同一 messages 继续驱动修复
    test_result = {"returncode": 0, "stdout": "", "stderr": ""}
    gate_round = 0
    _timeout_gate = int(_cfg("test_gate_timeout_sec") or 300)
    # P0-2 超时早停：连续两轮 timeout 且摘要相同则停止回灌
    _last_timeout_excerpt = None
    _consecutive_timeout = 0
    # P1-10：同错收敛检测
    _prev_gate_key = None
    # #2 fix：记录上一轮 drive 前的 modified 快照，用于判断本轮是否有新增写
    _prev_gate_modified: set = set()
    # 方案a #6：改入口文件却无 smoke 时只回灌一次要求补
    _smoke_demanded = False

    while gate_round < _SOLO_GATE_MAX_ROUNDS:
        # 发现6：累积「是否曾宣告完成」（含初始 drive 与上一 gate 轮 drive 的结果）
        _ever_completed = _ever_completed or agent_completed
        # P1-11 / R19 兜底：轮次耗尽不直接判失败，做一次最终裁定（不再回灌）
        if no_progress_state["total_rounds"] >= _SOLO_SOFT_LIMIT:
            console.print("[solo gate] 主 loop 轮次已耗尽，做一次最终裁定（不再回灌）", style="yellow", highlight=False)
            gate_status, test_result = _final_gate_verdict(Path(_get_workspace()), _timeout_gate)
            console.print(f"[solo gate] 最终裁定：{gate_status}", style="yellow", highlight=False)
            # 发现6：兜底裁定通过但 agent 烧满轮次未重宣告——若此前曾宣告完成则认可，避免假阴性
            if gate_status == "passed" and not agent_completed and _ever_completed:
                agent_completed = True
                console.print("[solo gate] 兜底通过且此前曾宣告完成，认可 agent_completed", style="green", highlight=False)
            break
        modified = _task_log_mod.snapshot_files_modified()
        _ws_path = Path(_get_workspace())
        # 解法B v2：主动 smoke 硬卡——改了 CLI 入口(__main__.py/cli.py)却无 tests/test_smoke.py，
        # 在任何测试运行/no_command 判定之前强制回灌补 smoke（端到端，随手单测糊弄不过去）。
        # 把 #6（原仅 passed 分支）下沉为主动拦截：collected-0、甚至无 test_cmd 时也卡得到。
        if (_enforce == "gate" and _entry_modified(modified) and not _smoke_exists(_ws_path)
                and not _smoke_demanded
                and gate_round < _SOLO_GATE_MAX_ROUNDS
                and no_progress_state["total_rounds"] < _SOLO_SOFT_LIMIT):
            _smoke_demanded = True
            gate_round += 1
            console.print("[solo gate] 强制 smoke：改了 CLI 入口但无 tests/test_smoke.py，回灌要求补", style="yellow", highlight=False)
            messages.append({"role": "user", "content": (
                "你改动了 CLI 入口文件（__main__.py / cli.py），但 tests/ 下没有 tests/test_smoke.py。"
                "本任务强制要求端到端 smoke：请新建 tests/test_smoke.py，用 subprocess 调用真实入口"
                "（如 subprocess.run([sys.executable, '-m', '<pkg>', ...], capture_output=True)），"
                "断言退出码为 0 且关键输出正确；先把它跑绿，再 task_complete。"
                "单元测试与实现共享同样的模块假设，测不出跨文件 CLI 调用链断裂，不能替代 smoke。"
            )})
            _remaining = _SOLO_SOFT_LIMIT - no_progress_state["total_rounds"]
            _prev_gate_modified = set(_task_log_mod.snapshot_files_modified())
            signal = _solo_drive(messages, tools, compact_state,
                                 soft_limit=no_progress_state["total_rounds"] + min(_SOLO_GATE_DRIVE_LIMIT, max(1, _remaining)),
                                 start_tokens=start_tokens, budget_state=budget_state,
                                 no_progress_state=no_progress_state)
            agent_completed = bool(signal.get("early_exit") and signal.get("success"))
            continue
        scope = _infer_test_scope([{"filename": f} for f in modified])
        # P0-4：两路共用 smoke 强并入
        scope = _force_include_smoke(scope, _ws_path)
        # P0-1 / P1-12：区分 scope 是否命中（targeted）还是全量兜底（full）；按项目类型选 detector
        _ptype = _PROJECT_TYPE or "python"
        if _ptype.lower() == "node.js" or _ptype.lower() == "node":
            from linter import _detect_node_test_cmd as _node_test_cmd
            targeted_cmd = None  # node detector 暂不区分 targeted
            test_cmd = _node_test_cmd(_ws_path)
            coverage = "full" if test_cmd else None
        else:
            targeted_cmd = _detect_python_test_cmd(_ws_path, scope=scope) if scope else None
            if targeted_cmd:
                test_cmd = targeted_cmd
                coverage = "targeted"
            else:
                test_cmd = _detect_python_test_cmd(_ws_path)
                coverage = "full" if test_cmd else None
        if not test_cmd:
            console.print("[solo gate] 无可用测试命令，跳过外部复核", style="yellow", highlight=False)
            gate_status = "no_command"
            break
        test_result = test(test_cmd, timeout_sec=_timeout_gate)
        # collected-0 边界：未收集到任何测试
        if not judge(test_result) and _no_tests_collected(test_result):
            # 解法B（enforcement==gate）：改了实现却无正规测试 → 回灌强制补，不放行（确定性拦截，不靠模型自觉）
            if (_enforce == "gate" and _has_impl_files(modified)
                    and gate_round < _SOLO_GATE_MAX_ROUNDS
                    and no_progress_state["total_rounds"] < _SOLO_SOFT_LIMIT):
                gate_round += 1
                console.print("[solo gate] 强制测试：改了实现但无正规 tests/，回灌要求补", style="yellow", highlight=False)
                messages.append({"role": "user", "content": (
                    "你已改动实现代码，但 tests/ 下没有任何 pytest 能收集的正规测试（collected 0）。"
                    "本任务强制要求留下可复核的测试：请在 tests/ 目录建正规 pytest 用例（覆盖关键能力路径；"
                    "若有 CLI 入口另加 tests/test_smoke.py，用 subprocess 跑真实入口断言退出码与输出），"
                    "把它们跑绿后再 task_complete。根目录 _test_*.py 临时脚本不算正规测试。"
                )})
                _remaining = _SOLO_SOFT_LIMIT - no_progress_state["total_rounds"]
                _prev_gate_modified = set(_task_log_mod.snapshot_files_modified())
                signal = _solo_drive(messages, tools, compact_state,
                                     soft_limit=no_progress_state["total_rounds"] + min(_SOLO_GATE_DRIVE_LIMIT, max(1, _remaining)),
                                     start_tokens=start_tokens, budget_state=budget_state,
                                     no_progress_state=no_progress_state)
                agent_completed = bool(signal.get("early_exit") and signal.get("success"))
                continue
            console.print("[solo gate] 未收集到任何测试（collected 0），判 no_command，不回灌",
                          style="yellow", highlight=False)
            gate_status = "no_command"
            break
        if judge(test_result):
            console.print(f"[solo gate] 测试通过（{test_cmd}）", style="green", highlight=False)
            # P0-1：scope 命中得到针对性测试 → passed；全量兜底 → coverage_unknown（不能证明本次改动被覆盖）
            gate_status = "passed" if coverage == "targeted" else "coverage_unknown"
            # 方案a #6：targeted 绿但改了 CLI 入口却无 tests/test_smoke.py → no_smoke（不放行），回灌一次要求补
            if gate_status == "passed" and _entry_modified(modified) and not _smoke_exists(_ws_path):
                if (not _smoke_demanded
                        and no_progress_state["total_rounds"] < _SOLO_SOFT_LIMIT
                        and gate_round < _SOLO_GATE_MAX_ROUNDS):
                    _smoke_demanded = True
                    gate_status = "no_smoke"
                    console.print("[solo gate] 改动含 CLI 入口但缺 tests/test_smoke.py，回灌要求补端到端 smoke",
                                  style="yellow", highlight=False)
                    messages.append({"role": "user", "content": (
                        "针对性单元测试已通过，但本次改动涉及 CLI 入口文件（__main__.py / cli.py），"
                        "却没有 tests/test_smoke.py。单元测试与实现共享同样的模块假设，"
                        "测不出跨文件 CLI 调用链断裂。请新建 tests/test_smoke.py：用 subprocess 调用真实入口"
                        "（如 subprocess.run([sys.executable, '-m', '<pkg>', ...], capture_output=True)），"
                        "断言退出码为 0 且关键输出正确，先把它跑绿，再 task_complete。"
                    )})
                    gate_round += 1
                    _remaining = _SOLO_SOFT_LIMIT - no_progress_state["total_rounds"]
                    _prev_gate_modified = set(_task_log_mod.snapshot_files_modified())
                    signal = _solo_drive(messages, tools, compact_state,
                                         soft_limit=no_progress_state["total_rounds"] + min(_SOLO_GATE_DRIVE_LIMIT, max(1, _remaining)),
                                         start_tokens=start_tokens, budget_state=budget_state,
                                         no_progress_state=no_progress_state)
                    agent_completed = bool(signal.get("early_exit") and signal.get("success"))
                    continue  # 回去重跑测试（agent 应已补 smoke，_force_include_smoke 会纳入）
                else:
                    # 已要求过仍未补，或轮次/gate 耗尽 → 保持 no_smoke，不放行
                    gate_status = "no_smoke"
                    break
            # #3 fix：gate 绿但 agent 未重宣告（drive 撞 _drive_limit 退出），给一次确认 drive
            if gate_status == "passed" and not agent_completed:
                if no_progress_state["total_rounds"] < _SOLO_SOFT_LIMIT:
                    messages.append({"role": "user", "content": "针对性测试已全部通过。如确认任务完成，请立即调用 task_complete(success=true)。"})
                    _confirm_signal = _solo_drive(
                        messages, tools, compact_state,
                        soft_limit=no_progress_state["total_rounds"] + 2,
                        start_tokens=start_tokens, budget_state=budget_state,
                        no_progress_state=no_progress_state,
                    )
                    agent_completed = bool(_confirm_signal.get("early_exit") and _confirm_signal.get("success"))
                    signal = _confirm_signal
            break
        gate_round += 1
        # P0-2：超时早停——连续 2 轮 timeout 且错误摘要相同，停止烧 gate 轮
        kind, hint = _classify_test_failure(test_result)
        if kind == "timeout":
            cur_excerpt = _clip((test_result.get("stderr") or "") + (test_result.get("stdout") or ""), 200, 200)
            if _last_timeout_excerpt is not None and cur_excerpt == _last_timeout_excerpt:
                _consecutive_timeout += 1
            else:
                _consecutive_timeout = 1
            _last_timeout_excerpt = cur_excerpt
            if _consecutive_timeout >= 2:
                console.print("[solo gate] 连续 2 轮超时且错误相同，停止回灌避免浪费", style="yellow", highlight=False)
                gate_status = "failed"
                break
        else:
            _consecutive_timeout = 0
            _last_timeout_excerpt = None

        # P1-10 / #2 fix：同错收敛检测——err_hash 用 test-id 集合，第三元用新增写标志
        _stdout = test_result.get("stdout") or ""
        _stderr = test_result.get("stderr") or ""
        _test_ids = _parse_pytest_failures(_stdout + _stderr)
        if _test_ids:
            _cur_err_hash = hashlib.md5(
                (" ".join(sorted(_test_ids))).encode()
            ).hexdigest()[:8]
        else:
            # 解析失败 fallback：用输出尾部 500 字符
            _cur_err_hash = hashlib.md5(
                ((_stderr + _stdout)[-500:]).encode()
            ).hexdigest()[:8]
        # 计算本 gate 轮内新增写（与上一轮 drive 前快照对比）
        _new_writes = set(_task_log_mod.snapshot_files_modified()) - _prev_gate_modified
        _cur_gate_key = (test_cmd, _cur_err_hash)
        # 收敛：test-id 集合 hash 相同 AND 本轮无新增写，才停止
        if _prev_gate_key is not None and _cur_gate_key == _prev_gate_key and not _new_writes:
            console.print("[solo gate] 同一失败集合连续两轮无新写，停止无效回灌", style="yellow", highlight=False)
            gate_status = "failed"
            break
        _prev_gate_key = _cur_gate_key

        # P0-5：用确定性 payload 回灌，不再 stderr or stdout 二选一
        feedback = _build_gate_feedback(test_cmd, test_result, kind, hint)
        console.print(f"[solo gate] 测试失败（{kind}），回灌驱动修复（gate 轮 {gate_round}）", style="yellow", highlight=False)
        messages.append({"role": "user", "content": feedback})
        # P1-9：每次 gate 回灌限制轮次，避免单次 drive 耗尽全部 soft_limit
        _remaining = _SOLO_SOFT_LIMIT - no_progress_state["total_rounds"]
        _drive_limit = min(_SOLO_GATE_DRIVE_LIMIT, max(1, _remaining))
        # 记录本次 drive 前的 modified 快照，供下轮收敛判定用
        _prev_gate_modified = set(_task_log_mod.snapshot_files_modified())
        signal = _solo_drive(messages, tools, compact_state,
                             soft_limit=no_progress_state["total_rounds"] + _drive_limit,
                             start_tokens=start_tokens, budget_state=budget_state,
                             no_progress_state=no_progress_state)
        # gate 每轮后更新 agent_completed（agent 可能在回灌后放弃）
        agent_completed = bool(signal.get("early_exit") and signal.get("success"))
    else:
        console.print(f"[solo gate] 回灌已达 {_SOLO_GATE_MAX_ROUNDS} 轮上限", style="yellow", highlight=False)
        gate_status = "failed"

    # P0-1：最终成功 = agent 自述成功 AND 针对性测试绿；其余一律 False
    final_success = agent_completed and (gate_status == "passed")
    return {
        "success": final_success,
        "test_result": test_result,
        "task_complete_signal": {
            "early_exit": signal.get("early_exit", False),
            "success": signal.get("success", False),   # agent 自述
            "agent_completed": agent_completed,
            "gate_status": gate_status,
            "summary": signal.get("summary", ""),
        },
    }


def test(test_command, timeout_sec=None):
    """执行测试命令。timeout_sec：None 则用配置 test_gate_timeout_sec（默认 300s）。
    注意：agent 主循环内自己跑的 execute_command 仍走默认 30s，互不干扰。"""
    if not test_command or not test_command.strip():
        console.print("警告：无测试命令，跳过测试")
        return {"returncode": 0, "stdout": "", "stderr": ""}
    _timeout = timeout_sec or int(_cfg("test_gate_timeout_sec") or 300)
    console.print(f"执行测试：{test_command}（超时 {_timeout}s）")
    return execute_command(test_command, _timeout_sec=_timeout)


def judge(test_result):
    """判断测试是否通过"""
    return test_result.get("returncode") == 0


def _classify_test_failure(tr: dict) -> tuple:
    """P0-2：把测试失败分三类，回灌时让 agent 知道该怎么处理。返回 (kind, hint)。"""
    if tr.get("error_kind") == "timeout":
        return ("timeout", "测试被超时强杀——可能用例太慢或死循环/死锁，不要当成断言失败盲改业务逻辑；考虑缩小本轮 scope 或排查阻塞点。")
    out = (tr.get("stderr") or "") + (tr.get("stdout") or "")
    rc = tr.get("returncode")
    if rc == 2 or "ModuleNotFoundError" in out or "ImportError" in out or "collected 0 items" in out:
        return ("uncollectable", "测试无法收集/运行（导入或崩溃），先修可运行性与 import 链，再谈断言。")
    return ("assertion", "断言失败，正常定位修复。")


def _clip(s: str, head: int = 1500, tail: int = 2000) -> str:
    """P0-5：保头尾截断，防回灌总量过大。"""
    if len(s) <= head + tail:
        return s
    return s[:head] + f"\n[... {len(s)-head-tail} chars omitted ...]\n" + s[-tail:]


def _build_gate_feedback(test_cmd: str, tr: dict, kind: str, hint: str) -> str:
    """P0-5：构造确定性 gate 回灌 payload。stdout/stderr 两路都给，不二选一；保头尾截断。"""
    rc = tr.get("returncode")
    stdout = tr.get("stdout") or ""
    stderr = tr.get("stderr") or ""
    # pytest 失败详情常在 stdout；运行时 traceback 常在 stderr。两路都给，不二选一。
    parts = [f"外部 test gate 运行 `{test_cmd}` 失败（returncode={rc}，类型={kind}）。", hint]
    if stdout.strip():
        parts.append("---- STDOUT（截断保头尾）----\n" + _clip(stdout))
    if stderr.strip():
        parts.append("---- STDERR（截断保头尾）----\n" + _clip(stderr))
    if not stdout.strip() and not stderr.strip():
        parts.append("（测试无输出）")
    parts.append("请在当前 context 内定位并修复，跑绿后再 task_complete。禁止弱化断言来骗过测试。")
    return "\n".join(parts)


def _parse_pytest_failures(text: str) -> set:
    """从 pytest 输出抽 FAILED 行的 test id（形如 'tests/x.py::test_y'）。
    pytest 短摘要行格式：`FAILED <nodeid> - <msg...>` 或 `FAILED <nodeid>`。
    解析失败返回空集，不抛异常。"""
    if not text:
        return set()
    import re as _re
    out = set()
    # 容忍 colorize / 行首空格；匹配到 ' - ' 或行尾结束
    for m in _re.finditer(r"^FAILED\s+(\S+?)(?:\s+-\s|$)", text, flags=_re.MULTILINE):
        out.add(m.group(1))
    return out


def _capture_baseline_failures(test_command: str) -> set:
    """code() 前跑一次 test_command 捕获 baseline failures。
    best-effort：任何异常返回 empty set，不影响主流程。"""
    if not test_command or not test_command.strip():
        return set()
    if "pytest" not in test_command:
        # 非 pytest 命令的输出格式无法解析，跳过 baseline 捕获
        return set()
    try:
        console.print(f"[baseline] 跑一次 {test_command} 记录 pre-existing failures...",
                      style="cyan", highlight=False)
        r = execute_command(test_command, _timeout_sec=120)
        text = (r.get("stdout") or "") + "\n" + (r.get("stderr") or "")
        failures = _parse_pytest_failures(text)
        # R6 必改：smoke test 永不进 baseline。否则 CLI 起始就崩 → smoke 红被记入 baseline，
        # run() 的 "current ⊆ baseline → 视为通过" 旁路会把假绿放行，治本方案被吞掉。
        failures = {f for f in failures if "test_smoke.py" not in f}
        if failures:
            console.print(f"[baseline] 记录 {len(failures)} 条 pre-existing failures（fix 阶段会忽略）",
                          style="cyan", highlight=False)
        else:
            console.print("[baseline] 0 条 pre-existing failures（干净）",
                          style="cyan", highlight=False)
        return failures
    except Exception as e:
        console.print(f"[baseline] 捕获失败（忽略，不影响主流程）：{e}",
                      style="yellow", highlight=False)
        return set()

def _print_read_cache_summary():
    """P2.1 命中率：所有任务出口（正常/中断/异常/早退）调一次。total=0 时静默。
    在 run() 的 finally 调用，避免 report() 早退路径丢汇总。"""
    total, hits, rate = _read_cache_stats()
    if total > 0:
        console.print(
            f"[read_cache] 总计 {total} 次, 命中 {hits} 次 ({rate * 100:.1f}%)",
            highlight=False,
        )


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

    # P2 #11 UserPromptSubmit hook（子 agent 内 / 全局禁用时跳过）
    extra_system_messages: list = []
    if not _is_in_subagent() and not _hooks_mod.is_disabled():
        try:
            ws = _get_workspace()
            hr = _hooks_mod.run_hook_event(
                "UserPromptSubmit",
                {"event": "UserPromptSubmit", "user_input": requirement,
                 "mode": mode, "cwd": ws},
                match_target=None,   # UserPromptSubmit 不按工具名 match
                workspace_dir=ws,
            )
            if hr.get("ran", 0) > 0:
                if hr["decision"] == "block":
                    console.print(f"[hook] UserPromptSubmit 阻止：{hr.get('reason', '')}",
                                  style="yellow", highlight=False)
                    return {"success": False,
                            "report": f"Hook 阻止用户输入：{hr.get('reason', '')}",
                            "test_result": {"stderr": "blocked by hook"}}
                # modify user_input
                mod = hr.get("modify", {})
                if isinstance(mod.get("user_input"), str):
                    requirement = mod["user_input"]
                    console.print("[hook] UserPromptSubmit 修改了 user_input", highlight=False)
                # 收集 system_message 注入下游 _run（通过 system_message 拼到 requirement 头）
                extra_system_messages = list(hr.get("system_messages", []))
                for err in hr.get("errors", []):
                    console.print(f"[hook] {err}", style="yellow", highlight=False)
        except Exception as e:
            console.print(f"[hook] UserPromptSubmit 异常忽略：{e}", style="yellow", highlight=False)
    if extra_system_messages:
        # 把 hook 给的 system_message 拼到 requirement 头部——LLM 会作为额外约束看到
        prefix = "\n".join(f"[hook 注入] {m}" for m in extra_system_messages)
        requirement = prefix + "\n\n" + requirement

    try:
        res = _run(requirement, mode)
        # #61 任务失败自动保存回放
        if not res["success"]:
            create_replay_package(res["test_result"].get("stderr", "任务失败"))

        # P2 #11 Stop hook（任务完成后，返回前）
        if not _is_in_subagent() and not _hooks_mod.is_disabled():
            try:
                ws = _get_workspace()
                hr = _hooks_mod.run_hook_event(
                    "Stop",
                    {"event": "Stop", "user_input": requirement,
                     "success": bool(res.get("success")),
                     "task_summary": str(res.get("report", ""))[:500],
                     "cwd": ws},
                    match_target=None,
                    workspace_dir=ws,
                )
                if hr.get("ran", 0) > 0 and hr.get("system_messages"):
                    # Stop 阶段任务已结束，system_message 直接打印给用户看
                    for sm in hr["system_messages"]:
                        console.print(f"[hook/Stop] {sm}", highlight=False)
                for err in hr.get("errors", []):
                    console.print(f"[hook] {err}", style="yellow", highlight=False)
            except Exception as e:
                console.print(f"[hook] Stop 异常忽略：{e}", style="yellow", highlight=False)
        return res
    except interrupt.Interrupted:
        return _interrupted_result()
    except CostExceededError as e:
        console.print(f"[费用熔断] {e}，保留已完成改动并停止。", style="yellow", highlight=False)
        try:
            finish_task_log(False, 0, {"returncode": -1, "stdout": "", "stderr": str(e)})
        except Exception:
            pass
        return {"success": False, "report": str(e),
                "test_result": {"returncode": -1, "stdout": "", "stderr": str(e)}}
    except Exception as e:
        # 异常退出也保存回放
        create_replay_package(str(e))
        raise
    finally:
        # P2.1 命中率汇总：所有出口（正常 return / Interrupted / 异常 raise / _run 内部早退 return）都打印
        _print_read_cache_summary()


def _path_match(planned: str, actual: str) -> bool:
    """按 / 边界比较路径后缀，避免无边界子串误判（如 add.py 匹配 myadd.py）。

    F1 fix: 第三分支仅对裸文件名（不含 /）的 actual 生效，防止
    plan="pkg/a.py" + actual="a.py" 因 p.endswith("/a.py") 造成假匹配。
    """
    p = planned.replace("\\", "/").strip("/")
    a = actual.replace("\\", "/").strip("/")
    return a == p or a.endswith("/" + p) or ("/" not in a and p.endswith("/" + a))


def _run(requirement, mode):
    _hil_mod.reset_auto_accept()  # 每轮任务重置 HIL "全部接受"标志
    os.makedirs(_get_workspace(), exist_ok=True)  # 兜底：新目录首次运行时 workspace/ 可能不存在
    original_requirement = requirement
    init_task_log(requirement, mode)
    _read_cache_clear()  # P2.1：每个新任务开始清 read_file 命中表

    # P2 #8 Skills：每次 run() 入口扫描 + 匹配 skill；后续 plan/code/audit/fix 共用 _ACTIVE_SKILLS_PROMPT
    global _ACTIVE_SKILLS_PROMPT, _ACTIVE_MEMORY_INDEX
    try:
        import skills as _skills_mod
        prompt_frag, matched = _skills_mod.load_and_format(
            requirement, _get_workspace(), mode=mode
        )
        _ACTIVE_SKILLS_PROMPT = prompt_frag
        if matched:
            console.print(f"[skills] 命中 {len(matched)} 个：{', '.join(s.name for s in matched)}",
                          highlight=False)
    except Exception as e:
        _ACTIVE_SKILLS_PROMPT = ""
        console.print(f"[skills] 加载失败（不影响主流程）：{e}", style="yellow", highlight=False)

    # P2 #12 Memory：每次 run() 入口加载 MEMORY.md 索引
    try:
        import memory as _mem_mod
        _ACTIVE_MEMORY_INDEX = _mem_mod.load_memory_index(_get_workspace())
        if _ACTIVE_MEMORY_INDEX:
            n_total = len(_mem_mod.discover_memories(_get_workspace()))
            console.print(f"[memory] 加载 {n_total} 条索引", highlight=False)
    except Exception as e:
        _ACTIVE_MEMORY_INDEX = ""
        console.print(f"[memory] 加载失败（不影响主流程）：{e}", style="yellow", highlight=False)

    # audit 模式：完全独立路径，不进 plan/code/review/fix
    if mode == "audit":
        res = audit(original_requirement)
        finish_task_log(res["success"], 0,
                        task_complete_signal=res.get("task_complete_signal"))
        return {"success": res["success"],
                "test_result": {"returncode": 0 if res["success"] else 1,
                                "stdout": res.get("report", ""), "stderr": ""}}

    # solo 模式：单一连续 context 端到端 agent，独立路径，不进 plan/code/review/fix
    if mode == "solo":
        res = solo(original_requirement)
        finish_task_log(res["success"], 0, test_result=res.get("test_result"),
                        task_complete_signal=res.get("task_complete_signal"))
        return {"success": res["success"],
                "test_result": res.get("test_result",
                                       {"returncode": 0 if res["success"] else 1,
                                        "stdout": "", "stderr": ""})}

    # [路由] code/auto 模式：按复杂度路由，减少简单/只读任务的 pipeline 开销
    # --mode audit 已在上方 early return，本块不影响显式 audit 模式
    plan_result = None  # 哨兵：未赋值时统一走 plan()，保证 plan/code/auto 模式都能赋值
    if mode in ("code", "auto"):
        _task_class = _classify_task(original_requirement)
        console.print(f"[路由] 任务分类：{_task_class}", highlight=False)

        if _task_class == "readonly":
            # 只读 → 直接走 audit()，跳过 plan/code/test/fix；用 haiku 降低延迟
            console.print("[路由] 只读任务 → audit 路径（haiku）", highlight=False)
            res = audit(original_requirement, model_override="claude-haiku-4-5")
            finish_task_log(res["success"], 0,
                            task_complete_signal=res.get("task_complete_signal"))
            return {"success": res["success"],
                    "test_result": {"returncode": 0 if res["success"] else 1,
                                    "stdout": res.get("report", ""), "stderr": ""}}

        if _task_class == "simple" and _simple_fast_eligible(original_requirement):
            # simple-fast：合成 plan_result，跳过 plan() LLM 调用
            _sf_fname = _extract_filename_from_requirement(original_requirement)
            # M2 修复：提前推断 test_command，让 baseline 捕获有效
            try:
                from linter import _detect_python_test_cmd
                _sf_scope = _infer_test_scope([{"filename": _sf_fname}])
                _sf_test_cmd = _detect_python_test_cmd(_get_workspace(), scope=_sf_scope) or ""
            except Exception:
                _sf_test_cmd = ""
            plan_result = {
                "files": [{"filename": _sf_fname, "intent": original_requirement,
                           "description": original_requirement, "expected_edits": 5}],
                "test_command": _sf_test_cmd,
                "_simple_fast": True,
            }
            console.print(f"[路由] simple-fast → 跳过 plan，目标文件：{_sf_fname}", highlight=False)
        elif _task_class == "simple":
            # 简单任务但无明确文件名：注入尺度 hint，仍走 plan()
            requirement = original_requirement + (
                "\n\n[任务规模提示] SIMPLE 任务（单文件/少量改动）。"
                "expected_edits ≤5，不需要 explorer 扫全局依赖。"
            )
        # complex → requirement 不变，走完整 pipeline

    # plan_result 为 None 时（plan/complex/simple-hint/plan 模式）统一调 plan()
    if plan_result is None:
        # 阶段1：制定计划
        console.print("阶段1：制定计划")
        plan_result = plan(requirement)
    _apply_test_scope_override(plan_result)
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
                _apply_test_scope_override(plan_result)
                print_plan(plan_result)
                retry_count += 1

    # backlog #1: 进入 code() 前先跑一次测试，记录 pre-existing failures
    # 后续 fix 循环只针对增量 failures（current \ baseline）；如果 LLM 改完后
    # current 等于 baseline 子集 → 视为通过（不再被 baseline 失败误判为回归）
    global _BASELINE_FAILURES
    _BASELINE_FAILURES = _capture_baseline_failures(plan_result.get("test_command", ""))

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

    # [Debt 3] code() 运行前，记录 tests/ 现有测试文件，用于检测越权新建
    global _PRE_TASK_TEST_FILES
    _ws_pre = Path(_get_workspace())
    _tests_dir_pre = _ws_pre / "tests"
    _PRE_TASK_TEST_FILES = set()
    if _tests_dir_pre.is_dir():
        for _tp in _tests_dir_pre.rglob("test_*.py"):
            _PRE_TASK_TEST_FILES.add(_tp.relative_to(_ws_pre).as_posix())

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

    # [Debt 3] 检测 coder 越权新建的测试文件，无条件从 test_command scope 排除。
    # 设计意图：test scope 以 plan_files 为准，coder 在 plan 外新建的测试文件（哪怕质量高）
    # 不应影响本任务的 success 判定，避免"coder 自建测试失败 → 任务被误判 success=False"。
    # 注意：此逻辑不依赖 prompt 是否含否定约束，对所有任务生效。
    _unsanctioned_tests: set[str] = set()
    _ws_post = Path(_get_workspace())
    _tests_dir_post = _ws_post / "tests"
    if _tests_dir_post.is_dir() and _PRE_TASK_TEST_FILES is not None:
        _current_tests_post = {
            _tp.relative_to(_ws_post).as_posix()
            for _tp in _tests_dir_post.rglob("test_*.py")
        }
        _plan_test_fnames = {
            (f.get("filename") if isinstance(f, dict) else f)
            for f in plan_result.get("files", [])
        }
        _plan_test_fnames = {
            Path(fn).as_posix() for fn in _plan_test_fnames
            if fn and Path(fn).name.startswith("test_")
        }
        _new_tests = _current_tests_post - _PRE_TASK_TEST_FILES
        _unsanctioned_tests = {
            f for f in _new_tests
            if not any(_path_match(pf, f) for pf in _plan_test_fnames)
        }
        if _unsanctioned_tests:
            console.print(
                f"[test_scope] coder 越权新建了 {len(_unsanctioned_tests)} 个未计划测试文件："
                f"{', '.join(sorted(_unsanctioned_tests))}，从 test_command scope 排除",
                style="yellow", highlight=False,
            )
            _apply_test_scope_override(plan_result, exclude=_unsanctioned_tests)

    # simple-fast：进 test loop 前，用实际修改文件补全 test_command（M3 修复：在 early_exit 之后）
    if plan_result.get("_simple_fast") and not plan_result.get("test_command"):
        try:
            _sf_modified = _task_log_mod.get_current_log().get("files_modified", [])
            if _sf_modified:
                from linter import _detect_python_test_cmd
                _sf_scope = _infer_test_scope([{"filename": f} for f in _sf_modified])
                _sf_cmd = _detect_python_test_cmd(_get_workspace(), scope=_sf_scope)
                if _sf_cmd:
                    plan_result["test_command"] = _sf_cmd
                    _task_log_mod._current_task_log["test_command"] = _sf_cmd
                    console.print(f"[simple-fast] 回填 test_command：{_sf_cmd}", highlight=False)
        except Exception as _e:
            console.print(f"[simple-fast] test_command 回填失败（跳过）：{_e}",
                          style="yellow", highlight=False)

    # 质量门：plan 里 expected_edits>0 的文件必须出现在 files_modified，否则 coder 未真正完成
    # fix() 不适合补做编码工作（它只修测试失败），所以这里只记录 missing，
    # 在 judge(test_result) 通过时拦截，防止假通过。
    def _safe_edits(val):
        try:
            return int(val or 0)
        except (TypeError, ValueError):
            return 0

    _planned_files = {
        f.get("filename") for f in plan_result.get("files", [])
        if isinstance(f, dict) and _safe_edits(f.get("expected_edits")) > 0
    }
    _actual_files = set(_task_log_mod.snapshot_files_modified())
    _missing_files = {f for f in _planned_files if f and not any(
        _path_match(f, a) for a in _actual_files
    )}

    def _check_still_missing():
        """重新采样已修改文件，返回仍未修改的计划文件集合（fix loop 可能已补改）。"""
        _new_actual = set(_task_log_mod.snapshot_files_modified())
        return {f for f in _missing_files if f and not any(_path_match(f, a) for a in _new_actual)}

    if _missing_files:
        _missing_msg = f"计划文件未被修改：{', '.join(sorted(_missing_files))}（coder 轮次耗尽未落地编辑）"
        console.print(f"[质量门] {_missing_msg}", style="yellow", highlight=False)

    console.print("\n阶段3：测试与修复")
    attempts = 0
    test_result = None
    max_attempts = _cfg("max_attempts") or _config_mod.MAX_ATTEMPTS

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
        _auto_generate_tests(plan_result, _task_log_mod.snapshot_files_modified())



    # #26 Linter：先跑 ruff，有错误走修复循环
    linter_result = run_linter(files=_task_log_mod.snapshot_files_modified())
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

    # [P1 #6] 用户明确要求修测试 / 修 bug 时禁用 baseline 跳过：
    # - run() 不把 current ⊆ baseline 视为通过
    # - fix() 不在 prompt 里注入 baseline 列表 → LLM 不会被诱导 task_complete(success=true) 收尾
    _user_wants_fix = _prompt_requests_test_fix(original_requirement)
    if _user_wants_fix and _BASELINE_FAILURES:
        console.print(
            f"[baseline] 用户 prompt 要求修测试 / 修 bug → 禁用 baseline 跳过（不再把当前失败误判为 pre-existing）",
            style="yellow", highlight=False,
        )

    # [Debt 2] 跨轮失败数和 changed files 追踪（oscillation detection）
    _prev_fail_count: int | None = None
    _prev_changed: list | None = None
    _last_snapshot_set: set | None = None  # 上一轮结束时的文件集，用于计算本轮差集

    while attempts < max_attempts:
        test_result = test(plan_result.get("test_command", ""))
        if judge(test_result):
            # 质量门二次检查：测试虽通过，但 coder 若未改计划文件，仅警告（不强制失败）
            # architect 可能过度规划了与任务无关的文件，硬拦截会造成误判，先用 warning 积累数据
            if _missing_files:
                _still_missing = _check_still_missing()
                if _still_missing:
                    console.print(
                        f"[质量门⚠] 计划文件未修改（仅警告）：{', '.join(sorted(_still_missing))}",
                        style="yellow", highlight=False,
                    )
            console.print("测试通过！")
            # 保留快照供 /revert（旧快照由下次任务的 _gc_old_snapshots 自动清理）
            files = plan_result.get("files", [])
            file_names = [f.get("filename") if isinstance(f, dict) else str(f) for f in files]
            file_names = [name for name in file_names if name]
            summary = f"执行了任务：{original_requirement}。创建/修改了文件：{', '.join(file_names)}"
            add_to_history(original_requirement, summary)
            finish_task_log(True, attempts, test_result, task_complete_signal=coder_signal)
            return report(True, test_result, task_complete_signal=coder_signal)

        # backlog #1: returncode!=0 但 current failures 是 baseline 子集 → 视为通过
        # [P1 #6] _user_wants_fix=True 时跳过此逻辑，强制走完整 fix loop
        _increment = None
        if _BASELINE_FAILURES and not _user_wants_fix:
            _cur_text = (test_result.get("stdout") or "") + "\n" + (test_result.get("stderr") or "")
            _cur_fail = _parse_pytest_failures(_cur_text)
            _increment = _cur_fail - _BASELINE_FAILURES
            if _cur_fail and not _increment:
                # 质量门：baseline-pass 旁路，同样仅警告
                if _missing_files:
                    _still_missing = _check_still_missing()
                    if _still_missing:
                        console.print(
                            f"[质量门⚠] baseline-pass 旁路，计划文件未修改（仅警告）：{', '.join(sorted(_still_missing))}",
                            style="yellow", highlight=False,
                        )
                console.print(
                    f"[baseline] 当前 {len(_cur_fail)} 条失败全部在 baseline 内（{len(_BASELINE_FAILURES)} 条 pre-existing）→ 视为通过",
                    style="green", highlight=False,
                )
                files = plan_result.get("files", [])
                file_names = [f.get("filename") if isinstance(f, dict) else str(f) for f in files]
                file_names = [name for name in file_names if name]
                summary = (
                    f"执行了任务：{original_requirement}。创建/修改了文件：{', '.join(file_names)}。"
                    f"测试 {len(_cur_fail)} 条失败均为 pre-existing baseline，未引入回归。"
                )
                add_to_history(original_requirement, summary)
                finish_task_log(True, attempts, test_result, task_complete_signal=coder_signal)
                return report(True, test_result, task_complete_signal=coder_signal)

        console.print(f"测试失败 (尝试 {attempts + 1}/{max_attempts})")
        if _increment:
            console.print(
                f"[baseline] 增量 failures: {len(_increment)} 条（baseline {len(_BASELINE_FAILURES)} 条已忽略）",
                style="cyan", highlight=False,
            )
        # [Debt 2] 计算本轮失败数（用于震荡检测）
        _cur_fail_count_raw: int | None = None
        try:
            _cur_text_raw = (test_result.get("stdout") or "") + "\n" + (test_result.get("stderr") or "")
            _cur_fail_raw = _parse_pytest_failures(_cur_text_raw)
            _cur_fail_count_raw = len(_cur_fail_raw)
        except Exception:
            pass

        if attempts < max_attempts - 1:
            # [P1 #6] _user_wants_fix=True 时：
            # 1) 不传 baseline_failures（user content 没有 baseline_block）
            # 2) disable_baseline_skip=True 让 fix() 在 user content 里反向覆盖 _TESTER_ROLE 的归属跳过引导
            _baseline_for_fix = None if _user_wants_fix else _BASELINE_FAILURES
            fix_signal = fix(test_result, plan_result,
                             baseline_failures=_baseline_for_fix,
                             disable_baseline_skip=_user_wants_fix,
                             prev_changed=_prev_changed,
                             prev_fail_count=_prev_fail_count,
                             cur_fail_count=_cur_fail_count_raw)
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
            # [Debt 2] 更新跨轮追踪状态（只取本轮新增文件差集，不累积）
            _prev_fail_count = _cur_fail_count_raw
            try:
                _cur_snapshot_set = set(_task_log_mod.snapshot_files_modified())
                _prev_changed = list(_cur_snapshot_set - (_last_snapshot_set or set()))
                _last_snapshot_set = _cur_snapshot_set
            except Exception:
                _prev_changed = None
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
        # P2 #4-A2 review M2: autogen tests 需要整文件给生成器 LLM 看，绕过默认 limit
        r = read_file(src, limit=10**9, max_bytes=10**9)
        if "content" in r:
            file_contents.append(f"# {src}\n{r['content']}")

    if not file_contents:
        return

    combined = "\n\n".join(file_contents)
    test_targets = ", ".join(f"tests/test_{Path(f).name}" for f in non_test_srcs)

    msgs = [
        {"role": "system", "content": f"""{_CODER_ROLE}
You are a test-generation assistant. Given the source code, generate a minimal test file covering three cases: normal path, boundary values, and invalid input.
Test files go under tests/ with filename format test_<original-filename>.py.
The test file must start with:
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
Use the write_file tool to write the test file — do not use other tools."""},
        {"role": "user", "content": f"Generate tests for the following source files (target files: {test_targets}):\n\n{combined}"}
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
                        _task_log_mod.record_file_modified(args.get("filename", ""))
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
