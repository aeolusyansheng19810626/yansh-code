"""共享 console 单例 + JSON 模式切换。

`agent.set_batch_mode(json_output=True)` 时通过 `set_json_mode(True)` 把
所有模块的 console 输出重定向到 stderr，避免污染 stdout 上的 task_log JSON。
"""
import sys
from rich.console import Console


class _ConsoleProxy:
    def __init__(self):
        self._inner = Console()

    def set_file(self, file):
        self._inner = Console(file=file)

    def __getattr__(self, name):
        return getattr(self._inner, name)


console = _ConsoleProxy()


def set_json_mode(enabled: bool) -> None:
    console.set_file(sys.stderr if enabled else sys.stdout)
