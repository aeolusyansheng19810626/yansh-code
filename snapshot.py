"""任务级快照（方案 A：纯文件复制，不动用户的 git 状态）

任务开始时仅备份计划中已存在的文件作为 baseline；任务过程中由
_backup_file_if_needed() 在 LLM 写入前增量补充。回滚时按 meta.files
还原内容；任务期间新增的文件按 workspace_files 差集删除。
"""
import os
import json
import shutil
from datetime import datetime
from pathlib import Path

from console_shared import console
import config as _cfg_mod

# 快照/回滚时需要跳过的目录
_SNAPSHOT_IGNORE_DIRS = {".git", ".yansh", "__pycache__", "venv", "node_modules", ".pytest_cache"}

_SNAPSHOT_DIR = Path(_cfg_mod.WORKSPACE_DIR) / ".yansh" / "snapshots"


def _reinit_paths():
    """--cwd 变更后由 agent._reinit_paths() 调用"""
    global _SNAPSHOT_DIR
    _SNAPSHOT_DIR = Path(_cfg_mod.WORKSPACE_DIR) / ".yansh" / "snapshots"


def _should_skip_dir(root: str) -> bool:
    parts = set(Path(root).parts)
    return bool(parts & _SNAPSHOT_IGNORE_DIRS)


def create_snapshot(file_list):
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    ws = _cfg_mod.WORKSPACE_DIR
    snap_dir = _SNAPSHOT_DIR / timestamp
    snap_dir.mkdir(parents=True, exist_ok=True)

    workspace_files = []
    for root, dirs, files in os.walk(ws):
        if _should_skip_dir(root):
            dirs.clear()
            continue
        for filename in files:
            rel_path = os.path.relpath(os.path.join(root, filename), ws)
            workspace_files.append(rel_path.replace("\\", "/"))

    backed = []
    for filename in file_list:
        src = Path(ws) / filename
        if src.exists() and src.is_file():
            dst = snap_dir / filename
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), str(dst))
            backed.append(filename)
    (snap_dir / "meta.json").write_text(
        json.dumps({"files": backed, "workspace_files": workspace_files, "timestamp": timestamp},
                   ensure_ascii=False), encoding="utf-8"
    )
    console.print(f"[快照] {snap_dir.name} (baseline {len(backed)} 文件)", highlight=False)
    return {"mode": "file", "path": str(snap_dir), "timestamp": timestamp}


def _backup_file_if_needed(snap_info, filename):
    """LLM 写入前增量备份：若快照中尚无此文件且当前文件存在则备份；
    若文件原本不存在，仅在 meta.json 中标记，使回滚时能删除这个新文件。"""
    if not snap_info or not isinstance(snap_info, dict) or snap_info.get("mode") != "file":
        return
    if not filename:
        return
    snap_dir = Path(snap_info["path"])
    target = snap_dir / filename
    if target.exists():
        return  # 已备份过
    src = Path(_cfg_mod.WORKSPACE_DIR) / filename
    meta_file = snap_dir / "meta.json"
    try:
        meta = json.loads(meta_file.read_text(encoding="utf-8")) if meta_file.exists() else {}
    except Exception:
        meta = {}

    if src.exists() and src.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(target))
        if filename not in meta.get("files", []):
            meta.setdefault("files", []).append(filename)
            meta_file.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")


def restore_snapshot(snap_info):
    """根据快照恢复工作区，返回恢复数量。"""
    if not snap_info:
        return 0
    if isinstance(snap_info, Path):
        return _restore_file_snapshot(snap_info)
    if isinstance(snap_info, dict) and snap_info.get("mode") == "file":
        return _restore_file_snapshot(Path(snap_info["path"]))
    return 0


def _restore_file_snapshot(snap_dir: Path) -> int:
    ws = _cfg_mod.WORKSPACE_DIR
    meta_file = snap_dir / "meta.json"
    if not meta_file.exists():
        return 0
    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    restored = 0
    for filename in meta.get("files", []):
        src = snap_dir / filename
        dst = Path(ws) / filename
        if src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), str(dst))
            restored += 1
    workspace_files_then = set(meta.get("workspace_files", []))
    current_files = []
    for root, dirs, files in os.walk(ws):
        if _should_skip_dir(root):
            dirs.clear()
            continue
        for filename in files:
            rel_path = os.path.relpath(os.path.join(root, filename), ws)
            current_files.append(rel_path.replace("\\", "/"))
    for f in current_files:
        if f not in workspace_files_then:
            path = Path(ws) / f
            try:
                if path.exists():
                    path.unlink()
            except Exception as e:
                console.print(f"[警告] 回滚时无法删除 {f}: {e}", style="yellow", highlight=False)
    return restored


def cleanup_snapshot(snap_info):
    if not snap_info:
        return
    snap_dir = None
    if isinstance(snap_info, dict) and snap_info.get("mode") == "file":
        snap_dir = Path(snap_info["path"])
    elif isinstance(snap_info, Path):
        snap_dir = snap_info
    if snap_dir and snap_dir.exists():
        shutil.rmtree(str(snap_dir))


def _gc_old_snapshots(keep=10):
    """保留最近 keep 个快照目录，更老的删除（防止 .yansh/snapshots 无限增长）"""
    if not _SNAPSHOT_DIR.exists():
        return
    candidates = sorted(
        (s for s in _SNAPSHOT_DIR.iterdir() if s.is_dir()),
        key=lambda p: p.name,
        reverse=True
    )
    for old in candidates[keep:]:
        try:
            shutil.rmtree(str(old))
        except Exception:
            pass


def get_latest_snapshot():
    """返回最新快照目录，不存在返回 None"""
    if not _SNAPSHOT_DIR.exists():
        return None
    candidates = sorted(
        (s for s in _SNAPSHOT_DIR.iterdir() if s.is_dir() and (s / "meta.json").exists()),
        reverse=True
    )
    if not candidates:
        return None
    s = candidates[0]
    return {"mode": "file", "path": str(s), "timestamp": s.name}
