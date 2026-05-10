import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from pathlib import Path
import agent
import tools
from agent import (
    set_batch_mode, create_snapshot, restore_snapshot, cleanup_snapshot
)
from tools import write_file, apply_patch, list_files, search_in_files, append_to_file
from config import load_project_config

ROOT = Path(__file__).parent.parent.parent
WORKSPACE = ROOT / "workspace"
PASS_COUNT = 0
FAIL_COUNT = 0
RESULTS = []

def report(name: str, ok: bool, reason: str = ""):
    global PASS_COUNT, FAIL_COUNT
    if ok:
        print(f"[PASS] {name}")
        PASS_COUNT += 1
    else:
        print(f"[FAIL: {reason}] {name}")
        FAIL_COUNT += 1
    RESULTS.append((name, ok, reason))

def cleanup_workspace():
    for f in WORKSPACE.glob("*.py"):
        f.unlink()

load_project_config()
set_batch_mode(True, json_output=False)

def test_scene_20_patch_val():
    print("\n=== 场景20: apply_patch行号校验 ===")
    cleanup_workspace()
    write_file("test_patch.py", "line1\nline2\nline3\nline4\nline5\n")
    try:
        results = []
        p_a = "--- a/test_patch.py\n+++ b/test_patch.py\n@@ -0,1 +0,1 @@\n-line1\n+new content\n"
        res_a = apply_patch(p_a); results.append("error" in res_a and "1" in res_a["error"])
        p_b = "--- a/test_patch.py\n+++ b/test_patch.py\n@@ -10,1 +10,1 @@\n-line10\n+new content\n"
        res_b = apply_patch(p_b); results.append("error" in res_b and "总行数" in res_b["error"])
        p_c = "--- a/test_patch.py\n+++ b/test_patch.py\n@@ -4,2 +4,2 @@\n-line4\n-line5\n+new content\n"
        res_c = apply_patch(p_c); results.append("error" in res_c and "start" in res_c["error"])
        ok = all(results); report("场景20-apply_patch行号校验", ok)
        return ok
    except Exception as e:
        report("场景20-apply_patch行号校验", False, str(e)[:120])
        return False

def test_scene_21_snap_del():
    print("\n=== 场景21: restore_snapshot删除新建文件 ===")
    cleanup_workspace()
    old_f = WORKSPACE / "old.py"; write_file("old.py", "# old")
    try:
        snap = create_snapshot(["old.py"])
        new_f = WORKSPACE / "new.py"; write_file("new.py", "# new")
        restore_snapshot(snap); cleanup_snapshot(snap)
        ok = not new_f.exists() and old_f.exists()
        report("场景21-restore_snapshot删除新建文件", ok)
        return ok
    except Exception as e:
        report("场景21-restore_snapshot删除新建文件", False, str(e)[:120])
        return False

def test_scene_22_gitignore():
    print("\n=== 场景22: .gitignore过滤 ===")
    cleanup_workspace()
    try:
        write_file(".gitignore", "*.log\nsecret.txt")
        write_file("app.log", "log"); write_file("secret.txt", "secret"); write_file("main.py", "print")
        files = list_files().get("files", [])
        list_ok = "main.py" in files and "app.log" not in files and "secret.txt" not in files
        res = search_in_files("secret")
        search_ok = all("secret.txt" not in m["file"] for m in res.get("matches", []))
        ok = list_ok and search_ok
        report("场景22-.gitignore过滤", ok)
        return ok
    except Exception as e:
        report("场景22-.gitignore过滤", False, str(e)[:120])
        return False

def test_scene_23_append():
    print("\n=== 场景23: append_to_file ===")
    cleanup_workspace()
    try:
        write_file("append_test.txt", "l1\nl2\n")
        append_to_file("append_test.txt", "l3\nl4")
        content = (WORKSPACE / "append_test.txt").read_text(encoding="utf-8")
        ok = "l1\nl2\nl3\nl4" in content
        res_err = append_to_file("../outside.txt", "hack")
        ok = ok and "error" in res_err
        report("场景23-append_to_file", ok)
        return ok
    except Exception as e:
        report("场景23-append_to_file", False, str(e)[:120])
        return False

if __name__ == "__main__":
    results = [
        test_scene_20_patch_val(),
        test_scene_21_snap_del(),
        test_scene_22_gitignore(),
        test_scene_23_append(),
    ]
    passed = sum(results)
    total = len(results)
    print(f"\n{'='*60}")
    print(f"测试结果：{passed}/{total} 通过")
    print(f"{'='*60}")
