import sys
import agent
from rich.console import Console
from agent import run, classify_input, chat, add_to_history, maybe_compress_history, compress_history, show_context, clear_history, detect_project_type, load_history
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

def main():
    console.print("yansh-code CLI")

    # #28 加载历史会话
    restored = load_history()
    if restored:
        console.print(f"[已恢复会话] 共 {restored} 轮历史", highlight=False)

    # #27 项目类型检测
    proj_type, proj_cmd = detect_project_type()
    if proj_type:
        agent._PROJECT_TYPE = proj_type
        agent._PROJECT_TEST_CMD = proj_cmd
        console.print(f"[项目类型] {proj_type} | 测试命令：{proj_cmd}", highlight=False)

    current_mode = "auto"
    interrupt.start_listener()

    # 若命令行带了参数，直接处理第一条需求
    if len(sys.argv) > 1:
        first_input = " ".join(sys.argv[1:]).strip()
        if first_input:
            interrupt.reset()
            console.print("正在处理需求...")
            handle_task_result(run(first_input, mode=current_mode))

    # 主循环
    while True:
        user_input = console.input("\n> ").strip()

        if not user_input:
            continue

        # 检查退出命令
        if user_input.lower() in ["exit", "quit"]:
            console.print("再见！")
            break

        # 检查模式切换命令
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

        # 判断是新任务还是闲聊
        input_type = classify_input(user_input)

        maybe_compress_history()

        if input_type == "task":
            console.print("正在处理新任务...")
            interrupt.reset()
            handle_task_result(run(user_input, mode=current_mode))
        else:
            # 闲聊模式
            response = chat(user_input)
            console.print(response)

if __name__ == "__main__":
    main()