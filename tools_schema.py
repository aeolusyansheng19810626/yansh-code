"""LLM 工具调用 schema 定义（TOOLS 列表），供 agent.py 调用时传给 LLM。"""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "在workspace目录下写入文件（覆盖整个文件）。如果文件已存在且只改局部，应优先用 replace_in_file 或 replace_symbol——整体重写容易丢失现有上下文。",
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
            "description": "读取workspace目录下的文件，可选按行号区间截取（用于大文件分块读取）。注意：刚通过 write_file/replace_in_file/replace_symbol 修改过的文件不要再 read 验证——写工具失败会直接返回错误。优先用 search_in_files / list_symbols 定位再精读区间，而不是整文件 read。",
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
            "description": "在workspace目录下执行命令（30秒超时）。查询多个相关 env 变量或运行多个独立命令时，用 `;`（PowerShell）或 `&&`（bash）合并到一次调用，不要拆成多次。例：`$env:A; $env:B; $env:C` 一次拿三个值。",
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
            "name": "delete_file",
            "description": "删除 workspace 目录下的文件。破坏性操作：仅在用户明确要求删除文件时使用；普通的清理代码场景应用 replace_in_file 删除内容而非整个文件。",
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
            "description": "返回项目符号清单（函数/类 name/type/line），分层模式默认只看顶层避免撑爆 context。**默认（不传 path/recursive）只列顶层文件 + 子目录摘要**（py_files/total_symbols 计数）；传 path 下钻该目录顶层；只在小项目或确实需要全树时传 recursive=true。默认只扫 .py。",
            "parameters": {
                "type": "object",
                "properties": {
                    "extensions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "文件扩展名列表，例 [\".py\"]。不传则默认 [\".py\"]。"
                    },
                    "path": {
                        "type": "string",
                        "description": "可选：相对 workspace 的子目录，下钻查看该目录顶层（不递归子树）。不传则查 workspace 根。"
                    },
                    "recursive": {
                        "type": "boolean",
                        "description": "可选：true=递归扫子树（旧全量行为，大项目慎用）；默认 false（只看一层）。"
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "directory_summary",
            "description": "返回某目录的整体感知摘要：文件数、扩展名分布、关键文件（README/pyproject 等）、直接子目录、文件名采样。不递归。用于在大项目里快速了解某目录是干啥的——比 list_files 更高信息密度。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "相对 workspace 的目录路径，默认 '.'（workspace 根）。"
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
    },
    {
        "type": "function",
        "function": {
            "name": "dispatch_subagent",
            "description": "派发一个独立子 agent 跑子任务，返回 summary 字符串。**核心价值：context 隔离**——子 agent 烧自己的 context 跑探索，主 agent 只看到一段总结。\n\n用法场景：\n- 大型代码库探索（'查 X 模块怎么用的，列出所有调用点'）\n- 重活：跑测试 + 修复 + 复测整套（role='general'）\n- **多分支并行调研**——一次 response 里发多个 dispatch_subagent tool_call，会**并发跑**（最多 4 个同时），总耗时≈max(单个) 而不是 sum。例：分析 A/B/C 三个模块怎么用，一次发 3 个 dispatch_subagent 比串行查快 3×。\n\n约束：\n- 子 agent **不能再派子 agent**（递归被禁，避免失控）\n- 子 agent 的工具集按 role 限定\n- max_steps 上限 16，超出即截断\n\n何时不要用：单文件读 / 一次 grep 这种简单任务直接调底层工具更便宜——dispatch_subagent 多一次 LLM cascade 起步就 1k+ token。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "给子 agent 的任务描述（自然语言，自包含——子 agent 看不到主 agent 上下文）"
                    },
                    "role": {
                        "type": "string",
                        "enum": ["explorer", "general", "auditor"],
                        "description": "explorer=只读探索（默认，最常用，最便宜）；general=全工具（能写文件/跑命令）；auditor=只读审计（同 explorer）"
                    },
                    "max_steps": {
                        "type": "integer",
                        "description": "子 agent 最多跑几轮 LLM 循环，默认 8，上限 16"
                    }
                },
                "required": ["task"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "task_complete",
            "description": "显式声明本次任务结束。**每次任务都必须以此工具收尾**（fix/audit loop 识别后退出）。完成时 task_complete(success=true, summary='做了什么')；确认无法继续时 task_complete(success=false, summary='为什么放弃')。不要沉默退出——没调任何工具时 loop 会再追问一次浪费一轮。",
            "parameters": {
                "type": "object",
                "properties": {
                    "success": {"type": "boolean", "description": "true=任务完成；false=主动放弃"},
                    "summary": {"type": "string", "description": "一句话说明做了什么或为什么放弃"}
                },
                "required": ["success", "summary"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "update_plan_draft",
            "description": "[Plan Mode 专用] 写入/更新当前 plan 草稿（markdown）。每次想沉淀方案就调一次——后续轮次会拿到最新草稿。建议结构：## 目标 / ## 步骤 / ## 关键文件 / ## 风险与权衡。多次调用会**整体替换**草稿（不是追加），所以每次给完整版本。",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "完整 plan 草稿（markdown）"}
                },
                "required": ["content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "exit_plan_mode_signal",
            "description": "[Plan Mode 专用] 当前轮探索/写草稿告一段落、可请用户审阅时调用。**不会真正退出 Plan Mode**——用户可继续追问、补充需求或要求修改方案；批准必须由用户用 /approve 触发。一轮结束时调一次即可。",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "description": "可选：本轮做了什么、为什么觉得到位了"}
                },
                "required": []
            }
        }
    }
]


# 审计模式 / 只读人格使用的工具白名单。任何写/执行类工具都不应在此集合内。
# task_complete 也加入——audit 流程也要靠它显式收尾。
READONLY_TOOL_NAMES = {
    "read_file", "list_files", "glob_files", "search_in_files",
    "list_symbols", "get_symbol_definition", "find_references",
    "workspace_symbols", "directory_summary",
    "git_diff", "git_log",
    "fetch_webpage", "search_docs",
    "task_complete",
    "update_plan_draft", "exit_plan_mode_signal",
    # P2 #9：派子 agent 不直接修改 workspace（子 agent role=explorer 时也是只读），
    # 所以加进 READONLY 让 audit/plan 也能用。子 agent 内部不再看到这个工具（递归禁用）。
    "dispatch_subagent",
}
