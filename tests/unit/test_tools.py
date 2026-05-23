import os
import shutil
import sys
import subprocess
import time
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from tools import write_file, read_file, delete_file, list_files, execute_command, replace_in_file, move_file, search_in_files, task_complete, ERROR_KINDS, _err
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
    globals()["move_file"] = tools.move_file
    globals()["search_in_files"] = tools.search_in_files
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
    globals()["move_file"] = tools.move_file
    globals()["search_in_files"] = tools.search_in_files


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


def test_move_file_success():
    """正常移动：文件移动后src不存在，dst存在且内容一致"""
    write_file("source.txt", "test content")
    result = move_file("source.txt", "destination.txt")
    assert "success" in result
    
    # 验证src不存在
    result = read_file("source.txt")
    assert "error" in result
    
    # 验证dst存在且内容一致
    result = read_file("destination.txt")
    assert result["content"] == "test content"


def test_move_file_to_subdir():
    """移动到子目录：dst父目录自动创建"""
    write_file("file.txt", "content")
    result = move_file("file.txt", "deep/nested/dir/file.txt")
    assert "success" in result
    
    # 验证文件在新位置
    result = read_file("deep/nested/dir/file.txt")
    assert result["content"] == "content"
    
    # 验证原位置不存在
    result = read_file("file.txt")
    assert "error" in result


def test_move_file_src_not_exist():
    """src不存在：返回错误"""
    result = move_file("nonexistent.txt", "destination.txt")
    assert "error" in result
    assert "does not exist" in result["error"]


def test_move_file_path_traversal():
    """路径越界：src或dst含../返回错误"""
    write_file("test.txt", "content")
    
    # src越界
    result = move_file("../outside.txt", "inside.txt")
    assert "error" in result
    assert "exceeds workspace" in result["error"]
    
    # dst越界
    result = move_file("test.txt", "../outside.txt")
    assert "error" in result
    assert "exceeds workspace" in result["error"]


def test_search_basic():
    """普通字符串匹配：验证返回文件名、行号、内容正确"""
    write_file("test1.py", "def hello():\n    print('world')\n    return 42")
    write_file("test2.py", "# No match here\npass")
    
    result = search_in_files("print", workspace=globals()["WORKSPACE_DIR_OVERRIDE"])
    assert result["total"] == 1
    assert len(result["matches"]) == 1
    
    match = result["matches"][0]
    assert match["file"] == "test1.py"
    assert match["line"] == 2
    assert "print('world')" in match["content"]


def test_search_no_match():
    """搜索不存在的字符串：验证 total=0"""
    write_file("file.py", "def foo():\n    pass")
    
    result = search_in_files("nonexistent_string", workspace=globals()["WORKSPACE_DIR_OVERRIDE"])
    assert result["total"] == 0
    assert result["matches"] == []


def test_search_regex():
    """正则模式匹配：验证能找到函数定义"""
    write_file("code.py", "def hello():\n    pass\ndef world():\n    pass\nclass Test:\n    pass")
    
    result = search_in_files(r"def \w+", workspace=globals()["WORKSPACE_DIR_OVERRIDE"], regex=True)
    assert result["total"] == 2
    assert len(result["matches"]) == 2
    
    # 验证找到两个函数定义
    lines = [m["line"] for m in result["matches"]]
    assert 1 in lines
    assert 3 in lines


def test_search_extension_filter():
    """扩展名过滤：只搜 .py 文件，验证 .txt 文件中的匹配被排除"""
    write_file("script.py", "target_word in python")
    write_file("note.txt", "target_word in text")
    write_file("readme.md", "target_word in markdown")

    result = search_in_files("target_word", workspace=globals()["WORKSPACE_DIR_OVERRIDE"], extensions=[".py"])
    assert result["total"] == 1
    assert result["matches"][0]["file"] == "script.py"


# ── #58 HIL diff 生成 ──────────────────────────────────────────────────────────

import agent as _agent
import hil as _hil


def test_build_diff_lines_new_file():
    """`is_new_file=True` 时 from 为"新建文件"，所有行为 + 开头"""
    lines = _hil._build_diff_lines("foo.py", "", "a = 1\nb = 2\n", is_new_file=True)
    assert any("+a = 1" in l for l in lines)
    header_from = next((l for l in lines if l.startswith("---")), "")
    assert "新建文件" in header_from


