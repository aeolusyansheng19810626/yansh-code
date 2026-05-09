import sys
import json
import argparse
import agent
from rich.console import Console
from agent import (
    run, classify_input, chat, add_to_history, maybe_compress_history,
    compress_history, show_context, clear_history, detect_project_type,
    load_history, get_latest_snapshot, restore_snapshot, cleanup_snapshot,
    show_recent_logs, set_batch_mode, get_last_task_log,
)
from config import load_project_config, get_config, override_config
import interrupt

console = Console()

VALID_MODES = {"plan", "code", "auto"}


def handle_task_result(result):
    if result["success"]:
        console.print("任务完成")
    else:
        console.print("任务失败")
        test_result = result["test_result"]
        if test_result.get("stdout"):
            console.print("测试输出：")
            console.print(test_result["stdout"])
        if test_result.get("stderr"):
            console.print("错误信息：")
            console.print(test_result["stderr"])


def show_config():
    """#43 打印当前生效配置"""
    cfg = get_config()
    console.print("[当前生效配置]", highlight=False)
    for k, v in cfg.items():
        console.print(f"  {k}: {v}", highlight=False)


def main():
    # ---------- #40 argparse ----------
    parser = argparse.ArgumentParser(prog="yansh-code", add_help=True)
    parser.add_argument("--task", type=str, default=None, help="直接执行任务（批处理模式）")
    parser.add_argument("--mode", type=str, default=None, choices=list(VALID_MODES), help="运行模式")
    parser.add_argument("--json", action="store_true", dest="json_output", help="JSON 输出到 stdout，其余输出到 stderr")
    args, unknown = parser.parse_known_args()

    # 如果没有 --task 但有位置参数，兼容旧用法（sys.argv[1:]）
    positional_task = " ".join(unknown).strip() if unknown else None

    # ---------- #43 加载项目配置 ----------
    load_project_config()

    # CLI 参数优先级高于配置文件
    if args.mode:
        override_config(mode=args.mode)

    # ---------- #40 批处理模式初始化 ----------
    batch_task = args.task or positional_task
    if batch_task:
        set_batch_mode(True, json_output=args.json_output)
        current_mode = get_config().get("mode", "auto")
        if args.mode:
            current_mode = args.mode

        result = run(batch_task, mode=current_mode)
        log = get_last_task_log()

        if args.json_output:
            output = {
                "success": result["success"],
                "requirement": batch_task,
                "plan": log.get("plan", []),
                "files_modified": log.get("files_modified", []),
                "test_result": log.get("test_result", "unknown"),
                "attempts": log.get("attempts", 0),
                "duration_seconds": log.get("duration_seconds", 0.0),
                "error": log.get("error", None),
            }
            print(json.dumps(output, ensure_ascii=False), flush=True)
        else:
            handle_task_result(result)
        return

    # ---------- 交互模式 ----------
    console.print("yansh-code CLI")

    # 加载历史会话
    restored = load_history()
    if restored:
        console.print(f"[已恢复会话] 共 {restored} 轮历史", highlight=False)

    # 项目类型检测
    proj_type, proj_cmd = detect_project_type()
    if proj_type:
        agent._PROJECT_TYPE = proj_type
        agent._PROJECT_TEST_CMD = proj_cmd
        console.print(f"[项目类型] {proj_type} | 测试命令：{proj_cmd}", highlight=False)

    current_mode = get_config().get("mode", "auto")
    if args.mode:
        current_mode = args.mode
    interrupt.start_listener()

    # 主循环
    while True:
        user_input = console.input("\n> ").strip()

        if not user_input:
            continue

        if user_input.lower() in ["exit", "quit"]:
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

        input_type = classify_input(user_input)
        maybe_compress_history()

        if input_type == "task":
            console.print("正在处理新任务...")
            interrupt.reset()
            handle_task_result(run(user_input, mode=current_mode))
        else:
            response = chat(user_input)
            console.print(response)


if __name__ == "__main__":
    main()
