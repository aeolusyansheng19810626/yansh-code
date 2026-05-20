import sys
import os
import argparse
from rich.console import Console
import agent
from agent import (
    classify_input, chat, run, compress_history, show_context, clear_history, maybe_compress_history,
    get_latest_snapshot, restore_snapshot, cleanup_snapshot, show_recent_logs,
    detect_project_type, _PROJECT_TYPE, _PROJECT_TEST_CMD, _LOG_DIR
)
from config import load_project_config, get_config, override_config, set_workspace_dir, WORKSPACE_DIR, CLAUDE_OPUS, CLAUDE_SONNET, CLAUDE_HAIKU, GEMINI_3_FLASH, GEMINI_31_PRO
import interrupt
from pathlib import Path
import monitor
console = Console()


_SLASH_COMMANDS = [
    ("/mode",     "切换模式 [plan|code|auto]"),
    ("/model",    "切换 LLM 模型"),
    ("/compress", "压缩对话历史"),
    ("/context",  "显示上下文使用情况"),
    ("/clear",    "清空对话历史"),
    ("/revert",   "回滚最近一次任务的文件改动"),
    ("/log",      "显示最近任务日志"),
    ("/config",   "显示当前配置"),
    ("/rules",    "显示项目规则 (.agent_rules)"),
    ("/hil",      "切换 Human-in-Loop 模式"),
    ("/stats",    "Token 消耗统计"),
    ("/replay",   "加载失败案例回放"),
]


def _match_slash(text: str):
    """返回与 text 前缀匹配的命令列表，text 为 / 开头时才返回结果"""
    if not text.startswith("/"):
        return []
    return [(c, d) for c, d in _SLASH_COMMANDS if c.startswith(text)]


