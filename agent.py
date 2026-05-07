import json
from openai import OpenAI
from rich.console import Console
from rich.panel import Panel
from config import OPENROUTER_API_KEY, OPENROUTER_BASE_URL, QUALITY_CASCADE, MAX_ATTEMPTS
from tools import write_file, read_file, execute_command, list_files, replace_in_file

console = Console()

# 初始化OpenAI客户端（兼容OpenRouter）
client = OpenAI(
    api_key=OPENROUTER_API_KEY,
    base_url=OPENROUTER_BASE_URL,
)

# 定义可用工具
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "在workspace目录下写入文件",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "文件名（相对于workspace）"},
                    "content": {"type": "string", "description": "文件内容"}
                },
                "required": ["filename", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取workspace目录下的文件",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "文件名（相对于workspace）"}
                },
                "required": ["filename"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "execute_command",
            "description": "在workspace目录下执行命令（30秒超时）",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的命令"}
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "列出workspace目录下的所有文件",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "replace_in_file",
            "description": "在workspace文件中精确替换字符串。old_str必须唯一匹配，否则返回错误",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "文件名（相对于workspace）"},
                    "old_str": {"type": "string", "description": "要替换的旧字符串（必须唯一匹配）"},
                    "new_str": {"type": "string", "description": "替换后的新字符串"}
                },
                "required": ["filename", "old_str", "new_str"]
            }
        }
    }
]

def call_llm(messages, tools=None, tool_choice=None, response_format=None):
    """尝试QUALITY_CASCADE中的模型，依次降级调用"""
    for model in QUALITY_CASCADE:
        try:
            kwargs = {
                "model": model,
                "messages": messages
            }
            if tools is not None:
                kwargs["tools"] = tools
            if tool_choice is not None:
                kwargs["tool_choice"] = tool_choice
            if response_format is not None:
                kwargs["response_format"] = response_format
            response = client.chat.completions.create(**kwargs)
            return response
        except Exception as e:
            print(f"模型 {model} 调用失败: {e}")
            continue
    raise RuntimeError("所有模型调用均失败")


def plan(requirement):
    """制定计划：生成文件列表和测试命令"""
    # 先获取当前 workspace 文件结构，注入到 LLM 上下文中避免重复创建
    ws_files = list_files()
    files_list = "\n".join(f"- {f}" for f in ws_files.get("files", []))
    system_prompt = f"""你是一个代码规划助手。根据用户需求，返回JSON格式的计划，包含：
- files：数组，每个元素为 {{"filename": "文件名", "description": "修改意图/需求说明"}}；对于已有文件只需填写修改意图，不要重复列出完整内容
- test_command：测试命令

注意目录结构：实现文件放workspace/根目录（如add.py），测试文件必须放workspace/tests/目录（如tests/test_add.py）。

当前workspace已有文件：
{files_list if files_list else "(空)"}

注意：不要重复创建已有文件，尽量基于已有文件做增量修改。对已有文件只描述要追加/修改什么。"""
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"需求：{requirement}"}
    ]
    response = call_llm(messages, response_format={"type": "json_object"})
    return json.loads(response.choices[0].message.content)

def code(plan):
    """根据计划逐个文件生成/修改代码。文件已存在优先用replace_in_file做精确修改；不存在则用write_file新建。"""
    import os as _os
    from tools import write_file, read_file, replace_in_file
    from config import WORKSPACE_DIR

    files = plan.get("files", [])
    console.print(f"[blue]计划处理 {len(files)} 个文件...[/blue]")

    for file_entry in files:
        if isinstance(file_entry, dict):
            filename = file_entry.get("filename", "")
            intent = file_entry.get("intent", file_entry.get("description", ""))
        else:
            filename = file_entry
            intent = ""

        if not filename:
            continue

        filepath = _os.path.join(WORKSPACE_DIR, filename)
        file_exists = _os.path.exists(filepath)

        if file_exists:
            existing = read_file(filename)
            if "error" in existing:
                console.print(f"[red]✗ 读取 {filename} 失败: {existing['error']}[/red]")
                continue
            existing_content = existing.get("content", "")
            console.print(f"[yellow]📖 {filename} 已存在，读取现有内容进行增量修改...[/yellow]")

            sys_prompt = """你是一个代码修改助手。对已有文件进行精确修改。

可用操作：
1. replace_in_file(filename, old_str, new_str) — 对已有文件做精确替换
2. write_file(filename, content) — 仅用于新建文件

规则：
- 已有文件**必须**使用 replace_in_file 做精确替换，不得使用 write_file 重写整个文件
- write_file 只允许用于新建文件
- 每次调用 replace_in_file 只修改一处，如有多处修改需要多次调用"""
        else:
            console.print(f"[green]🆕 {filename} 是新建文件...[/green]")
            sys_prompt = f"""你是一个代码生成助手。请生成文件 `{filename}` 的完整代码。

可用操作：
1. write_file(filename, content) — 写入新文件

需求/修改意图：{intent}

注意：测试文件必须放在tests/目录下，且测试文件开头需要加sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))来正确导入实现模块。"""

        # 构建消息
        user_content = f"当前文件：{filename}\n修改意图：{intent}"
        if file_exists:
            user_content += f"\n\n现有内容：\n```\n{existing_content}\n```"

        msgs = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_content}
        ]

        # 多轮工具调用循环
        attempts_left = 5
        while attempts_left > 0:
            attempts_left -= 1
            response = call_llm(msgs, tools=TOOLS, tool_choice="auto")
            response_message = response.choices[0].message
            msgs.append(response_message)

            if response_message.tool_calls:
                for tool_call in response_message.tool_calls:
                    func_name = tool_call.function.name
                    func_args = json.loads(tool_call.function.arguments)

                    if func_name == "write_file":
                        result = write_file(**func_args)
                        console.print(f"[green]✓ 写入 {func_args.get('filename')}[/green]")
                    elif func_name == "read_file":
                        result = read_file(**func_args)
                    elif func_name == "replace_in_file":
                        result = replace_in_file(**func_args)
                        if "success" in result:
                            console.print(f"[green]✓ 替换 {func_args.get('filename')}[/green]")
                        else:
                            console.print(f"[red]✗ 替换失败: {result.get('error')}[/red]")
                    elif func_name == "execute_command":
                        result = execute_command(**func_args)
                    elif func_name == "list_files":
                        result = list_files()
                    else:
                        result = {"error": "未预期的调用"}

                    msgs.append({
                        "tool_call_id": tool_call.id,
                        "role": "tool",
                        "name": func_name,
                        "content": json.dumps(result)
                    })
            else:
                break

    console.print("[green]✓ 代码生成/修改完成[/green]")

