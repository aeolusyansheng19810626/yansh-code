import json
import threading
from openai import OpenAI
from rich.console import Console
from config import OPENROUTER_API_KEY, OPENROUTER_BASE_URL, QUALITY_CASCADE, MAX_ATTEMPTS
from tools import write_file, read_file, execute_command, list_files, replace_in_file
import interrupt

console = Console()

# 对话历史管理
conversation_history = []
MAX_HISTORY = 20
CHAT_CONTEXT_ROUNDS = 5
COMPRESS_THRESHOLD = 6000
COMPRESS_MODEL = "meta-llama/llama-3.1-8b-instant"

# 初始化OpenAI客户端（兼容OpenRouter）
client = OpenAI(
    api_key=OPENROUTER_API_KEY,
    base_url=OPENROUTER_BASE_URL,
)

def add_to_history(user_msg, assistant_msg):
    """添加对话到历史，超过最大长度时删除最早的"""
    global conversation_history
    conversation_history.append({"role": "user", "content": user_msg})
    conversation_history.append({"role": "assistant", "content": assistant_msg})
    
    # 保持历史在最大长度内
    while len(conversation_history) > MAX_HISTORY * 2:
        conversation_history.pop(0)
        conversation_history.pop(0)

def get_recent_history(rounds=CHAT_CONTEXT_ROUNDS):
    """获取最近N轮对话历史"""
    return conversation_history[-(rounds * 2):] if conversation_history else []

KEEP_RECENT_TURNS = 3  # 压缩时保留最近 N 轮不压缩

def maybe_compress_history():
    """历史字符数超过阈值时，压缩旧轮次，保留最近 KEEP_RECENT_TURNS 轮原文"""
    global conversation_history
    total_chars = sum(len(m["content"]) for m in conversation_history)
    if total_chars <= COMPRESS_THRESHOLD:
        return

    keep_count = KEEP_RECENT_TURNS * 2  # 每轮 user+assistant 共2条
    if len(conversation_history) <= keep_count:
        return  # 消息太少，不压缩

    old_msgs = conversation_history[:-keep_count]
    recent_msgs = conversation_history[-keep_count:]

    history_text = "\n".join(f"[{m['role']}]: {m['content']}" for m in old_msgs)
    prompt = (
        f"请将以下对话历史压缩成摘要：\n\n{history_text}\n\n"
        "严格按以下格式输出，不添加其他内容：\n"
        '【已完成任务】\n- ...\n\n【关键文件】\n- ...\n\n【未解决问题】\n- ...（没有则写"无"）'
    )
    try:
        response = client.chat.completions.create(
            model=COMPRESS_MODEL,
            messages=[{"role": "user", "content": prompt}]
        )
        summary = response.choices[0].message.content
    except Exception as e:
        console.print(f"[警告] 上下文压缩失败: {e}")
        return

    conversation_history = [{"role": "assistant", "content": summary}] + recent_msgs
    console.print("[上下文已自动压缩]")

def compress_history():
    """手动压缩：复用同一逻辑，但使用不同的提示文案"""
    global conversation_history
    total_chars = sum(len(m["content"]) for m in conversation_history)
    keep_count = KEEP_RECENT_TURNS * 2
    if total_chars <= COMPRESS_THRESHOLD or len(conversation_history) <= keep_count:
        console.print("[上下文较短，无需压缩]")
        return

    old_msgs = conversation_history[:-keep_count]
    recent_msgs = conversation_history[-keep_count:]
    history_text = "\n".join(f"[{m['role']}]: {m['content']}" for m in old_msgs)
    prompt = (
        f"请将以下对话历史压缩成摘要：\n\n{history_text}\n\n"
        "严格按以下格式输出，不添加其他内容：\n"
        '【已完成任务】\n- ...\n\n【关键文件】\n- ...\n\n【未解决问题】\n- ...（没有则写"无"）'
    )
    try:
        response = client.chat.completions.create(
            model=COMPRESS_MODEL,
            messages=[{"role": "user", "content": prompt}]
        )
        summary = response.choices[0].message.content
    except Exception as e:
        console.print(f"[警告] 上下文压缩失败: {e}")
        return
    conversation_history = [{"role": "assistant", "content": summary}] + recent_msgs
    console.print("[上下文已手动压缩]")

def show_context():
    """打印当前上下文大小"""
    turns = len(conversation_history) // 2
    total_chars = sum(len(m["content"]) for m in conversation_history)
    hint = "  （建议压缩）" if total_chars > COMPRESS_THRESHOLD else ""
    console.print(f"[上下文状态] 共 {turns} 轮，总字符数：{total_chars} / {COMPRESS_THRESHOLD}{hint}")

