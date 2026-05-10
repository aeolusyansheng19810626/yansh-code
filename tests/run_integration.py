import subprocess, sys, re, os

files = [
    "tests/integration/test_1_9.py",
    "tests/integration/test_10_12.py",
    "tests/integration/test_13_16.py",
    "tests/integration/test_17_19.py",
    "tests/integration/test_20_23.py",
    "tests/integration/test_24_25.py",
    "tests/integration/test_26_27.py",
    "tests/integration/test_28_31.py",
    "tests/integration/test_32_35.py",
]

if __name__ == "__main__":
    all_results = []  # (name, ok, reason)
    failed_files = 0
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}

    for f in files:
        print(f"\n{'='*60}")
        print(f"运行: {f}")
        print(f"{'='*60}", flush=True)

        lines = []
        proc = subprocess.Popen(
            [sys.executable, "-u", f],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", env=env,
        )
        for line in proc.stdout:
            print(line, end="", flush=True)
            lines.append(line)
        proc.wait()
        if proc.returncode != 0:
            failed_files += 1

        for line in lines:
            m = re.match(r'\[PASS\]\s+(.+)', line)
            if m:
                all_results.append((m.group(1).strip(), True, ""))
                continue
            m = re.match(r'\[FAIL(?:: *(.*?))?\]\s+(.+)', line)
            if m:
                all_results.append((m.group(2).strip(), False, m.group(1) or ""))

    pass_count = sum(1 for _, ok, _ in all_results if ok)
    total = len(all_results)
    pct = 100 * pass_count // total if total else 0

    print(f"\n{'='*60}")
    print(f"集成测试完成：{pass_count}/{total} 个场景通过 ({pct}%)")
    print(f"{'='*60}")
    for name, ok, reason in all_results:
        if ok:
            print(f"  ✓ {name}")
        else:
            print(f"  ✗ {name}  →  {reason}")
    print()
    sys.exit(failed_files)
