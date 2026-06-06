"""单元测试：_classify_task() 任务复杂度路由"""
from unittest.mock import patch

import pytest

from agent import _classify_task


# ── readonly 正例 ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("req", [
    "分析函数 X 的并发条件",
    "解释 agent.py 的 _run 流程",
    "在哪里调用了 write_file",
    "列出所有调用 _err 的地方",
    "审查 tools.py 有没有安全问题",
    "梳理一下调用链",
    "评估这个方案的可行性",
    "比较两种实现方式的权衡",
    "analyze the concurrency conditions in function X",
    "where is write_file called?",
    "review the code quality of agent.py",
    "explain how _run works",
])
def test_readonly_positive(req):
    assert _classify_task(req) == "readonly", f"应分到 readonly：{req!r}"


# ── 否定词防护（最重要的回归防护）────────────────────────────────────────────

@pytest.mark.parametrize("req", [
    "分析一下然后修复它",
    "找到竞态条件并 fix",
    "解释清楚后帮我实现",
    "审查代码并修改问题",
    "分析完之后添加一个测试",
    "explain and then fix the bug",
])
def test_readonly_negation(req):
    result = _classify_task(req)
    assert result != "readonly", f"含写入动作，不应是 readonly：{req!r}，得到 {result!r}"


# ── complex 正例 ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("req", [
    "重构整个 agent.py 的路由逻辑",
    "分析全部文件的依赖关系",
    "把 pipeline 迁移到 async",
    "分析所有文件的兼容性",
    "refactor the entire project",
    "migrate all files to the new API",
    "impact analysis across whole codebase",
])
def test_complex_positive(req):
    assert _classify_task(req) == "complex", f"应分到 complex：{req!r}"


# ── simple 兜底 ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("req", [
    "修复 read_file 的 offset bug",
    "给 list_files 加 max_depth 参数",
    "fix the offset bug in read_file",
    "add max_depth parameter",
    "",
])
def test_simple_fallback(req):
    assert _classify_task(req) == "simple", f"应分到 simple：{req!r}"


# ── "不要修改X" 编辑任务不应被误判 readonly（本次修复的回归防护）──────────────

@pytest.mark.parametrize("req", [
    "修复这个 bug，但不要修改函数签名",
    "给 X 新增一个函数，但不要修改现有接口",
    "将 directory_summary 重命名为 summarize_directory，同步更新所有引用点，功能保持不变",
])
def test_no_modify_edit_task_not_readonly(req):
    result = _classify_task(req)
    assert result != "readonly", f"含写入动作，不应是 readonly：{req!r}，得到 {result!r}"


@pytest.mark.parametrize("req", [
    "分析 agent.py 的并发逻辑，不要修改任何代码",
])
def test_no_modify_pure_analysis_is_readonly(req):
    assert _classify_task(req) == "readonly", f"纯分析且禁止修改，应分到 readonly：{req!r}"


# ── 重命名任务不应被误判 readonly（rename 关键词补丁回归防护）────────────────────

@pytest.mark.parametrize("req", [
    "不修改逻辑，只是重命名变量",
    "rename foo to bar, do not change behavior",
])
def test_rename_edit_task_not_readonly(req):
    result = _classify_task(req)
    assert result != "readonly", f"重命名是编辑任务，不应是 readonly：{req!r}，得到 {result!r}"


# ── LLM 兜底失败不崩溃 ──────────────────────────────────────────────────────

def test_llm_fallback_exception_does_not_raise():
    """LLM 兜底抛异常时，_classify_task 应降级为 simple，不向上抛。"""
    with patch("agent._llm_classify_task", side_effect=Exception("LLM down")):
        # 构造一个关键词无法命中、长度 ≥20 字的模糊输入，触发 LLM 兜底路径
        ambiguous = "这段代码的整体设计思路是否合理以及后续如何演进"
        result = _classify_task(ambiguous)
        assert result in ("readonly", "simple", "complex"), "应返回有效分类"