def test_build_diff_lines_modify():
    """修改场景：- 行包含旧内容，+ 行包含新内容"""
    old = "def foo():\n    return 1\n"
    new = "def foo():\n    return 42\n"
    lines = _hil._build_diff_lines("bar.py", old, new)
    assert any("-    return 1" in l for l in lines)
    assert any("+    return 42" in l for l in lines)


def test_build_diff_lines_no_change():
    """内容相同时返回空列表"""
    content = "x = 1\n"
    lines = _hil._build_diff_lines("same.py", content, content)
    assert lines == []


def test_build_diff_lines_truncation():
    """超过 50 行时截断并插入提示行"""
    old = "\n".join(f"line{i} = {i}" for i in range(60)) + "\n"
    new = "\n".join(f"line{i} = {i + 1}" for i in range(60)) + "\n"
    lines = _hil._build_diff_lines("big.py", old, new)
    assert len(lines) <= 41  # 30 + 1截断提示 + 10
    assert any("截断" in l for l in lines)


def test_build_diff_lines_exactly_50_no_truncation():
    """恰好 50 行时不截断"""
    old = "\n".join(f"a{i}" for i in range(24)) + "\n"
    new = "\n".join(f"b{i}" for i in range(24)) + "\n"
    lines = _hil._build_diff_lines("mid.py", old, new)
    assert not any("截断" in l for l in lines)


# ── #50 多模态视觉 ─────────────────────────────────────────────────────────────


