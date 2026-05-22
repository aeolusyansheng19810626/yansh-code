"""会话级运行时状态封装（P1 #5）

把散落在 agent.py / tools.py 模块级的可变全局状态——`_BATCH_MODE`、
`_PROJECT_TYPE` / `_PROJECT_TEST_CMD`、`_CURRENT_SNAPSHOT`、`_AST_CACHE`
——统一抽象为 `Session` 对象。

设计折中（渐进式重构而非大爆炸）：
  - 模块级变量保留作为运行期事实存储，不破坏现有读写路径
  - `Session` 提供 `snapshot()` / `restore()` 实现 push/pop，配合 context manager 让单测可以
    `with scoped_session(tmp_path):` 隔离一个任务的状态
  - `reset()` 一行清零，给 pytest fixture 用

后续若想把状态完全迁出去，只需逐个文件改读写到 `current()` 上，模块级变量退场即可。
本次不做：保持兼容并先把"测试可隔离"这个最痛的点解决。
"""
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Session:
    """跨 agent / tools 的会话级状态镜像。
    字段名与对应模块级变量一致（去掉前导下划线）。
    """
    workspace_dir: Optional[str] = None
    batch_mode: bool = False
    batch_strict: bool = False
    project_type: Optional[str] = None
    project_test_cmd: Optional[str] = None
    current_snapshot: Optional[dict] = None
    # P2 #7：Plan Mode 会话级状态
    plan_mode: bool = False
    plan_draft: str = ""
    plan_history: list = field(default_factory=list)
    # P4-3：把 agent.py 的 _ACTIVE_* / _SUBAGENT_STATS 一并镜像，
    # 让单测 scoped_session 能彻底隔离（之前一个 test 加载的 memory 索引
    # 会被下一个 test 看到）
    active_skills_prompt: str = ""
    active_memory_index: str = ""
    subagent_stats: dict = field(default_factory=dict)
    # AST 缓存按引用持有 tools._AST_CACHE（不复制）；只在 reset 时清空
    _ast_cache_ref: Optional[dict] = field(default=None, repr=False)

    # ---------- 与模块级变量同步 ----------

    def pull(self) -> "Session":
        """从 agent.py / tools.py 拉一份当前模块状态进 Session（用于 snapshot）"""
        import agent as _a
        import tools as _t
        import config as _cfg
        self.workspace_dir = _cfg.WORKSPACE_DIR
        self.batch_mode = bool(getattr(_a, "_BATCH_MODE", False))
        self.batch_strict = bool(getattr(_t, "_BATCH_STRICT", False))
        self.project_type = getattr(_a, "_PROJECT_TYPE", None)
        self.project_test_cmd = getattr(_a, "_PROJECT_TEST_CMD", None)
        self.current_snapshot = getattr(_a, "_CURRENT_SNAPSHOT", None)
        self.plan_mode = bool(getattr(_a, "_PLAN_MODE", False))
        self.plan_draft = str(getattr(_a, "_PLAN_DRAFT", "") or "")
        self.plan_history = list(getattr(_a, "_PLAN_HISTORY", []) or [])
        self.active_skills_prompt = str(getattr(_a, "_ACTIVE_SKILLS_PROMPT", "") or "")
        self.active_memory_index = str(getattr(_a, "_ACTIVE_MEMORY_INDEX", "") or "")
        self.subagent_stats = dict(getattr(_a, "_SUBAGENT_STATS", {}) or {})
        self._ast_cache_ref = getattr(_t, "_AST_CACHE", None)
        return self

    def push(self) -> None:
        """把 Session 当前值写回模块级变量（用于 restore 或测试 setup）"""
        import agent as _a
        import tools as _t
        import config as _cfg
        if self.workspace_dir is not None:
            _cfg.set_workspace_dir(self.workspace_dir)
            _a._reinit_paths()
            _t._reinit_paths()
        _a._BATCH_MODE = self.batch_mode
        _t._BATCH_MODE = self.batch_mode
        _t._BATCH_STRICT = self.batch_strict
        _a._PROJECT_TYPE = self.project_type
        _a._PROJECT_TEST_CMD = self.project_test_cmd
        _a._CURRENT_SNAPSHOT = self.current_snapshot
        _a._PLAN_MODE = self.plan_mode
        _a._PLAN_DRAFT = self.plan_draft
        _a._PLAN_HISTORY = list(self.plan_history)
        _a._ACTIVE_SKILLS_PROMPT = self.active_skills_prompt
        _a._ACTIVE_MEMORY_INDEX = self.active_memory_index
        # _SUBAGENT_STATS 是个 dict——更新内容而非替换引用，免得别处持有的引用过时
        if hasattr(_a, "_SUBAGENT_STATS"):
            _a._SUBAGENT_STATS.clear()
            _a._SUBAGENT_STATS.update(self.subagent_stats)

    # ---------- 测试便利 ----------

    def reset(self, workspace_dir: Optional[str] = None) -> None:
        """清零所有可变状态（含 AST 缓存）。给单测 fixture 用，避免 reload(tools)。"""
        self.batch_mode = False
        self.batch_strict = False
        self.project_type = None
        self.project_test_cmd = None
        self.current_snapshot = None
        self.plan_mode = False
        self.plan_draft = ""
        self.plan_history = []
        self.active_skills_prompt = ""
        self.active_memory_index = ""
        self.subagent_stats = {
            "calls": 0, "total_steps": 0, "last_task": "",
            "last_summary": "", "last_role": "", "last_steps": 0,
            "last_success": False,
        }
        if workspace_dir is not None:
            self.workspace_dir = workspace_dir
        self.push()
        # _AST_CACHE 是按 mtime 索引的、生命周期同 process 的纯缓存，clear 安全
        if self._ast_cache_ref is None:
            import tools as _t
            self._ast_cache_ref = getattr(_t, "_AST_CACHE", None)
        if self._ast_cache_ref is not None:
            self._ast_cache_ref.clear()


@contextmanager
def scoped_session(workspace_dir: Optional[str] = None):
    """单测/集成验证用的 push/pop 上下文管理器。
    进入时拍快照，退出时恢复——避免一个测试污染下一个。

    用法：
        with scoped_session(tmp_path):
            ...  # 这里 batch_mode/project_type 等均为干净初值
    """
    snap = Session().pull()
    try:
        Session(workspace_dir=str(workspace_dir) if workspace_dir else None).reset(
            workspace_dir=str(workspace_dir) if workspace_dir else None
        )
        yield
    finally:
        snap.push()


# 兼容入口：模块级 current() 返回当前模块状态的只读快照
def current() -> Session:
    return Session().pull()
