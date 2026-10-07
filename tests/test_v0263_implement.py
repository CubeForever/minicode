"""v0.26.3 worktree 写权限并行实现(N 选 1 + 零污染验证)。

重点钉死"应用回主仓"这一步——只读版永远不碰主仓库,写权限版独有的
风险点(路径/未跟踪文件/删除传播)。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from minicode.tools.base import ToolContext
from minicode.tools.worktree import WorktreeImplementTool


def _git_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "config", "user.email", "t@t"],
                   cwd=str(tmp_path), check=True)
    subprocess.run(["git", "config", "user.name", "t"],
                   cwd=str(tmp_path), check=True)
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "doomed.py").write_text("dead = True\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=str(tmp_path),
                   check=True)
    return tmp_path


def _ctx(repo: Path, factory) -> ToolContext:
    return ToolContext(cwd=repo, config=None, session=None, ui=None,
                       worktree_factory=factory)


def _tool() -> WorktreeImplementTool:
    return WorktreeImplementTool()


PY = f'"{sys.executable}"'


def _lane_a(prompt, cwd, checkpoints):
    """方案 A:改 VALUE=42,新增 helper.py,删除 doomed.py —— 会通过 check。"""
    cwd = Path(cwd)
    (cwd / "app.py").write_text("VALUE = 42\n", encoding="utf-8")
    (cwd / "helper.py").write_text("def h():\n    return 42\n",
                                   encoding="utf-8")
    (cwd / "doomed.py").unlink()
    assert checkpoints is not None
    return "方案 A 完成"


def _lane_b(prompt, cwd, checkpoints):
    """方案 B:改 VALUE=99 —— check(要求 42)会失败。"""
    (Path(cwd) / "app.py").write_text("VALUE = 99\n", encoding="utf-8")
    return "方案 B 完成"


CHECK_42 = f'{PY} -c "import app; assert app.VALUE == 42; print(\'ok\')"'


def test_winner_applied_with_zero_pollution(tmp_path):
    """N 选 1 应用后,主仓的 M/A/D 三类全传播;存档 patch + __pycache__
    是有意产生的 harness 产物,不算污染。"""
    repo = _git_repo(tmp_path)

    calls = []

    def factory(prompt, cwd, checkpoints):
        calls.append(Path(cwd))
        return _lane_a(prompt, cwd, checkpoints) if len(calls) == 1 \
            else _lane_b(prompt, cwd, checkpoints)

    out = _tool().run({"tasks": ["方案A:42", "方案B:99"], "check": CHECK_42},
                      _ctx(repo, factory))
    # 胜者(方案 A 通过 check)已应用:改/增/删三类全传播
    assert (repo / "app.py").read_text(encoding="utf-8") == "VALUE = 42\n"
    assert (repo / "helper.py").exists()
    assert not (repo / "doomed.py").exists()
    assert "已应用" in out
    st = subprocess.run(["git", "status", "--porcelain"], cwd=str(repo),
                        capture_output=True).stdout.decode()
    lines = [ln for ln in st.splitlines() if ln.strip()]
    # 3 个真实条目 + .minicode/worktree 存档 + __pycache__(有意产生)
    assert len(lines) == 5
    assert "零污染" in out and "不一致" not in out


def test_dirty_repo_refused(tmp_path):
    """写权限版硬约束:主仓有未提交改动即拒绝。"""
    repo = _git_repo(tmp_path)
    (repo / "app.py").write_text("VALUE = 7\n", encoding="utf-8")
    with pytest.raises(Exception) as ei:
        _tool().run({"tasks": ["a", "b"], "check": CHECK_42},
                    _ctx(repo, _lane_a))
    assert "未提交" in str(ei.value)


def test_no_check_refuses(tmp_path):
    """无客观选优依据不自动应用。"""
    repo = _git_repo(tmp_path)
    with pytest.raises(Exception) as ei:
        _tool().run({"tasks": ["a", "b"], "check": ""}, _ctx(repo, _lane_a))
    assert "check" in str(ei.value)


def test_requires_exactly_two_tasks(tmp_path):
    repo = _git_repo(tmp_path)
    for bad in (["only one"], ["a", "b", "c"]):
        with pytest.raises(Exception):
            _tool().run({"tasks": bad, "check": CHECK_42},
                        _ctx(repo, _lane_a))


def test_no_lane_passes_check_applies_nothing(tmp_path):
    """两路都不过 check → 不应用,主仓保持干净。"""
    repo = _git_repo(tmp_path)

    def _snap(tag):
        open("wtdebug.log", "a").write(
            tag + ": __pycache__=" + str((repo / "__pycache__").exists()) + "\n")

    _snap("A git 后")

    def factory(prompt, cwd, checkpoints):
        (Path(cwd) / "app.py").write_text("VALUE = 99\n", encoding="utf-8")
        return "改了但不过 check"

    out = _tool().run({"tasks": ["a", "b"], "check": CHECK_42},
                      _ctx(repo, factory))
    _snap("B 工具后")
    assert "未应用" in out
    assert (repo / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    st = subprocess.run(["git", "status", "--porcelain"], cwd=str(repo),
                        capture_output=True).stdout.decode()
    _snap("C 断言前")
    # __pycache__ 是 check 跑 python 的字节码缓存(运行副产物,不算污染)
    real = [ln for ln in st.splitlines() if "__pycache__" not in ln]
    assert not real                              # 主仓零真实改动
    assert "app.py" not in st                    # 胜者未应用(两路皆败)


def test_lane_creation_failure_surfaces(tmp_path, monkeypatch):
    import minicode.tools.worktree as wt_mod
    repo = _git_repo(tmp_path)
    real = wt_mod._git

    def failing(args, cwd):
        if "worktree" in args and "add" in args:
            import subprocess as sp
            return sp.CompletedProcess(args, 128, b"",
                                       "fatal: 模拟失败".encode("utf-8"))
        return real(args, cwd)

    monkeypatch.setattr(wt_mod, "_git", failing)
    with pytest.raises(Exception) as ei:
        _tool().run({"tasks": ["a", "b"], "check": CHECK_42},
                    _ctx(repo, _lane_a))
    assert "两路均未产出" in str(ei.value) or "模拟" in str(ei.value)


def test_winner_checkpoints_inside_worktree(tmp_path):
    """检查点目录在 worktree 内部(要点 1+8):不被主仓 prune 触碰。"""
    repo = _git_repo(tmp_path)
    seen = {}

    def factory(prompt, cwd, checkpoints):
        seen["ckpt_root"] = str(getattr(checkpoints, "root", ""))
        seen["wt"] = str(cwd)
        return _lane_a(prompt, cwd, checkpoints)

    _tool().run({"tasks": ["a", "b"], "check": CHECK_42},
                _ctx(repo, factory))
    assert seen["wt"] in seen["ckpt_root"]        # 检查点根在 worktree 内
    assert not Path(seen["ckpt_root"]).exists()   # 随 worktree 删除