def _read_input(prompt_str="> "):
    """Windows: Shift+Enter 换行，Enter 提交。/ 命令实时补全。非 Windows 降级为 input()。"""
    if sys.platform != "win32":
        return input(prompt_str)

    import ctypes
    import ctypes.wintypes as wt

    kernel32 = ctypes.windll.kernel32

    # 开启 ANSI 转义序列
    stdout_h = kernel32.GetStdHandle(-11)
    mode = wt.DWORD()
    kernel32.GetConsoleMode(stdout_h, ctypes.byref(mode))
    kernel32.SetConsoleMode(stdout_h, mode.value | 0x0004)

    stdin_h = kernel32.GetStdHandle(-10)

    KEY_EVENT         = 0x0001
    VK_RETURN         = 0x0D
    VK_BACK           = 0x08
    VK_LEFT           = 0x25
    VK_RIGHT          = 0x27
    VK_DELETE         = 0x2E
    VK_HOME           = 0x24
    VK_END            = 0x23
    VK_TAB            = 0x09
    VK_ESCAPE         = 0x1B
    SHIFT_PRESSED     = 0x0010
    LEFT_CTRL_PRESSED = 0x0008
    RIGHT_CTRL_PRESSED= 0x0004

    class KEY_EVENT_RECORD(ctypes.Structure):
        _fields_ = [
            ("bKeyDown",          wt.BOOL),
            ("wRepeatCount",      wt.WORD),
            ("wVirtualKeyCode",   wt.WORD),
            ("wVirtualScanCode",  wt.WORD),
            ("uChar",             wt.WCHAR),
            ("dwControlKeyState", wt.DWORD),
        ]

    class EVENT_UNION(ctypes.Union):
        _fields_ = [("KeyEvent", KEY_EVENT_RECORD), ("padding", ctypes.c_byte * 16)]

    class INPUT_RECORD(ctypes.Structure):
        _fields_ = [("EventType", wt.WORD), ("Event", EVENT_UNION)]

    buf = []
    cursor = 0
    prev_lines = 1
    term_cursor_line = 0  # 终端光标当前在输入区第几行
    completion_enabled = True  # Esc 暂时关闭，下一次按 / 时自动重开

    try:
        from wcwidth import wcswidth as _wcswidth
        def _dispwidth(s): w = _wcswidth(s); return w if w >= 0 else len(s)
    except ImportError:
        try:
            from prompt_toolkit.utils import get_cwidth as _gcw
            def _dispwidth(s): return sum(_gcw(c) for c in s)
        except ImportError:
            def _dispwidth(s): return len(s)

    def redraw():
        nonlocal prev_lines, term_cursor_line
        text  = "".join(buf)
        lines = text.split("\n")

        # 候选区：仅在单行 / 命令时显示
        completions = []
        if completion_enabled and "\n" not in text:
            completions = _match_slash(text)

        total_lines = len(lines) + len(completions)

        # 精确移回输入区顶部
        if term_cursor_line > 0:
            sys.stdout.write(f"\033[{term_cursor_line}A")
        sys.stdout.write("\r")

        # 输入区
        for i, line in enumerate(lines):
            sys.stdout.write("\033[2K")
            sys.stdout.write(f"{prompt_str}{line}")
            if i < len(lines) - 1:
                sys.stdout.write("\n")

        # 候选区（紧跟输入区下方，无 prompt 前缀）
        for cmd, desc in completions:
            sys.stdout.write("\n\033[2K")
            sys.stdout.write(f"  \033[36m{cmd}\033[0m  \033[2m{desc}\033[0m")

        # 清除行数减少时残留的旧行
        extra = prev_lines - total_lines
        for _ in range(extra):
            sys.stdout.write("\n\033[2K")
        if extra > 0:
            sys.stdout.write(f"\033[{extra}A")

        prev_lines = total_lines

        # 把光标移回输入区光标位置
        before       = "".join(buf[:cursor])
        before_lines = before.split("\n")
        cur_line     = len(before_lines) - 1
        cur_col      = _dispwidth(before_lines[-1])
        end_line     = total_lines - 1

        if end_line > cur_line:
            sys.stdout.write(f"\033[{end_line - cur_line}A")
        col = _dispwidth(prompt_str) + cur_col
        sys.stdout.write(f"\r\033[{col}C" if col > 0 else "\r")
        term_cursor_line = cur_line
        sys.stdout.flush()

    sys.stdout.write(prompt_str)
    sys.stdout.flush()

    while True:
        rec = INPUT_RECORD()
        n   = wt.DWORD(0)
        kernel32.ReadConsoleInputW(stdin_h, ctypes.byref(rec), 1, ctypes.byref(n))

        if rec.EventType != KEY_EVENT:
            continue
        key = rec.Event.KeyEvent
        if not key.bKeyDown:
            continue

        vk    = key.wVirtualKeyCode
        state = key.dwControlKeyState
        shift = bool(state & SHIFT_PRESSED)
        ctrl  = bool(state & (LEFT_CTRL_PRESSED | RIGHT_CTRL_PRESSED))
        ch    = key.uChar

        if vk == VK_RETURN:
            if shift:
                buf.insert(cursor, "\n"); cursor += 1; redraw()
            else:
                # 检测是否为粘贴场景：粘贴时后面还有待处理的键盘事件
                peeked = (INPUT_RECORD * 1)()
                n_peeked = wt.DWORD(0)
                kernel32.PeekConsoleInputW(stdin_h, ctypes.byref(peeked), 1, ctypes.byref(n_peeked))
                if n_peeked.value > 0:
                    buf.insert(cursor, "\n"); cursor += 1; redraw()
                else:
                    sys.stdout.write("\n"); sys.stdout.flush()
                    return "".join(buf)
        elif vk == VK_BACK:
            if cursor > 0:
                buf.pop(cursor - 1); cursor -= 1; redraw()
        elif vk == VK_DELETE:
            if cursor < len(buf):
                buf.pop(cursor); redraw()
        elif vk == VK_LEFT:
            if cursor > 0:
                cursor -= 1; redraw()
        elif vk == VK_RIGHT:
            if cursor < len(buf):
                cursor += 1; redraw()
        elif vk == VK_HOME:
            before = "".join(buf[:cursor])
            cursor = before.rfind("\n") + 1; redraw()
        elif vk == VK_END:
            after = "".join(buf[cursor:])
            nl = after.find("\n")
            cursor = cursor + nl if nl != -1 else len(buf); redraw()
        elif ctrl and vk == 0x43:
            raise KeyboardInterrupt
        elif ctrl and vk == 0x44:
            raise EOFError
        elif vk == VK_TAB:
            # Tab 补全：仅当有候选时替换 buf 为首个完整命令
            text = "".join(buf)
            matches = _match_slash(text) if completion_enabled and "\n" not in text else []
            if matches:
                cmd = matches[0][0]
                buf[:] = list(cmd)
                cursor = len(buf)
                redraw()
            # 没匹配时静默忽略 Tab（不插入实体 Tab 字符）
        elif vk == VK_ESCAPE:
            # 隐藏候选区；按 / 时自动重开
            if completion_enabled:
                completion_enabled = False
                redraw()
        elif ch and ord(ch) >= 32:
            if ch == "/":
                completion_enabled = True  # 按 / 重开补全
            buf.insert(cursor, ch); cursor += 1; redraw()