def regression():
    """执行全量回归测试"""
    cmd = "pytest tests/ -v"
    console.print(f"[blue]执行回归测试：{cmd}[/blue]")
    return execute_command(cmd)

def fix(test_result, plan, regression_error=None):
    """根据测试错误修复代码（多轮工具调用）"""
    console.print("[yellow]开始修复代码...[/yellow]")
    
    error_info = test_result.get("stderr") or test_result.get("stdout") or "未知错误"
    
    content = f"测试失败！\n错误输出：\n{error_info}\n\n计划：{json.dumps(plan)}"
    if regression_error:
        content += f"\n\n回归测试也失败了！\n回归错误输出：\n{regression_error}"
    
    messages = [
        {"role": "system", "content": "你是代码修复助手。根据错误信息修复代码，使用write_file工具重写文件。"},
        {"role": "user", "content": content}
    ]
    
    while True:
        response = call_llm(messages, tools=TOOLS, tool_choice="auto")
        response_message = response.choices[0].message
        messages.append(response_message)
        
        if response_message.tool_calls:
            console.print(f"[blue]执行 {len(response_message.tool_calls)} 个修复操作...[/blue]")
            for tool_call in response_message.tool_calls:
                func_name = tool_call.function.name
                func_args = json.loads(tool_call.function.arguments)
                
                if func_name == "write_file":
                    result = write_file(**func_args)
                    console.print(f"[green]✓ 修复 {func_args.get('filename')}[/green]")
                elif func_name == "read_file":
                    result = read_file(**func_args)
                elif func_name == "execute_command":
                    result = execute_command(**func_args)
                elif func_name == "list_files":
                    result = list_files()
                else:
                    result = {"error": "未预期的调用"}
                
                messages.append({
                    "tool_call_id": tool_call.id,
                    "role": "tool",
                    "name": func_name,
                    "content": json.dumps(result)
                })
        else:
            console.print("[green]✓ 修复完成[/green]")
            break

def test(test_command):
    """执行测试命令"""
    if not test_command or not test_command.strip():
        console.print("[yellow]警告：无测试命令，跳过测试[/yellow]")
        return {"returncode": 0, "stdout": "", "stderr": ""}
    
    console.print(f"[blue]执行测试：{test_command}[/blue]")
    return execute_command(test_command)

def judge(test_result):
    """判断测试是否通过"""
    return test_result.get("returncode") == 0

def report(success, test_result):
    """输出最终结果"""
    return {
        "success": success,
        "test_result": test_result
    }

def run(requirement):
    """主运行流程"""
    # 1. 制定计划
    console.print(Panel.fit("📋 阶段1：制定计划", style="bold blue"))
    plan_result = plan(requirement)
    console.print(f"[green]计划：{json.dumps(plan_result, ensure_ascii=False)}[/green]")
    
    # 2. 生成代码
    console.print(Panel.fit("✍️ 阶段2：生成代码", style="bold blue"))
    code(plan_result)
    
    # 3. 测试循环
    console.print(Panel.fit("🧪 阶段3：测试与修复", style="bold blue"))
    attempts = 0
    test_result = None
    while attempts < MAX_ATTEMPTS:
        test_result = test(plan_result.get("test_command", ""))
        if judge(test_result):
            console.print("[bold green]✅ 单元测试通过！[/bold green]")
            # 测试通过后执行全量回归
            regression_result = regression()
            if judge(regression_result):
                console.print("[bold green]✅ 回归测试通过！[/bold green]")
                return report(True, test_result)
            else:
                console.print(f"[red]❌ 回归测试失败 (尝试 {attempts + 1}/{MAX_ATTEMPTS})[/red]")
                regression_error = regression_result.get("stderr") or regression_result.get("stdout") or "回归测试失败"
                if attempts < MAX_ATTEMPTS - 1:
                    fix(test_result, plan_result, regression_error=regression_error)
                attempts += 1
        else:
            console.print(f"[red]❌ 测试失败 (尝试 {attempts + 1}/{MAX_ATTEMPTS})[/red]")
            if attempts < MAX_ATTEMPTS - 1:
                fix(test_result, plan_result)
            attempts += 1
    
    console.print("[bold red]❌ 达到最大尝试次数，任务失败[/bold red]")
    return report(False, test_result)
