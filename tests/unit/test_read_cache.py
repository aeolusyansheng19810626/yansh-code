"""P2.1 read_file 命中检测单元测试。

覆盖：
  - 第二次相同 (filename, offset, limit) → 命中 → 返回 stub error，不调真 read_file
  - offset/limit 不同 → miss → 各自记录
  - _read_cache_clear → 重置 cache
  - 经 _dispatch_tool_call 路径整体行为：第二次返回 duplicate_read
"""
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import agent
import state


def _mk_read_file_tc(filename, offset=None, limit=None, tc_id="t1"):
    """构造一个 read_file 的 tool_call 对象（OpenAI 格式）"""
    args = {"filename": filename}
    if offset is not None:
        args["offset"] = offset
    if limit is not None:
        args["limit"] = limit
    import json as _json
    tc = MagicMock()
    tc.id = tc_id
    tc.function.name = "read_file"
    tc.function.arguments = _json.dumps(args)
    return tc, args


# ── 底层 helper 行为 ─────────────────────────────────────────────────────────


def test_read_cache_first_call_miss():
    agent._read_cache_clear()
    args = {"filename": "a.py", "offset": None, "limit": None}
    assert agent._read_cache_hit_or_record(args) is False


def test_read_cache_second_call_hit():
    agent._read_cache_clear()
    args = {"filename": "a.py", "offset": None, "limit": None}
    agent._read_cache_hit_or_record(args)  # 第一次
    assert agent._read_cache_hit_or_record(args) is True  # 第二次命中


def test_read_cache_different_offset_no_hit():
    """同 filename 不同 offset → 视为不同 read，都不命中"""
    agent._read_cache_clear()
    a1 = {"filename": "a.py", "offset": 1, "limit": 50}
    a2 = {"filename": "a.py", "offset": 51, "limit": 50}
    assert agent._read_cache_hit_or_record(a1) is False
    assert agent._read_cache_hit_or_record(a2) is False
    # 但同 (filename, 1, 50) 第二次命中
    assert agent._read_cache_hit_or_record(a1) is True


def test_read_cache_clear_resets():
    agent._read_cache_clear()
    args = {"filename": "x.py", "offset": None, "limit": None}
    agent._read_cache_hit_or_record(args)
    agent._read_cache_clear()
    assert agent._read_cache_hit_or_record(args) is False  # 清空后重新 miss


def test_read_cache_empty_filename_passthrough():
    """空 filename 不进 cache（让 read_file 自己处理参数错误）"""
    agent._read_cache_clear()
    args = {"filename": "", "offset": None, "limit": None}
    assert agent._read_cache_hit_or_record(args) is False
    assert agent._read_cache_hit_or_record(args) is False  # 仍然不命中


# ── 集成：_dispatch_tool_call_inner 路径 ────────────────────────────────────


def test_dispatch_read_file_second_returns_duplicate_error(tmp_path):
    """通过 _dispatch_tool_call_inner 跑：第二次同 read 返回 duplicate_read。"""
    with state.scoped_session(tmp_path):
        (tmp_path / "a.py").write_text("def f(): pass\n", encoding="utf-8")
        agent._read_cache_clear()

        tc1, args1 = _mk_read_file_tc("a.py", tc_id="c1")
        out1 = agent._dispatch_tool_call_inner(tc1, dict(args1), mode="code")
        assert "error" not in out1["result"], f"第一次应正常读: {out1['result']}"
        assert "content" in out1["result"]

        tc2, args2 = _mk_read_file_tc("a.py", tc_id="c2")
        out2 = agent._dispatch_tool_call_inner(tc2, dict(args2), mode="code")
        assert out2["result"].get("error") == "duplicate_read"
        assert "content" not in out2["result"], "命中时不应附 content（关键省 token）"
        assert "hint" in out2["result"]
        assert out2["result"]["filename"] == "a.py"


def test_dispatch_read_file_different_range_both_succeed(tmp_path):
    """同文件不同 range → 都正常读，不互相命中"""
    with state.scoped_session(tmp_path):
        (tmp_path / "a.py").write_text("\n".join(f"line{i}" for i in range(20)), encoding="utf-8")
        agent._read_cache_clear()

        tc1, a1 = _mk_read_file_tc("a.py", offset=1, limit=5, tc_id="c1")
        tc2, a2 = _mk_read_file_tc("a.py", offset=10, limit=5, tc_id="c2")
        out1 = agent._dispatch_tool_call_inner(tc1, dict(a1), mode="code")
        out2 = agent._dispatch_tool_call_inner(tc2, dict(a2), mode="code")
        assert "content" in out1["result"]
        assert "content" in out2["result"]
        assert out1["result"].get("error") != "duplicate_read"
        assert out2["result"].get("error") != "duplicate_read"


