"""fixplan 阶段1：A 环境卡 / B compact env_anchor / 快赢（deny 一致化、agent_state）。
对应 notes/shadow/2026-06-11_05。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import agent
import config
import tools


def _setup_ws(tmp_path):
    config.set_workspace_dir(str(tmp_path))
    tools._reinit_paths()
    agent._reinit_paths()


# ── 快赢1：deny python -c 一致化（也拦 python3 -c）──

def test_deny_python_c_blocked():
    assert tools._check_dangerous('python -c "print(1)"') is not None


def test_deny_python3_c_now_blocked():
    """一致化：原放行 python3 -c（零安全收益），现也拦。"""
    assert tools._check_dangerous('python3 -c "print(1)"') is not None
    assert tools._check_dangerous('python3.11 -c "x"') is not None


def test_deny_python_m_not_blocked():
    """python -m / pytest 不应被误拦。"""
    assert tools._check_dangerous("python -m miniql data") is None
    assert tools._check_dangerous("pytest -q") is None


# ── 快赢2：agent_state 不被 gate 的 pytest collected-0 污染 ──

def test_agent_state_skips_pytest_exit5(tmp_path):
    _setup_ws(tmp_path)
    tools._update_agent_state("pytest -q", 5)
    sp = tmp_path / ".yansh" / "agent_state.md"
    assert (not sp.exists()) or ("pytest -q" not in sp.read_text(encoding="utf-8"))


def test_agent_state_records_pytest_real_failure(tmp_path):
    """pytest rc=1（真失败）仍记录。"""
    _setup_ws(tmp_path)
    tools._update_agent_state("pytest -q", 1)
    sp = tmp_path / ".yansh" / "agent_state.md"
    assert "pytest -q" in sp.read_text(encoding="utf-8")


# ── 快赢3：agent_state 记录 cd 复合命令 ──

def test_state_cmd_re_matches_cd_compound():
    assert tools._STATE_CMD_RE.match("cd /c/ws && python foo.py")
    assert tools._STATE_CMD_RE.match("cd ws && pytest -q")
    assert tools._STATE_CMD_RE.match("python -m miniql")
    assert not tools._STATE_CMD_RE.match("ls -la")


def test_agent_state_records_cd_compound(tmp_path):
    _setup_ws(tmp_path)
    tools._update_agent_state("cd ws && python build.py", 1)
    sp = tmp_path / ".yansh" / "agent_state.md"
    assert "cd ws && python build.py" in sp.read_text(encoding="utf-8")


# ── A：环境契约 ──

def test_build_env_contract_contains_essentials(tmp_path, monkeypatch):
    _setup_ws(tmp_path)
    monkeypatch.setattr(agent, "_detect_interpreter", lambda: ("python", "Python 3.11.9"))
    c = agent._build_env_contract()
    assert str(tmp_path) in c
    assert "不需要 cd" in c
    assert "python3" in c  # 明确禁用 python3
    assert "编码" in c


def test_seed_env_state_idempotent(tmp_path):
    _setup_ws(tmp_path)
    contract = "[环境契约 — 测试]\n- cwd: x"
    agent._seed_env_state(contract)
    sp = tmp_path / ".yansh" / "agent_state.md"
    first = sp.read_text(encoding="utf-8")
    assert "## 环境契约" in first
    agent._seed_env_state(contract)  # 再次不重复写
    assert sp.read_text(encoding="utf-8") == first


# ── B：env_anchor 跨 compact 重注入 ──

def test_compact_reinjects_env_anchor(monkeypatch):
    monkeypatch.setattr(agent, "_summarize_old_history", lambda text: "摘要")
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "u3"},
        {"role": "assistant", "content": "a3"},
    ]
    out = agent._compact_messages(msgs, keep_recent_pairs=1, env_anchor="ENV-CONTRACT-XYZ")
    joined = "".join(m.get("content", "") for m in out if isinstance(m.get("content"), str))
    assert "ENV-CONTRACT-XYZ" in joined
    assert "环境契约锚点" in joined


def test_make_compact_state_has_env_anchor():
    st = agent._make_compact_state()
    assert "env_anchor" in st
    assert st["env_anchor"] is None
