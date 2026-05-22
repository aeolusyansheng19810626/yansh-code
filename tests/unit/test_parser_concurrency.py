"""P1-3 tree-sitter Parser 并发安全单元测试。

Codex review 找出：tools._TS_PARSER 模块级单例 + 跨线程共用 parser.parse，
tree-sitter Python binding 的 Parser 不是 thread-safe。并发 subagent 启动
都会跑 workspace_symbols → 撞 parser。

修复：parser.parse 调用前用 _TS_PARSER_LOCK 串行。
本测试验证 N 个线程并发 parse 不同 .py 文件不崩、不脏数据。
"""
import os
import sys
import threading
import random

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import tools


def test_concurrent_parse_no_crash(tmp_path, monkeypatch):
    """N 个线程并发 list_symbols 不同 .py 文件——不应崩、结果正确"""
    monkeypatch.setattr(tools, "_WORKSPACE_ROOT", tmp_path.resolve())
    monkeypatch.setattr("config.WORKSPACE_DIR", str(tmp_path))

    # 准备 10 个不同的 .py 文件，每个有可识别的符号
    N_FILES = 10
    expected = {}
    for i in range(N_FILES):
        fname = f"mod_{i}.py"
        src = "\n".join([
            f"def func_{i}_a(x): return x + {i}",
            f"def func_{i}_b(): pass",
            f"class Cls_{i}: pass",
        ])
        (tmp_path / fname).write_text(src, encoding="utf-8")
        expected[fname] = {f"func_{i}_a", f"func_{i}_b", f"Cls_{i}"}

    results = {}
    errors = []
    results_lock = threading.Lock()

    def worker(fname):
        # 加点抖动让线程更可能交错
        import time
        time.sleep(random.uniform(0, 0.01))
        try:
            r = tools.list_symbols(fname)
            with results_lock:
                if "error" in r:
                    errors.append((fname, r["error"]))
                else:
                    syms = {s["name"] for s in r["symbols"]}
                    results[fname] = syms
        except Exception as e:
            with results_lock:
                errors.append((fname, str(e)))

    # 每个文件让 5 个线程并发 parse → 总 50 个线程 / 10 个文件
    threads = []
    for fname in expected:
        for _ in range(5):
            t = threading.Thread(target=worker, args=(fname,), daemon=True)
            threads.append(t)
            t.start()
    for t in threads:
        t.join(timeout=10)

    assert all(not t.is_alive() for t in threads), "有线程没在 10s 内完成"
    assert not errors, f"并发 parse 报错：{errors[:3]}"

    # 每个文件的结果都对（symbols 集合匹配）
    for fname, exp_syms in expected.items():
        assert fname in results, f"{fname} 没结果"
        assert results[fname] == exp_syms, \
            f"{fname} 符号不匹配：期望 {exp_syms}，实际 {results[fname]}"


def test_concurrent_parse_same_file_consistent(tmp_path, monkeypatch):
    """同一文件多线程并发 parse → 结果应一致（不脏 cache）"""
    monkeypatch.setattr(tools, "_WORKSPACE_ROOT", tmp_path.resolve())
    monkeypatch.setattr("config.WORKSPACE_DIR", str(tmp_path))

    fname = "shared.py"
    (tmp_path / fname).write_text(
        "def alpha(): pass\ndef beta(): pass\nclass Gamma: pass\n",
        encoding="utf-8",
    )

    all_results = []
    lock = threading.Lock()

    def worker():
        for _ in range(10):
            r = tools.list_symbols(fname)
            with lock:
                if "error" in r:
                    all_results.append(("err", r["error"]))
                else:
                    syms = tuple(sorted(s["name"] for s in r["symbols"]))
                    all_results.append(("ok", syms))

    N_THREADS = 8
    threads = [threading.Thread(target=worker, daemon=True) for _ in range(N_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert all(not t.is_alive() for t in threads)
    # 全部 ok，结果都是同一个 tuple
    statuses = {r[0] for r in all_results}
    assert statuses == {"ok"}, f"有错误：{[r for r in all_results if r[0] == 'err'][:3]}"
    sym_sets = {r[1] for r in all_results}
    assert len(sym_sets) == 1, f"结果不一致（cache race？）：{sym_sets}"
    assert sym_sets == {("Gamma", "alpha", "beta")}


def test_lock_present_in_module():
    """sanity check：_TS_PARSER_LOCK 存在且是 Lock"""
    assert hasattr(tools, "_TS_PARSER_LOCK")
    assert isinstance(tools._TS_PARSER_LOCK, type(threading.Lock()))


def test_ts_parse_locked_helper_present():
    """_ts_parse_locked helper 存在——三处 parser.parse 必须走它"""
    assert hasattr(tools, "_ts_parse_locked")
    assert callable(tools._ts_parse_locked)


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
