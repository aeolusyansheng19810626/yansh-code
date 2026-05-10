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
from config import load_project_config, get_config, override_config, WORKSPACE_DIR
import interrupt
from pathlib import Path
import monitor
from prompt_toolkit import prompt
from prompt_toolkit.key_binding import KeyBindings

console = Console()

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
    args = parser.parse_args()

    # 加载配置
    load_project_config()
    override_config(model=args.model)
    
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
        agent.set_batch_mode(True, json_output=args.json)
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

    # 主循环：multiline=True，Enter 换行，Meta+Enter（Alt+Enter）提交
    kb = KeyBindings()
    @kb.add('c-enter')
    def _submit(event):
        event.current_buffer.validate_and_handle()

    while True:
        try:
            user_input = prompt('> ', key_bindings=kb, multiline=True).strip()
        except (EOFError, KeyboardInterrupt):
            console.print("再见！")
            break

        if not user_input:
            continue

        if user_input.lower() in ["exit", "quit", "/exit", "/quit"]:
            console.print("再见！")
            break

        if user_input.startswith("/mode"):
            parts = user_input.split()
            if len(parts) == 2 and parts[1] in VALID_MODES:
                current_mode = parts[1]
                console.print(f"已切换到 {current_mode} 模式")
            else:
                console.print(f"用法：/mode [plan|code|auto]")
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
