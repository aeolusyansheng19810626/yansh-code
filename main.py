import sys
from rich.console import Console
from rich.panel import Panel
from agent import run

console = Console()

def main():
    console.print(Panel.fit("🚀 yansh-code CLI", style="bold blue"))
    
    requirement = sys.argv[1] if len(sys.argv) > 1 else console.input("[bold green]请输入需求：[reset] ")
    if not requirement.strip():
        console.print("[red]错误：需求不能为空[/red]")
        sys.exit(1)
    
    console.print("[yellow]正在处理需求...[/yellow]")
    result = run(requirement)
    
    if result["success"]:
        console.print(Panel.fit("✅ 任务完成", style="bold green"))
    else:
        console.print(Panel.fit("❌ 任务失败", style="bold red"))
        test_result = result["test_result"]
        if test_result.get("stdout"):
            console.print("[blue]测试输出：[reset]")
            console.print(test_result["stdout"])
        if test_result.get("stderr"):
            console.print("[red]错误信息：[reset]")
            console.print(test_result["stderr"])

if __name__ == "__main__":
    main()