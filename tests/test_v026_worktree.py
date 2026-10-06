"""v0.26 worktree 并行探索(只读第一版)。

覆盖:worktree 创建与清理、子代理 cwd 隔离、非 git/无 HEAD 报错、
任务数边界、部分失败保留成功结果、factory cwd 透传。
"""
import subprocess
from pathlib import Path

import pytest

from minicode.tools.base import ToolContext
from minicode.tools.worktree import WorktreeExploreTool

REPORT_MD = """# 红队 precision 标注 — 占位

## 红队报告
"""


def _git_repo(tmp_path: Path) -> Path:
    """带一次提交的最小 git 仓库。"""
    subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(tmp_path),
                   check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(tmp_path),
                   check=True)
    (tmp_path / "app.py").write_text("print('v1')\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=str(tmp_path),
                   check=True)
    return tmp_path


def _ctx(repo: Path, factory) -> ToolContext:
    return ToolContext(cwd=repo, config=None, session=None, ui=None,
                       agent_factory=factory)


def _tool() -> WorktreeExploreTool:
    return WorktreeExploreTool()


def test_explore_creates_isolated_worktrees_and_cleans(tmp_path):
    seen_cwds = []

    def factory(prompt, cwd=None):
        seen_cwds.append(Path(cwd))
        assert (Path(cwd) / "app.py").exists()   # worktree 里有提交内容
        return f"报告: {Path(cwd).name}"

    repo = _git_repo(tmp_path)
    out = _tool().run({"tasks": ["查并发问题", "查注入面"]},
                      _ctx(repo, factory))
    assert len(seen_cwds) == 2                    # 两路各自独立 worktree
    assert seen_cwds[0] != seen_cwds[1]
    assert "查并发问题" in out and "查注入面" in out
    assert "2/2 路完成" in out
    # worktree 已清理
    wt_list = subprocess.run(["git", "worktree", "list", "--porcelain"],
                             cwd=str(repo), capture_output=True).stdout
    assert wt_list.decode("utf-8").count("worktree ") == 1   # 只剩主工作区
    assert not any(p.name.startswith("minicode-wt")
                   for p in tmp_path.iterdir())


def test_explore_requires_git_repo(tmp_path):
    with pytest.raises(Exception) as ei:
        _tool().run({"tasks": ["a", "b"]}, _ctx(tmp_path, lambda p, cwd=None: ""))
    assert "git" in str(ei.value)


def test_explore_requires_head(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
    with pytest.raises(Exception) as ei:
        _tool().run({"tasks": ["a", "b"]}, _ctx(tmp_path,
                                                lambda p, cwd=None: ""))
    assert "HEAD" in str(ei.value)


def test_task_count_bounds(tmp_path):
    repo = _git_repo(tmp_path)
    ctx = _ctx(repo, lambda p, cwd=None: "")
    with pytest.raises(Exception) as ei:
        _tool().run({"tasks": ["只有一条"]}, ctx)
    assert "2-4" in str(ei.value)
    with pytest.raises(Exception):
        _tool().run({"tasks": [str(i) for i in range(5)]}, ctx)


def test_worktree_add_failure_raises(tmp_path, monkeypatch):
    import minicode.tools.worktree as wt_mod
    repo = _git_repo(tmp_path)
    calls = {"n": 0}
    real = wt_mod._git

    def failing_git(args, cwd):
        if "worktree" in args and "add" in args:
            calls["n"] += 1
            import subprocess as sp
            return sp.CompletedProcess(args, 128,
                                       b"", "fatal: 模拟创建失败".encode("utf-8"))
        return real(args, cwd)

    monkeypatch.setattr(wt_mod, "_git", failing_git)
    with pytest.raises(Exception) as ei:
        _tool().run({"tasks": ["a", "b"]}, _ctx(repo,
                                                lambda p, cwd=None: ""))
    assert "创建失败" in str(ei.value) or "模拟" in str(ei.value)


def test_partial_failure_keeps_successful_results(tmp_path, monkeypatch):
    import minicode.tools.worktree as wt_mod
    repo = _git_repo(tmp_path)
    real = wt_mod._git
    state = {"n": 0}

    def half_fail_git(args, cwd):
        if "worktree" in args and "add" in args:
            state["n"] += 1
            if state["n"] == 2:   # 第二路创建失败
                import subprocess as sp
                return sp.CompletedProcess(args, 128, b"",
                                           "fatal: 模拟失败".encode("utf-8"))
        return real(args, cwd)

    monkeypatch.setattr(wt_mod, "_git", half_fail_git)
    seen = []

    def factory(prompt, cwd=None):
        seen.append(Path(cwd))
        return f"路报告-{len(seen)}"

    out = _tool().run({"tasks": ["成功路", "失败路", "成功路2"]},
                      _ctx(repo, factory))
    assert "路报告-1" in out and "失败的路" in out   # 成功结果保留 + 失败说明