def clear_history():
    """清空对话历史"""
    global conversation_history
    conversation_history = []
    console.print("[上下文已清除]")

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
    """尝试QUALITY_CASCADE中的模型，依次降级调用。在子线程中执行，每100ms检查一次ESC中断。"""
    result_holder = [None]
    exc_holder = [None]

    def _worker():
        for model in QUALITY_CASCADE:
            try:
                kwargs = {"model": model, "messages": messages}
                if tools is not None:
                    kwargs["tools"] = tools
                if tool_choice is not None:
                    kwargs["tool_choice"] = tool_choice
                if response_format is not None:
                    kwargs["response_format"] = response_format
                result_holder[0] = client.chat.completions.create(**kwargs)
                return
            except Exception as e:
                exc_holder[0] = e
                continue

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    while t.is_alive():
        if interrupt.is_interrupted():
            raise interrupt.Interrupted()
        t.join(timeout=0.1)

    if result_holder[0] is None:
        raise RuntimeError(f"所有模型调用均失败: {exc_holder[0]}")
    return result_holder[0]


def plan(requirement):
    """制定计划：生成文件列表和测试命令"""
    import platform
    
    # 检测系统并生成命令提示
    system_name = platform.system()
    if system_name == "Windows":
        cmd_hint = "当前运行环境是 Windows，使用 Windows 命令：查看文件用 type，列目录用 dir，禁止使用 cat、ls、grep。"
    else:
        cmd_hint = "当前运行环境是 Linux/Mac，使用 Unix 命令：查看文件用 cat，列目录用 ls。"
    
    # 先获取当前 workspace 文件结构，注入到 LLM 上下文中避免重复创建
    ws_files = list_files()
    files_list = "\n".join(f"- {f}" for f in ws_files.get("files", []))
    system_prompt = f"""你是一个代码规划助手。根据用户需求，返回JSON格式的计划，包含：
- files：数组，每个元素为 {{"filename": "文件名", "description": "修改意图/需求说明"}}；对于已有文件只需填写修改意图，不要重复列出完整内容
- test_command：测试命令

注意目录结构：实现文件放workspace/根目录（如add.py），测试文件必须放workspace/tests/目录（如tests/test_add.py）。

{cmd_hint}

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
    console.print(f"计划处理 {len(files)} 个文件...")

    for file_entry in files:
        if interrupt.is_interrupted():
            raise interrupt.Interrupted()

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
                console.print(f"读取 {filename} 失败: {existing['error']}")
                continue
            existing_content = existing.get("content", "")
            console.print(f"{filename} 已存在，读取现有内容进行增量修改...")

            sys_prompt = """你是一个代码修改助手。对已有文件进行精确修改。

可用操作：
1. replace_in_file(filename, old_str, new_str) — 对已有文件做精确替换
2. write_file(filename, content) — 仅用于新建文件

