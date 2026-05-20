"""LLM 工具调用 schema 定义（TOOLS 列表），供 agent.py 调用时传给 LLM。"""

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
            "description": "读取workspace目录下的文件，可选按行号区间截取（用于大文件分块读取）",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "文件名（相对于workspace）"},
                    "offset": {"type": "integer", "description": "起始行号（1-based，可选）"},
                    "limit":  {"type": "integer", "description": "读取行数上限（可选）"}
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
            "parameters": {"type": "object", "properties": {}, "required": []}
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
    },
    {
        "type": "function",
        "function": {
            "name": "get_symbol_definition",
            "description": "用 AST 精确查找函数或类的定义，返回所在文件、起始行号、完整代码。比读整个文件更高效，适合定位某个函数/类时使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol_name": {"type": "string", "description": "要查找的函数名或类名"},
                    "file_path": {"type": "string", "description": "指定搜索文件（相对于workspace，可选），不填则搜索整个workspace"}
                },
                "required": ["symbol_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_in_files",
            "description": "在workspace内搜索匹配字符串，返回文件名、行号和匹配内容",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "搜索模式（字符串或正则）"},
                    "regex": {"type": "boolean", "description": "是否使用正则表达式匹配（默认false）"},
                    "extensions": {"type": "array", "items": {"type": "string"}, "description": "文件扩展名过滤列表，如 [\".py\", \".md\"]"}
                },
                "required": ["pattern"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "move_file",
            "description": "移动文件从src到dst（相对于workspace），自动创建目标父目录",
            "parameters": {
                "type": "object",
                "properties": {
                    "src": {"type": "string", "description": "源文件路径（相对于workspace）"},
                    "dst": {"type": "string", "description": "目标文件路径（相对于workspace）"}
                },
                "required": ["src", "dst"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "apply_patch",
            "description": "应用 unified diff 格式的 patch 到文件，比 replace_in_file 更适合多处批量修改",
            "parameters": {
                "type": "object",
                "properties": {
                    "patch_text": {"type": "string", "description": "unified diff 格式的 patch 字符串"},
                    "file_path": {"type": "string", "description": "目标文件路径（相对于workspace，可从 patch 自动推断）"}
                },
                "required": ["patch_text"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_symbols",
            "description": "列出文件中所有函数和类（名称、类型、行号），用于了解文件结构再做精确修改",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "文件路径（相对于workspace）"}
                },
                "required": ["file_path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "replace_symbol",
            "description": "替换指定函数或类的完整实现（用 AST 定位，不依赖字符串精确匹配，比 replace_in_file 更稳）",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol_name": {"type": "string", "description": "要替换的函数名或类名"},
                    "new_code": {"type": "string", "description": "新的完整实现代码"},
                    "file_path": {"type": "string", "description": "文件路径（相对于workspace）"}
                },
                "required": ["symbol_name", "new_code", "file_path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_webpage",
            "description": "读取网页内容，提取正文文本，截断到3000字符。用于查询外部API文档等。",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "要读取的网页URL"}
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_docs",
            "description": "使用 DuckDuckGo 搜索文档，返回前3条结果的标题+摘要+URL",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "append_to_file",
            "description": "向指定文件末尾追加内容",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "文件名（相对于workspace）"},
                    "content": {"type": "string", "description": "要追加的内容"}
                },
                "required": ["filename", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "find_references",
            "description": "在指定目录下递归搜索所有 .py 文件中的符号引用（排除定义行）",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "要查找的符号名称"},
                    "path": {"type": "string", "description": "搜索起始路径（相对于workspace，默认 \".\"）"}
                },
                "required": ["symbol"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "glob_files",
            "description": "按 glob 模式匹配 workspace 内的文件路径，遵循 .gitignore。比 list_files 更灵活，pattern 例：'src/**/*.py'、'*.md'。",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "glob 模式"},
                    "path":    {"type": "string", "description": "搜索起始路径（相对于workspace，默认 \".\"）"}
                },
                "required": ["pattern"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "git_diff",
            "description": "查看 workspace 内的 git diff（未提交改动）。可选 path 限定文件，staged=true 显示已暂存的 diff。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path":   {"type": "string", "description": "可选：仅 diff 该路径"},
                    "staged": {"type": "boolean", "description": "是否查看 --cached（已 add 的部分），默认 false"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "workspace_symbols",
            "description": "扫描 workspace 内所有源文件，返回每个文件的函数和类清单（name/type/line）。用于一次性了解项目整体结构，避免反复 list_symbols。默认只扫 .py。",
            "parameters": {
                "type": "object",
                "properties": {
                    "extensions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "文件扩展名列表，例 [\".py\"]。不传则默认 [\".py\"]。"
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "git_log",
            "description": "查看 workspace 最近的 git 提交（git log --oneline）",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "返回条数，默认 10"}
                },
                "required": []
            }
        }
    }
]


# 审计模式 / 只读人格使用的工具白名单。任何写/执行类工具都不应在此集合内。
READONLY_TOOL_NAMES = {
    "read_file", "list_files", "glob_files", "search_in_files",
    "list_symbols", "get_symbol_definition", "find_references",
    "workspace_symbols",
    "git_diff", "git_log",
    "fetch_webpage", "search_docs",
}
