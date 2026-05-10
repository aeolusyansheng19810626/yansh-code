import subprocess, sys

if __name__ == "__main__":
    print("=" * 60)
    print("▶ 单元测试")
    print("=" * 60)
    r1 = subprocess.run([sys.executable, "tests/run_unit.py"])

    print("\n" + "=" * 60)
    print("▶ 集成测试")
    print("=" * 60)
    r2 = subprocess.run([sys.executable, "tests/run_integration.py"])
    
    sys.exit(r1.returncode or r2.returncode)