规则：
- 已有文件**必须**使用 replace_in_file 做精确替换，不得使用 write_file 重写整个文件
- write_file 只允许用于新建文件
- 每次调用 replace_in_file 只修改一处，如有多处修改需要多次调用"""
        else:
            console.print(f"{filename} 是新建文件...")
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
                        console.print(f"写入 {func_args.get('filename')}")
                    elif func_name == "read_file":
                        result = read_file(**func_args)
                    elif func_name == "replace_in_file":
                        result = replace_in_file(**func_args)
                        if "success" in result:
                            console.print(f"replace_in_file: {result.get('filename')}")
                            console.print(f"- {result.get('old_str')}")
                            console.print(f"+ {result.get('new_str')}")
                        else:
                            console.print(f"替换失败: {result.get('error')}")
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

    console.print("代码生成/修改完成")

def fix(test_result, plan):
    """根据测试错误修复代码（多轮工具调用）"""
    console.print("开始修复代码...")
    
    # 优先使用 stderr，如果为空则使用截断的 stdout
    stderr = test_result.get("stderr", "")
    stdout = test_result.get("stdout", "")
    
    if stderr:
        error_info = stderr
    elif stdout:
        # 截断 stdout 到最多 500 字符
        error_info = stdout[:500]
        if len(stdout) > 500:
            error_info += "\n... (输出已截断)"
    else:
        error_info = "未知错误"
    
    content = f"测试失败！\n错误输出：\n{error_info}\n\n计划：{json.dumps(plan)}"
    
    messages = [
        {"role": "system", "content": "你是代码修复助手。根据错误信息修复代码，使用write_file工具重写文件。"},
        {"role": "user", "content": content}
    ]
    
    while True:
        response = call_llm(messages, tools=TOOLS, tool_choice="auto")
        response_message = response.choices[0].message
        messages.append(response_message)

        if response_message.tool_calls:
            console.print(f"执行 {len(response_message.tool_calls)} 个修复操作...")
            for tool_call in response_message.tool_calls:
                func_name = tool_call.function.name
                func_args = json.loads(tool_call.function.arguments)
                
                if func_name == "write_file":
                    result = write_file(**func_args)
                    console.print(f"修复 {func_args.get('filename')}")
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
            console.print("修复完成")
            break

def test(test_command):
    """执行测试命令"""
    if not test_command or not test_command.strip():
        console.print("警告：无测试命令，跳过测试")
        return {"returncode": 0, "stdout": "", "stderr": ""}
    
    console.print(f"执行测试：{test_command}")
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

def _interrupted_result():
    console.print("已中断")
    return {"success": False, "test_result": {"returncode": -1, "stdout": "", "stderr": "已中断"}}


def run(requirement, mode="auto"):
    """主运行流程。mode: auto（默认）| plan（只出计划）| code（跳过确认直接执行）"""
    try:
        return _run(requirement, mode)
    except interrupt.Interrupted:
        return _interrupted_result()


def _run(requirement, mode):
    original_requirement = requirement

    # 阶段1：制定计划
    console.print("阶段1：制定计划")
    plan_result = plan(requirement)

    # 格式化计划输出
    def print_plan(plan_result):
        files = plan_result.get("files", [])
        test_cmd = plan_result.get("test_command", "")
        total_steps = len(files) + (1 if test_cmd else 0)
        for idx, file_entry in enumerate(files, 1):
            filename = file_entry.get("filename", "") if isinstance(file_entry, dict) else file_entry
            console.print(f"[{idx}/{total_steps}] write_file: {filename}")
        if test_cmd:
            console.print(f"[{total_steps}/{total_steps}] execute: {test_cmd}")

    print_plan(plan_result)

    # plan 模式：只输出计划，直接返回
    if mode == "plan":
        return {"success": True, "test_result": {"returncode": 0, "stdout": "", "stderr": ""}}

    # auto 模式：等待用户确认，支持修改重试
    if mode == "auto":
        retry_count = 0
        max_retries = 3
        while retry_count <= max_retries:
            user_confirm = console.input("\n确认执行？(y/n/修改) ").strip().lower()
            if interrupt.is_interrupted():
                raise interrupt.Interrupted()
            if user_confirm == 'y':
                break
            elif user_confirm == 'n':
                console.print("已取消")
                return {"success": False, "test_result": {"returncode": -1, "stdout": "", "stderr": "用户取消"}}
            else:
                if retry_count >= max_retries:
                    console.print(f"已达到最大重试次数 ({max_retries})，使用当前计划")
                    break
                console.print(f"正在根据修改意见重新生成计划... (尝试 {retry_count + 1}/{max_retries})")
                plan_result = plan(f"{requirement}\n\n修改意见：{user_confirm}")
                print_plan(plan_result)
                retry_count += 1

    # code / auto（确认后）：生成代码 + 测试
    console.print("\n阶段2：生成代码")
    code(plan_result)

    console.print("\n阶段3：测试与修复")
    attempts = 0
    test_result = None
    while attempts < MAX_ATTEMPTS:
        test_result = test(plan_result.get("test_command", ""))
        if judge(test_result):
            console.print("测试通过！")
            files = plan_result.get("files", [])
            file_names = [f.get("filename") if isinstance(f, dict) else str(f) for f in files]
            file_names = [name for name in file_names if name]
            summary = f"执行了任务：{original_requirement}。创建/修改了文件：{', '.join(file_names)}"
            add_to_history(original_requirement, summary)
            return report(True, test_result)
        else:
            console.print(f"测试失败 (尝试 {attempts + 1}/{MAX_ATTEMPTS})")
            if attempts < MAX_ATTEMPTS - 1:
                fix(test_result, plan_result)
            attempts += 1

    console.print("达到最大尝试次数，任务失败")
    add_to_history(original_requirement, f"任务失败：{original_requirement}")
    return report(False, test_result)


def classify_input(user_input):
    """判断用户输入是新任务还是闲聊"""
    messages = [
        {"role": "system", "content": "判断以下输入是'新任务'还是'闲聊'，只回复 task 或 chat。"},
        {"role": "user", "content": f"输入：{user_input}"}
    ]
    response = call_llm(messages)
    result = response.choices[0].message.content.strip().lower()
    return "task" if "task" in result else "chat"

def chat(user_input):
    """闲聊模式，LLM 直接回复，控制在 100 字以内"""
    messages = [
        {"role": "system", "content": "你是一个友好的助手。简洁回复用户，控制在 100 字以内。"}
    ]
    
    # 添加最近5轮历史
    messages.extend(get_recent_history())
    
    # 添加当前用户输入
    messages.append({"role": "user", "content": user_input})
    
    response = call_llm(messages)
    assistant_reply = response.choices[0].message.content
    
    # 保存到历史
    add_to_history(user_input, assistant_reply)
    
    return assistant_reply