def test_dispatch_other_tools_not_affected(tmp_path):
    """非 read_file 工具不受 cache 影响"""
    with state.scoped_session(tmp_path):
        agent._read_cache_clear()
        # search_in_files 重复调用——不该被命中拦截
        import json as _json
        for i in range(3):
            tc = MagicMock()
            tc.id = f"s{i}"
            tc.function.name = "search_in_files"
            tc.function.arguments = _json.dumps({"pattern": "def"})
            out = agent._dispatch_tool_call_inner(tc, {"pattern": "def"}, mode="code")
            assert out["result"].get("error") != "duplicate_read"


# ── P2.1 命中率计数器 ────────────────────────────────────────────────────────


def test_stats_count_total_and_hits():
    """多次 read_file：total/hits 计数正确"""
    agent._read_cache_clear()
    a1 = {"filename": "b.py", "offset": None, "limit": None}
    a2 = {"filename": "c.py", "offset": None, "limit": None}
    agent._read_cache_hit_or_record(a1)  # miss, total=1
    agent._read_cache_hit_or_record(a2)  # miss, total=2
    agent._read_cache_hit_or_record(a1)  # hit,  total=3, hits=1
    agent._read_cache_hit_or_record(a1)  # hit,  total=4, hits=2
    total, hits, rate = agent._read_cache_stats()
    assert total == 4
    assert hits == 2
    assert abs(rate - 0.5) < 1e-9


def test_stats_clear_resets_counters():
    """_read_cache_clear 后 total/hits 归零"""
    agent._read_cache_clear()
    a = {"filename": "d.py", "offset": None, "limit": None}
    agent._read_cache_hit_or_record(a)
    agent._read_cache_hit_or_record(a)
    agent._read_cache_clear()
    total, hits, _ = agent._read_cache_stats()
    assert total == 0
    assert hits == 0


def test_stats_empty_filename_not_counted():
    """空 filename 调用不计入 total（不进 cache 也不统计）"""
    agent._read_cache_clear()
    empty = {"filename": "", "offset": None, "limit": None}
    agent._read_cache_hit_or_record(empty)
    agent._read_cache_hit_or_record(empty)
    total, hits, _ = agent._read_cache_stats()
    assert total == 0
    assert hits == 0


def test_stats_subthread_first_call_no_attribute_error():
    """子线程首次调用 _read_cache_hit_or_record 不抛 AttributeError，且计数独立于主线程

    覆盖 reviewer 找出的盲点：threading.local 隔离下，子线程从未调过 _read_cache_clear，
    state.total/hits 属性不存在 → getattr 兜底必须生效；并验证主/子线程计数互不污染。
    """
    import threading
    # 主线程：clear + 1 次 read
    agent._read_cache_clear()
    main_a = {"filename": "main.py", "offset": None, "limit": None}
    agent._read_cache_hit_or_record(main_a)  # 主线程 total=1, hits=0

    sub_results = {}
    sub_error = []

    def sub_thread():
        try:
            # 子线程从未调过 _read_cache_clear，total/hits 属性不存在
            sub_a = {"filename": "sub.py", "offset": None, "limit": None}
            agent._read_cache_hit_or_record(sub_a)  # miss → total=1
            agent._read_cache_hit_or_record(sub_a)  # hit  → total=2, hits=1
            total, hits, _ = agent._read_cache_stats()
            sub_results["total"] = total
            sub_results["hits"] = hits
        except Exception as e:
            sub_error.append(e)

    t = threading.Thread(target=sub_thread)
    t.start()
    t.join()

    assert not sub_error, f"子线程首次调用抛异常：{sub_error}"
    # 子线程独立计数
    assert sub_results["total"] == 2
    assert sub_results["hits"] == 1
    # 主线程计数不受子线程影响
    main_total, main_hits, _ = agent._read_cache_stats()
    assert main_total == 1
    assert main_hits == 0


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
