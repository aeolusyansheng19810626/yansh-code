import os
import shutil
import sys
import subprocess
import time
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools import write_file, read_file, delete_file, list_files, execute_command, replace_in_file
from config import WORKSPACE_DIR

TEST_SUBDIR = "test_tools_workspace"


def _force_rmtree(path, max_retries=5, delay=1):
    """带重试的 rmtree，应对 Windows 文件锁"""
    for i in range(max_retries):
        if not os.path.exists(path):
            return
        try:
            shutil.rmtree(path)
            return
        except PermissionError:
            if i < max_retries - 1:
                time.sleep(delay)
            else:
                shutil.rmtree(path, ignore_errors=True)


@pytest.fixture(autouse=True)
def setup_teardown():
    """每个测试前后清理workspace，使用独立子目录避免干扰已有文件"""
    original_workspace = WORKSPACE_DIR
    test_dir = os.path.join(original_workspace, TEST_SUBDIR)
    _force_rmtree(test_dir)
    os.makedirs(test_dir, exist_ok=True)

    import config
    config.WORKSPACE_DIR = test_dir

    from importlib import reload
    import tools
    reload(tools)
    globals()["write_file"] = tools.write_file
    globals()["read_file"] = tools.read_file
    globals()["delete_file"] = tools.delete_file
    globals()["list_files"] = tools.list_files
    globals()["execute_command"] = tools.execute_command
    globals()["replace_in_file"] = tools.replace_in_file
    globals()["WORKSPACE_DIR_OVERRIDE"] = test_dir

    yield

    _force_rmtree(test_dir)
    config.WORKSPACE_DIR = original_workspace
    reload(tools)
    globals()["write_file"] = tools.write_file
    globals()["read_file"] = tools.read_file
    globals()["delete_file"] = tools.delete_file
    globals()["list_files"] = tools.list_files
    globals()["execute_command"] = tools.execute_command
    globals()["replace_in_file"] = tools.replace_in_file


def test_write_and_read():
    """写入内容后读回来内容一致"""
    result = write_file("hello.txt", "Hello, World!")
    assert "success" in result

    result = read_file("hello.txt")
    assert result["content"] == "Hello, World!"


def test_delete_file():
    """删除后文件不存在"""
    write_file("tmp.txt", "to be deleted")
    result = delete_file("tmp.txt")
    assert "success" in result

    result = read_file("tmp.txt")
    assert "error" in result


def test_delete_nonexistent():
    """删除不存在的文件返回合理错误"""
    result = delete_file("does_not_exist.txt")
    assert "error" in result
    assert "不存在" in result["error"]


def test_list_files():
    """写入几个文件后list能返回正确列表"""
    write_file("a.txt", "aaa")
    write_file("b.txt", "bbb")
    write_file("sub/c.txt", "ccc")

    result = list_files()
    files = result["files"]
    assert "a.txt" in files
    assert "b.txt" in files
    expected = os.path.join("sub", "c.txt")
    assert expected in files
    assert len(files) == 3


def test_list_files_empty():
    """空目录返回空列表"""
    result = list_files()
    assert result["files"] == []


def test_execute_command_success():
    """正常命令返回码为0，stdout有内容"""
    result = execute_command("echo hello")
    assert result["returncode"] == 0
    assert "hello" in result["stdout"]


def test_execute_command_timeout():
    """超时测试：sleep 60 应在30秒内被终止，返回非0"""
    result = execute_command("python -c \"import time; time.sleep(60)\"")
    assert "error" in result
    assert "超时" in result["error"]


def test_replace_in_file_success():
    """正常替换：写入文件后替换指定内容，读回验证"""
    write_file("replace.txt", "Hello, World! This is old text.")
    result = replace_in_file("replace.txt", "old", "new")
    assert "success" in result

    result = read_file("replace.txt")
    assert result["content"] == "Hello, World! This is new text."


def test_replace_in_file_not_found():
    """old_str不存在时返回错误"""
    write_file("replace.txt", "Hello, World!")
    result = replace_in_file("replace.txt", "nonexistent", "something")
    assert "error" in result
    assert "未找到" in result["error"]


def test_replace_in_file_multiple_matches():
    """old_str匹配多处时返回错误"""
    write_file("replace.txt", "aaa aaa aaa")
    result = replace_in_file("replace.txt", "aaa", "bbb")
    assert "error" in result
    assert "3 处匹配" in result["error"]


def test_replace_in_file_path_traversal():
    """路径越界返回错误"""
    result = replace_in_file("../secret.txt", "old", "new")
    assert "error" in result
    assert "超出" in result["error"]


def test_path_traversal_protection():
    """路径越界保护：尝试写入workspace外部应该返回错误"""
    result = write_file("../secret.txt", "hack")
    assert "error" in result
    assert "超出" in result["error"]

    result = read_file("../secret.txt")
    assert "error" in result
    assert "超出" in result["error"]

    result = delete_file("../secret.txt")
    assert "error" in result
    assert "超出" in result["error"]