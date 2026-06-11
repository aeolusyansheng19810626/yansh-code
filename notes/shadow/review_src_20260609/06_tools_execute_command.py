# tools.py: execute_command
# 行号 349-460
def execute_command(command, _timeout_sec=30):
    """在workspace目录下执行命令，30秒超时，三级命令策略（deny/safe/confirm）"""
    # Level 1: deny
    danger = _check_dangerous(command)
    if danger:
        return danger

    cmd_stripped = command.strip()

    # Level 2: safe — 直接执行
    is_safe = any(re.match(p, cmd_stripped, re.IGNORECASE) for p in _SAFE_PATTERNS)

    # Level 3: confirm — 默认所有非 _SAFE_PATTERNS 命令都需要用户确认。
    # 命中 _CONFIRM_PATTERNS 的额外提供识别标签（如 "pip install"）；未命中则归为 "未识别命令"。
    # batch + strict：未识别命令直接拒；batch 非 strict：自动确认（保留向后兼容）。
    if not is_safe:
        label = next(
            (lbl for pat, lbl in _CONFIRM_PATTERNS
             if re.search(pat, cmd_stripped, re.IGNORECASE)),
            "未识别命令",
        )
        if _BATCH_MODE and _BATCH_STRICT:
            _con().print(f"[batch-strict] 拒绝执行: {label} -> {command}", highlight=False)
            return _err("security", f"批处理严格模式拒绝执行: {label}", returncode=-1, stdout="", stderr="")
        if _BATCH_MODE:
            _con().print(f"[batch] 自动确认执行 ({label}): {command}", highlight=False)
        else:
            _c = _con()
            _c.print(f"[确认] 即将执行 ({label}): {command}", highlight=False)
            try:
                answer = _c.input("继续？(y/n) ").strip().lower()
            except EOFError:
                answer = "n"
            if answer != "y":
                return _err("security", "用户取消执行", returncode=-1, stdout="", stderr="")

    import threading
    # P1 #6：opt-in 沙箱——若启用，把命令包成 docker run 形态；默认禁用，行为不变
    try:
        import sandbox as _sandbox
        run_command = _sandbox.wrap_command(command, _get_workspace())
    except Exception:
        run_command = command

    try:
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        process = subprocess.Popen(
            run_command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=_get_workspace(),
            env=env,
        )

        stdout_lines = []
        stderr_lines = []

        def _read_stdout():
            for line in process.stdout:
                # batch 模式下 stdout 保留给 --json 输出，实时打印走 stderr
                print(line, end='', flush=True, file=sys.stderr if _BATCH_MODE else sys.stdout)
                stdout_lines.append(line)

        def _read_stderr():
            for line in process.stderr:
                stderr_lines.append(line)

        t_out = threading.Thread(target=_read_stdout, daemon=True)
        t_err = threading.Thread(target=_read_stderr, daemon=True)
        t_out.start()
        t_err.start()

        import interrupt
        import time
        start_time = time.time()
        try:
            while True:
                if interrupt.is_interrupted():
                    process.terminate()
                    process.wait(timeout=1)
                    raise interrupt.Interrupted()
                
                try:
                    process.wait(timeout=0.1)
                    break # Finished
                except subprocess.TimeoutExpired:
                    if time.time() - start_time > _timeout_sec:
                        process.kill()
                        t_out.join(timeout=2)
                        t_err.join(timeout=2)
                        return _err("timeout", f"命令执行超时（{_timeout_sec}秒）",
                                    "execute_command",
                                    stdout=''.join(stdout_lines),
                                    stderr=''.join(stderr_lines),
                                    returncode=-1)
        except interrupt.Interrupted:
            raise
        except Exception as e:
            return _err("internal", str(e))

        t_out.join()
        t_err.join()

        _update_agent_state(command, process.returncode)
        return {
            "stdout": _truncate_cmd_output(''.join(stdout_lines)),
            "stderr": _truncate_cmd_output(''.join(stderr_lines)),
            "returncode": process.returncode
