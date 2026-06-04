"""单元测试：_extract_filename_from_requirement + _simple_fast_eligible"""
import pytest
from agent import _extract_filename_from_requirement, _simple_fast_eligible


# ── _extract_filename_from_requirement ───────────────────────────────────────

@pytest.mark.parametrize("req, expected", [
    # 反引号包裹
    ("修复 `tools.py` 的 bug", "tools.py"),
    ("更新 `tests/unit/test_tools.py` 中的测试", "tests/unit/test_tools.py"),
    # 裸文件名
    ("修复 memory.py 里的 slug bug", "memory.py"),
    # 带路径
    ("改 src/agent.py 里的函数", "src/agent.py"),
    # 多个文件取第一个
    ("`tools.py` 和 `agent.py` 都要改", "tools.py"),
    # 各种扩展名
    ("更新 README.md", "README.md"),
    ("修改 config.json", "config.json"),
])
def test_extract_filename_found(req, expected):
    result = _extract_filename_from_requirement(req)
    assert result == expected, f"输入 {req!r}，期望 {expected!r}，实际 {result!r}"


@pytest.mark.parametrize("req", [
    "分析一下并发条件",
    "重构整个架构",
    "修复这个 bug",          # 无文件名
    "add the feature",
    "",
])
def test_extract_filename_not_found(req):
    assert _extract_filename_from_requirement(req) is None, f"不应找到文件名：{req!r}"


# ── _simple_fast_eligible ────────────────────────────────────────────────────

@pytest.mark.parametrize("req", [
    "修复 `memory.py` 里 find_memory 的 slug bug",
    "给 `tools.py` 的 read_file 加 max_bytes 参数",
    "fix the offset bug in `tools.py`",
    "update `config.json` with new defaults",
])
def test_simple_fast_eligible_true(req):
    assert _simple_fast_eligible(req) is True, f"应该 eligible：{req!r}"


@pytest.mark.parametrize("req", [
    # 无文件名
    "修复这个 bug",
    # complex 关键词
    "重构 `agent.py` 的整体架构",
    "迁移所有文件到新结构，修改 `tools.py`",
    # exploration 信号（调用链/行号）
    "找出 `agent.py` 里 _run 的调用链并修改",
    # m5：多文件 → 不走 fast
    "修复 `tools.py` 和 `agent.py` 的 bug",
])
def test_simple_fast_eligible_false(req):
    assert _simple_fast_eligible(req) is False, f"不应 eligible：{req!r}"


# ── m1/m2 修复验证 ────────────────────────────────────────────────────────────

def test_extract_url_not_matched():
    """URL 中的 .py 不应被提取"""
    assert _extract_filename_from_requirement("参考 https://example.com/foo.py") is None


def test_extract_bak_not_matched():
    """foo.py.bak 不应截断为 foo.py"""
    result = _extract_filename_from_requirement("修改 foo.py.bak 文件")
    assert result is None


def test_extract_init_py():
    """__init__.py 是合法目标"""
    assert _extract_filename_from_requirement("修改 `__init__.py`") == "__init__.py"