VALID_MODES = {"plan", "code", "auto"}

def show_config():
    cfg = get_config()
    console.print("\n[当前配置]", highlight=False)
    for k, v in cfg.items():
        console.print(f"  {k}: {v}", highlight=False)
    console.print()

def handle_task_result(result):
    if result["success"]:
        console.print("\n[bold green]任务完成！[/bold green]")
    else:
        console.print("\n[bold red]任务失败[/bold red]")

def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="yansh-code: 极简代码智能体")
    parser.add_argument("requirement", nargs="?", help="任务需求说明")
    parser.add_argument("--mode", choices=VALID_MODES, help="运行模式")
    parser.add_argument("--model", help="指定 LLM 模型")
    parser.add_argument("--json", action="store_true", help="以 JSON 格式输出最后结果 (batch 模式)")
    parser.add_argument("--strict", action="store_true", help="批处理模式下拒绝需确认命令（pip/npm install、git checkout/reset）")
    parser.add_argument("--cwd", metavar="DIR", help="指定工作目录（workspace），默认为 ./workspace")
    args = parser.parse_args()

    # --cwd：在一切初始化之前设置 workspace 路径
    if args.cwd:
        import config as _config_mod
        import tools as _tools_mod2
        import agent as _agent_mod2
        _config_mod.set_workspace_dir(args.cwd)
        _tools_mod2._reinit_paths()
        _agent_mod2._reinit_paths()

    # 加载配置
    load_project_config()
    override_config(model=args.model)
    if args.model:
        import llm_client as _llm_mod
        _cascade = [args.model] if args.model == CLAUDE_HAIKU else [args.model, CLAUDE_HAIKU]
        _llm_mod.set_quality_cascade(_cascade)
    
    # 检测项目类型
    global _PROJECT_TYPE, _PROJECT_TEST_CMD
    ptype, tcmd = detect_project_type()
    import agent as _agent_mod
    _agent_mod._PROJECT_TYPE = ptype
    _agent_mod._PROJECT_TEST_CMD = tcmd

    current_mode = get_config()["mode"]
    if args.mode:
        current_mode = args.mode
    
    # 批处理模式处理
    if args.requirement or args.json:
        agent.set_batch_mode(True, json_output=args.json, strict=args.strict)
        if args.requirement:
            res = agent.run(args.requirement, mode=current_mode)
            if args.json:
                import json
                log = agent.get_last_task_log()
                sys.stdout.reconfigure(encoding="utf-8", errors="replace")
                print(json.dumps(log, ensure_ascii=False), file=sys.stdout)
            else:
                handle_task_result(res)
        return

    # 交互模式
    console.print("[bold cyan]yansh-code 已启动[/bold cyan]")
    console.print(f"模式: {current_mode} | 模型: {get_config()['model']}")
    console.print("输入任务需求，或输入 /history, /clear, /compress, /rollback, /stats, /log, /config, /rules, /mode 获取帮助。")
    console.print("图片: @image <路径/URL>（分析截图/设计稿）  @paste（粘贴剪贴板截图）  @image design.png → 按设计稿生成代码", highlight=False)
    if _PROJECT_TYPE:
        console.print(f"检测到项目：[bold]{_PROJECT_TYPE}[/bold]")

    interrupt.start_listener()

    while True:
        try:
            user_input = _read_input('> ').strip()
        except (EOFError, KeyboardInterrupt):
            console.print("再见！")
            break

        if not user_input:
            continue

        if user_input.lower() in ["exit", "quit", "/exit", "/quit"]:
            console.print("再见！")
            break

        if user_input == "/mode" or user_input.startswith("/mode "):
            parts = user_input.split()
            if len(parts) == 2 and parts[1] in VALID_MODES:
                current_mode = parts[1]
                console.print(f"已切换到 {current_mode} 模式")
            else:
                console.print(f"用法：/mode [plan|code|auto]")
            continue

        if user_input == "/model":
            MODEL_MENU = [
                ("1", "DeepSeek V4 Flash",  "deepseek/deepseek-v4-flash"),
                ("2", "Claude Opus 4.7",    CLAUDE_OPUS),
                ("3", "Claude Sonnet 4.6",  CLAUDE_SONNET),
                ("4", "Claude Haiku 4.5",   CLAUDE_HAIKU),
                ("5", "Gemini 2.5 Flash",    GEMINI_3_FLASH),
                ("6", "Gemini 2.5 Pro",     GEMINI_31_PRO),
            ]
            import agent as _am
            current_code_model = get_config()["model"]
            current_review_model = _am.REVIEW_MODEL or "[跟随写代码模型]"
            console.print("\n[bold]选择要配置的模型：[/bold]", highlight=False)
            console.print(f"  1. 写代码模型（当前: {current_code_model}）", highlight=False)
            console.print(f"  2. Review 模型（当前: {current_review_model}）", highlight=False)
            try:
                target = _read_input("请输入数字 (1-2): ").strip()
            except (EOFError, KeyboardInterrupt):
                target = ""
            if target not in ("1", "2"):
                console.print("无效输入，已取消。", highlight=False)
                continue
            console.print("\n[bold]选择模型：[/bold]", highlight=False)
            for num, label, mid in MODEL_MENU:
                console.print(f"  {num}. {label}  ({mid})", highlight=False)
            try:
                choice = _read_input("请输入数字 (1-6): ").strip()
            except (EOFError, KeyboardInterrupt):
                choice = ""
            matched = next((m for n, _, m in MODEL_MENU if n == choice), None)
            if not matched:
                console.print("无效输入，未切换模型。", highlight=False)
                continue
            label = next(l for n, l, m in MODEL_MENU if m == matched)
            if target == "1":
                override_config(model=matched)
                from config import CLAUDE_HAIKU as _haiku
                import llm_client as _llm_mod
                _cascade = [matched] if matched == _haiku else [matched, _haiku]
                _llm_mod.set_quality_cascade(_cascade)
                console.print(f"写代码模型已切换到：{label}  ({matched})", highlight=False)
            else:
                _am.REVIEW_MODEL = matched
                console.print(f"Review 模型已切换到：{label}  ({matched})", highlight=False)
            continue

        if user_input == "/compress":
            compress_history()
            continue

        if user_input == "/context":
            show_context()
            continue

        if user_input == "/clear":
            clear_history()
            continue

        if user_input == "/revert":
            snap = get_latest_snapshot()
            if snap is None:
                console.print("没有可用的快照", highlight=False)
            else:
                n = restore_snapshot(snap)
                console.print(f"[已回滚] 恢复 {n} 个文件", highlight=False)
                cleanup_snapshot(snap)
            continue

        if user_input == "/log":
            show_recent_logs()
            continue

        if user_input == "/config":
            show_config()
            continue
            
        if user_input == "/rules":
            rules_path = Path(WORKSPACE_DIR) / ".agent_rules"
            if rules_path.exists():
                console.print(f"当前生效规则 ({rules_path})：\n{rules_path.read_text(encoding='utf-8')}", highlight=False)
            else:
                console.print("未发现 .agent_rules 文件", highlight=False)
            continue
            
        if user_input.startswith("/hil"):
            parts = user_input.split()
            if len(parts) == 2 and parts[1] in ("on", "off"):
                override_config(human_in_loop=(parts[1] == "on"))
                console.print(f"HIL 已{'开启' if parts[1] == 'on' else '关闭'}", highlight=False)
            else:
                status = "开启" if get_config().get("human_in_loop") else "关闭"
                console.print(f"HIL 当前状态: {status}  用法: /hil [on|off]", highlight=False)
            continue

        if user_input == "/stats":
            agent.show_stats()
            continue
            
        if user_input.startswith("/replay"):
            parts = user_input.split()
            if len(parts) > 1:
                if parts[1] == "list":
                    agent.list_replays()
                elif parts[1] == "load" and len(parts) > 2:
                    agent.load_replay(parts[2])
                else:
                    console.print("用法：/replay [list|load <id>]")
            else:
                console.print("用法：/replay [list|load <id>]")
            continue

        input_type = classify_input(user_input)
        maybe_compress_history()

        if input_type == "task":
            console.print("正在处理新任务...")
            agent.run(user_input, mode=current_mode)
            agent.show_stats()
        else:
            reply = chat(user_input)
            console.print(f"\n[bold green]Assistant:[/bold green]\n{reply}\n")
            agent.show_stats()

if __name__ == "__main__":
    main()
