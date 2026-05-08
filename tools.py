import os
import subprocess
import shutil
from pathlib import Path
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
    """在workspace目录下执行命令，30秒超时，实时输出"""
    import sys
    import time
    
    try:
        # 使用 Popen 实现流式输出
        process = subprocess.Popen(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=WORKSPACE_DIR,
            bufsize=1  # 行缓冲
        )
        
        stdout_lines = []
        stderr_lines = []
        start_time = time.time()
        
        # 实时读取输出
        while True:
            # 检查超时
            if time.time() - start_time > 30:
                process.kill()
                return {"error": "命令执行超时（30秒）"}
            
            # 读取 stdout
            line = process.stdout.readline()
            if line:
                print(line, end='', flush=True)  # 实时打印
                stdout_lines.append(line)
            
            # 检查进程是否结束
            if process.poll() is not None:
                # 读取剩余输出
                remaining = process.stdout.read()
                if remaining:
                    print(remaining, end='', flush=True)
                    stdout_lines.append(remaining)
                break
        
        # 读取 stderr
        stderr_output = process.stderr.read()
        if stderr_output:
            stderr_lines.append(stderr_output)
        
        return {
            "stdout": ''.join(stdout_lines),
            "stderr": ''.join(stderr_lines),
            "returncode": process.returncode
        }
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
        return {
            "success": f"文件 {filename} 替换成功",
            "filename": filename,
            "old_str": old_str,
            "new_str": new_str
        }
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
def move_file(src, dst):
    """移动文件从src到dst（相对于workspace）
    - 自动创建dst父目录
    - src不存在返回错误
    - 路径越界返回错误
    """
    # 构建完整路径
    src_path = Path(WORKSPACE_DIR) / src
    dst_path = Path(WORKSPACE_DIR) / dst
    
    # 安全检查：确保路径在workspace内
    abs_workspace = Path(WORKSPACE_DIR).resolve()
    abs_src = src_path.resolve()
    abs_dst = dst_path.resolve()
    
    try:
        if not abs_src.is_relative_to(abs_workspace):
            return {"error": "Source file path exceeds workspace directory"}
        if not abs_dst.is_relative_to(abs_workspace):
            return {"error": "Destination file path exceeds workspace directory"}
    except ValueError:
        # is_relative_to may throw ValueError in some cases
        return {"error": "File path exceeds workspace directory"}
    
    # Check if src exists
    if not src_path.exists():
        return {"error": f"Source file {src} does not exist"}
    
    # Create dst parent directory
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Move file
    try:
        shutil.move(str(src_path), str(dst_path))
        return {"success": f"File moved from {src} to {dst} successfully"}
    except Exception as e:
        return {"error": str(e)}

def search_in_files(pattern, workspace=None, regex=False, extensions=None):
    """在workspace目录下搜索文件内容
    
    Args:
        pattern: 搜索模式（字符串或正则）
        workspace: 搜索目录（默认使用WORKSPACE_DIR）
        regex: 是否使用正则表达式匹配
        extensions: 文件扩展名过滤列表（如 [".py", ".md"]）
    
    Returns:
        {"matches": [...], "total": int}
    """
    import re
    
    if workspace is None:
        workspace = Path(WORKSPACE_DIR)
    else:
        workspace = Path(workspace)
    
    # 路径安全检查
    abs_workspace = Path(WORKSPACE_DIR).resolve()
    abs_search_path = workspace.resolve()
    
    try:
        if not abs_search_path.is_relative_to(abs_workspace):
            return {"error": "Search path exceeds workspace directory"}
    except ValueError:
        return {"error": "Search path exceeds workspace directory"}
    
    matches = []
    
    # 递归搜索所有文件
    for file_path in workspace.rglob("*"):
        # 跳过目录
        if file_path.is_dir():
            continue
        
        # 跳过 .git 目录
        if ".git" in file_path.parts:
            continue
        
        # 扩展名过滤
        if extensions is not None:
            if file_path.suffix not in extensions:
                continue
        
        # 读取文件并搜索
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                for line_num, line in enumerate(f, start=1):
                    # 匹配逻辑
                    if regex:
                        if re.search(pattern, line):
                            rel_path = file_path.relative_to(workspace)
                            matches.append({
                                "file": str(rel_path).replace("\\", "/"),
                                "line": line_num,
                                "content": line.rstrip()
                            })
                    else:
                        if pattern in line:
                            rel_path = file_path.relative_to(workspace)
                            matches.append({
                                "file": str(rel_path).replace("\\", "/"),
                                "line": line_num,
                                "content": line.rstrip()
                            })
        except (UnicodeDecodeError, PermissionError):
            # 跳过二进制文件或无权限文件
            continue
    
    return {"matches": matches, "total": len(matches)}