def _make_png_bytes(w, h, color="red"):
    PIL = pytest.importorskip("PIL")
    from PIL import Image
    import io
    img = Image.new("RGB", (w, h), color=color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_load_image_base64():
    """小图片 base64 编码正确：解码回 PNG 可被 PIL 打开且尺寸一致"""
    pytest.importorskip("PIL")
    import base64, io
    from PIL import Image

    test_dir = globals()["WORKSPACE_DIR_OVERRIDE"]
    png_path = os.path.join(test_dir, "test_img.png")
    with open(png_path, "wb") as f:
        f.write(_make_png_bytes(10, 10))

    result = _agent._load_image_file(png_path)
    assert "error" not in result
    assert result["width"] == 10
    assert result["height"] == 10
    assert result["mime_type"] == "image/png"

    raw = base64.b64decode(result["base64"])
    img = Image.open(io.BytesIO(raw))
    assert img.size == (10, 10)


def test_load_image_resize():
    """超过 2048px 的图片自动缩放，保持比例"""
    pytest.importorskip("PIL")

    test_dir = globals()["WORKSPACE_DIR_OVERRIDE"]
    png_path = os.path.join(test_dir, "big_img.png")
    with open(png_path, "wb") as f:
        f.write(_make_png_bytes(3000, 1500))

    result = _agent._load_image_file(png_path)
    assert "error" not in result
    assert result["width"] == 2048
    assert result["height"] == 1024


def test_load_image_unsupported_format():
    """不支持的格式返回 error，消息包含'不支持'"""
    test_dir = globals()["WORKSPACE_DIR_OVERRIDE"]
    bmp_path = os.path.join(test_dir, "test.bmp")
    with open(bmp_path, "wb") as f:
        f.write(b"BM fake data")

    result = _agent._load_image_file(bmp_path)
    assert "error" in result
    assert "不支持" in result["error"]


def test_build_vision_content():
    """vision content 数组：图片在前、文字在后，URL 格式正确"""
    fake_img = {
        "base64": "abc123",
        "mime_type": "image/png",
        "width": 10,
        "height": 10,
        "source": "test.png",
    }
    content = _agent._build_vision_content("分析这张图", [fake_img])

    assert isinstance(content, list)
    assert len(content) == 2
    assert content[0]["type"] == "image_url"
    assert content[0]["image_url"]["url"] == "data:image/png;base64,abc123"
    assert content[1]["type"] == "text"
    assert content[1]["text"] == "分析这张图"


def test_build_vision_content_multiple_images():
    """多张图片时，所有图片均在文字之前"""
    imgs = [
        {"base64": "img1", "mime_type": "image/png", "width": 1, "height": 1, "source": "a.png"},
        {"base64": "img2", "mime_type": "image/jpeg", "width": 1, "height": 1, "source": "b.jpg"},
    ]
    content = _agent._build_vision_content("对比两图", imgs)
    assert len(content) == 3
    assert content[0]["image_url"]["url"] == "data:image/png;base64,img1"
    assert content[1]["image_url"]["url"] == "data:image/jpeg;base64,img2"
    assert content[2]["text"] == "对比两图"


# ── #P0_3 错误恢复闭环 ────────────────────────────────────────────────────────


def test_task_complete_returns_sentinel():
    """task_complete 返回 _task_complete=True，含 success/summary"""
    r = task_complete(True, "all done")
    assert r["_task_complete"] is True
    assert r["success"] is True
    assert r["summary"] == "all done"

    r2 = task_complete(False, "gave up")
    assert r2["_task_complete"] is True
    assert r2["success"] is False
    assert r2["summary"] == "gave up"


def test_err_helper_attaches_error_kind():
    """_err(kind, msg) 返回含 error 和 error_kind"""
    e = _err("permission", "blocked")
    assert e["error"] == "blocked"
    assert e["error_kind"] == "permission"
    assert e["error_kind"] in ERROR_KINDS


def test_err_helper_rejects_unknown_kind():
    """未知 kind 触发 assert"""
    with pytest.raises(AssertionError):
        _err("nonexistent_kind", "x")


def test_error_kind_write_file_path_traversal():
    """路径越界 → permission"""
    r = write_file("../secret.txt", "hack")
    assert "error" in r
    assert r["error_kind"] == "permission"


def test_error_kind_read_file_not_found():
    """读不存在文件 → not_found"""
    r = read_file("nonexistent_file_xyz.txt")
    assert "error" in r
    assert r["error_kind"] == "not_found"


def test_error_kind_delete_file_not_found():
    """删不存在文件 → not_found"""
    r = delete_file("nonexistent_xyz.txt")
    assert "error" in r
    assert r["error_kind"] == "not_found"


def test_error_kind_execute_command_security():
    """黑名单命令 → security"""
    r = execute_command("rm -rf /")
    assert "error" in r
    assert r["error_kind"] == "security"


def test_error_kind_execute_command_python_inline_security():
    """python -c 被列入黑名单 → security（覆盖 pre-existing test_execute_command_timeout 同场景，但断言新字段）"""
    r = execute_command("python -c \"import time; time.sleep(60)\"")
    assert "error" in r
    assert r["error_kind"] == "security"


def test_error_kind_replace_in_file_multiple_matches():
    """old_str 多处匹配 → invalid_args"""
    write_file("multi.txt", "aaa aaa aaa")
    r = replace_in_file("multi.txt", "aaa", "bbb")
    assert "error" in r
    assert r["error_kind"] == "invalid_args"


def test_error_kind_replace_in_file_not_found_string():
    """old_str 未找到 → not_found"""
    write_file("nomatch.txt", "hello")
    r = replace_in_file("nomatch.txt", "nonexistent", "x")
    assert "error" in r
    assert r["error_kind"] == "not_found"


def test_error_kind_move_file_src_not_exist():
    """move src 不存在 → not_found"""
    r = move_file("nonexistent_src.txt", "dst.txt")
    assert "error" in r
    assert r["error_kind"] == "not_found"


def test_error_kind_move_file_path_traversal():
    """move 路径越界 → permission"""
    r = move_file("../outside.txt", "inside.txt")
    assert "error" in r
    assert r["error_kind"] == "permission"


def test_error_field_preserved_for_legacy_callers():
    """老调用方读 result['error'] 仍能拿到原中文文案（兼容性）"""
    r = read_file("does_not_exist.txt")
    assert "不存在" in r["error"]
    # 同时新字段也在
    assert r["error_kind"] == "not_found"


def test_read_file_max_bytes_truncation():
    """max_bytes 截断：写入11字节内容，以 max_bytes=5 读取时内容为前5字节且 truncated=True；
    以 max_bytes=100 读取时不截断（truncated 为 False 或字段不存在）"""
    write_file("mb_test.txt", "hello world")  # 11 字节

    # 截断路径
    result = read_file("mb_test.txt", max_bytes=5)
    assert result["content"] == "hello"
    assert result.get("truncated") is True

    # 不截断路径
    result = read_file("mb_test.txt", max_bytes=100)
    assert result["content"] == "hello world"
    assert result.get("truncated") is not True
