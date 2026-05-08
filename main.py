import sys
from rich.console import Console
from agent import run, classify_input, chat, add_to_history

console = Console()

def main():
    console.print("yansh-code CLI")
    
    # 处理初始需求
    requirement = sys.argv[1] if len(sys.argv) > 1 else console.input("请输入需求：")
    if not requirement.strip():
        console.print("错误：需求不能为空")
        sys.exit(1)
    
    console.print("正在处理需求...")
    result = run(requirement)
    
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
    
    # 进入对话循环
    while True:
        user_input = console.input("\n> ").strip()
        
        if not user_input:
            continue
        
        # 检查退出命令
        if user_input.lower() in ["exit", "quit"]:
            console.print("再见！")
            break
        
        # 判断是新任务还是闲聊
        input_type = classify_input(user_input)
        
        if input_type == "task":
            console.print("正在处理新任务...")
            result = run(user_input)
            if result["success"]:
                console.print("任务完成")
            else:
                console.print("任务失败")
                test_result = result["test_result"]
                if test_result.get("stderr"):
                    console.print("错误信息：")
                    console.print(test_result["stderr"])
        else:
            # 闲聊模式
            response = chat(user_input)
            console.print(response)

if __name__ == "__main__":
    main()