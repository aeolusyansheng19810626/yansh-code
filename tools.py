import os
import subprocess
from config import WORKSPACE_DIR

def write_file(filename, content):
    """在workspace目录下写入文件"""
    os.makedirs(WORKSPACE_DIR, exist_ok=True)
    filepath = os.path.join(WORKSPACE_DIR, filename)
    
    # 安全检查：确保路径在workspace内
    abs_workspace = os.path.abspath(WORKSPACE_DIR)
    abs_filepath = os.path.abspath(filepath)
    if not abs_filepath.startswith(abs_workspace):
        return {"error": "文件路径超出workspace目录"}
    
    try:
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)
        return {"success": f"文件 {filename} 写入成功"}
    except Exception as e:
        return {"error": str(e)}

def read_file(filename):
    """读取workspace目录下的文件"""
    filepath = os.path.join(WORKSPACE_DIR, filename)
    
    abs_workspace = os.path.abspath(WORKSPACE_DIR)
    abs_filepath = os.path.abspath(filepath)
    if not abs_filepath.startswith(abs_workspace):
        return {"error": "文件路径超出workspace目录"}
    
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
        return {"content": content}
    except Exception as e:
        return {"error": str(e)}

def execute_command(command):
    """在workspace目录下执行命令，30秒超时"""
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=WORKSPACE_DIR
        )
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode
        }
    except subprocess.TimeoutExpired:
        return {"error": "命令执行超时（30秒）"}
    except Exception as e:
        return {"error": str(e)}

def delete_file(filename):
    """删除workspace目录下的文件"""
    filepath = os.path.join(WORKSPACE_DIR, filename)
    
    abs_workspace = os.path.abspath(WORKSPACE_DIR)
    abs_filepath = os.path.abspath(filepath)
    if not abs_filepath.startswith(abs_workspace):
        return {"error": "文件路径超出workspace目录"}
    
    try:
        os.remove(filepath)
        return {"success": f"文件 {filename} 删除成功"}
    except FileNotFoundError:
        return {"error": f"文件 {filename} 不存在"}
    except Exception as e:
        return {"error": str(e)}

def replace_in_file(filename, old_str, new_str):
    """在workspace文件中精确替换字符串。old_str必须唯一匹配，否则返回错误"""
    filepath = os.path.join(WORKSPACE_DIR, filename)

    abs_workspace = os.path.abspath(WORKSPACE_DIR)
    abs_filepath = os.path.abspath(filepath)
    if not abs_filepath.startswith(abs_workspace):
        return {"error": "文件路径超出workspace目录"}

    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
    except FileNotFoundError:
        return {"error": f"文件 {filename} 不存在"}
    except Exception as e:
        return {"error": str(e)}

    count = content.count(old_str)
    if count == 0:
        return {"error": f"在 {filename} 中未找到要替换的字符串"}
    if count > 1:
        return {"error": f"在 {filename} 中找到 {count} 处匹配，需唯一匹配"}

    content = content.replace(old_str, new_str, 1)
    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)
        return {"success": f"文件 {filename} 替换成功"}
    except Exception as e:
        return {"error": str(e)}

def list_files():
    """列出workspace目录下的所有文件"""
    os.makedirs(WORKSPACE_DIR, exist_ok=True)
    files = []
    for root, dirs, filenames in os.walk(WORKSPACE_DIR):
        for filename in filenames:
            rel_path = os.path.relpath(os.path.join(root, filename), WORKSPACE_DIR)
            files.append(rel_path)
    return {"files": files}