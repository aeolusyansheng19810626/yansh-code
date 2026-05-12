import subprocess, sys, os

files = [
    "tests/unit/test_tools.py",
    "tests/unit/test_security.py",
    "tests/unit/test_symbol_lookup.py",
    "tests/unit/test_context_cmds.py",
    "tests/unit/test_detect_test_cmd.py",
    "tests/unit/test_cwd.py",
    "tests/unit/test_git_snapshot.py",
]

if __name__ == "__main__":
    failed = 0
    for f in files:
        print(f"\n{'='*60}")
        print(f"运行: {f}")
        print(f"{'='*60}")
        r = subprocess.run([sys.executable, f])
        if r.returncode != 0:
            failed += 1
    print(f"\n{'='*60}")
    print(f"单元测试完成：{len(files)-failed}/{len(files)} 文件通过")
    print(f"{'='*60}")
    sys.exit(failed)
