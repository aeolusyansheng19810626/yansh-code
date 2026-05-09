"""#29 get_symbol_definition 测试"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import tools
from pathlib import Path

# 在 workspace 里创建测试 fixture 文件
_FIXTURE = "test_symbol_fixture.py"
_FIXTURE_SRC = '''\
def standalone_func(x, y):
    """普通函数"""
    return x + y


class MyClass:
    def method_one(self):
        return 1

    @staticmethod
    def static_method():
        return 42


def another_func():
    pass


class ChildClass(MyClass):
    def method_one(self):
        return super().method_one() + 1
'''

def setup_module(module):
    tools.write_file(_FIXTURE, _FIXTURE_SRC)

def teardown_module(module):
    tools.delete_file(_FIXTURE)

# ---------- 基础查找 ----------

def test_find_top_level_func():
    r = tools.get_symbol_definition("standalone_func", _FIXTURE)
    assert "matches" in r, f"应找到: {r}"
    assert r["total"] == 1
    m = r["matches"][0]
    assert m["line"] == 1
    assert "def standalone_func" in m["code"]
    print(f"[PASS] standalone_func @ line {m['line']}")

def test_find_class():
    r = tools.get_symbol_definition("MyClass", _FIXTURE)
    assert "matches" in r
    assert r["total"] == 1
    m = r["matches"][0]
    assert "class MyClass" in m["code"]
    print(f"[PASS] MyClass @ line {m['line']}")

def test_find_method():
    r = tools.get_symbol_definition("method_one", _FIXTURE)
    assert "matches" in r
    # MyClass.method_one + ChildClass.method_one
    assert r["total"] == 2
    print(f"[PASS] method_one 找到 {r['total']} 处")

def test_find_static_method():
    r = tools.get_symbol_definition("static_method", _FIXTURE)
    assert "matches" in r
    assert r["total"] == 1
    m = r["matches"][0]
    assert "def static_method" in m["code"]
    print(f"[PASS] static_method（含装饰器）@ line {m['line']}")

def test_find_another_func():
    r = tools.get_symbol_definition("another_func", _FIXTURE)
    assert "matches" in r
    assert r["total"] == 1
    print(f"[PASS] another_func @ line {r['matches'][0]['line']}")

# ---------- 全 workspace 搜索 ----------

def test_search_workspace():
    r = tools.get_symbol_definition("standalone_func")
    assert "matches" in r
    assert r["total"] >= 1
    files = [m["file"] for m in r["matches"]]
    assert any(_FIXTURE.replace("\\", "/") in f for f in files)
    print(f"[PASS] workspace 搜索: {r['total']} 处")

# ---------- 未找到 ----------

def test_not_found():
    r = tools.get_symbol_definition("nonexistent_xyz", _FIXTURE)
    assert "error" in r
    print(f"[PASS] 未找到时返回 error: {r['error']}")

# ---------- 路径越界 ----------

def test_path_traversal():
    r = tools.get_symbol_definition("foo", "../secret.py")
    assert "error" in r
    print(f"[PASS] 路径越界被拦截: {r['error']}")


if __name__ == "__main__":
    tests = [
        test_find_top_level_func,
        test_find_class,
        test_find_method,
        test_find_static_method,
        test_find_another_func,
        test_search_workspace,
        test_not_found,
        test_path_traversal,
    ]
    setup()
    failed = 0
    try:
        for t in tests:
            try:
                t()
            except Exception as e:
                print(f"[FAIL] {t.__name__}: {e}")
                failed += 1
    finally:
        teardown()
    print(f"\n{'全部通过' if not failed else f'{failed} 项失败'} ({len(tests)-failed}/{len(tests)})")
