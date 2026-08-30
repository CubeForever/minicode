import json
import threading
import types
from pathlib import Path

import pytest

from minicode.agent import Agent
from minicode.checkpoints import CheckpointManager
from minicode.config import Config
from minicode.fake import FakeProvider
from minicode.guard import bash_guard_reason, file_guard_reason
from minicode.session import Session
from minicode.tools import build_registry
from minicode.tools.base import ToolContext
from minicode.tools.panel import ConsultPanelTool
from minicode.tools.shell import ShellState
from minicode.ui import UI


def make_agent(tmp_path, provider, mode="yolo", ui=None, verify_command=None) -> Agent:
    cfg = Config(provider="fake", api_key="", model="fake", mode=mode,
                 verify_command=verify_command or "")
    cfg.cwd = tmp_path
    session = Session()
    agent = Agent(provider, session, ui or UI(), cfg,
                  build_registry(ShellState(tmp_path, "bash")),
                  checkpoints=CheckpointManager(tmp_path / "ck"))
    agent.system_prompt = "test"
    return agent


# ---------- 敏感路径门禁 ----------

def test_file_guard_reasons(tmp_path):
    assert file_guard_reason(tmp_path / ".env", tmp_path) is not None
    assert file_guard_reason(tmp_path / ".env.local", tmp_path) is not None
    assert file_guard_reason(tmp_path / ".git" / "config", tmp_path) is not None
    assert file_guard_reason(tmp_path / "server.pem", tmp_path) is not None
    assert file_guard_reason(tmp_path / "id_rsa", tmp_path) is not None
    assert file_guard_reason(Path("C:/elsewhere/x.txt"), tmp_path) is not None
    assert file_guard_reason(tmp_path / "normal.py", tmp_path) is None
    assert file_guard_reason(tmp_path / "env.txt", tmp_path) is None  # not .env


def test_bash_guard_reasons():
    assert bash_guard_reason("rm -rf .git") is not None
    assert bash_guard_reason("echo x > .env") is not None
    assert bash_guard_reason("git checkout .env") is not None
    assert bash_guard_reason("cat .env") is None            # reading allowed
    assert bash_guard_reason("git status") is None          # no sensitive path
    assert bash_guard_reason("echo hi > out.txt") is None   # no sensitive path



def test_bash_guard_ignores_benign_fd_redirections():
    cmd = ('cd "D:\proj" && ls -la && echo "---PYTHON---" && python --version 2>&1 '
           '&& echo "---FILES---" && find . -type f -not -path "./.git/*" '
           '2>/dev/null | head -50')
    assert bash_guard_reason(cmd) is None          # 实录中的误报命令
    assert bash_guard_reason("grep x .git/config 2>/dev/null") is None
    assert bash_guard_reason("rm -rf .git 2>/dev/null") is not None
    assert bash_guard_reason("echo x > .env 2>&1") is not None  # 真写入仍拦截


def test_guard_blocks_even_in_yolo_noninteractive(tmp_path):
    # under pytest stdin is not a tty -> yolo + sensitive path = auto-decline
    script = [
        {"tool_calls": [{"id": "t1", "name": "write_file",
                         "args": json.dumps({"path": ".env", "content": "K=1"})}]},
        {"text": "done"},
    ]
    agent = make_agent(tmp_path, FakeProvider(script), mode="yolo")
    agent.run_turn("write env")
    assert not (tmp_path / ".env").exists()
    assert "敏感路径保护" in agent.session.messages[2]["content"]


def test_guard_allows_normal_files_in_yolo(tmp_path):
    script = [
        {"tool_calls": [{"id": "t1", "name": "write_file",
                         "args": json.dumps({"path": "ok.txt", "content": "fine"})}]},
        {"text": "done"},
    ]
    agent = make_agent(tmp_path, FakeProvider(script), mode="yolo")
    agent.run_turn("write")
    assert (tmp_path / "ok.txt").exists()


def test_guard_asks_interactively_when_tty(tmp_path, monkeypatch):
    class TTYUI(UI):
        def __init__(self):
            super().__init__()
            self.asked = 0
            self.answer = "y"
        def confirm(self, title, preview=None):
            self.asked += 1
            return self.answer
    ui = TTYUI()
    fake_sys = types.SimpleNamespace(stdin=types.SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("minicode.agent.sys", fake_sys)
    script = [
        {"tool_calls": [{"id": "t1", "name": "write_file",
                         "args": json.dumps({"path": ".env", "content": "K=1"})}]},
        {"text": "done"},
    ]
    agent = make_agent(tmp_path, FakeProvider(script), mode="yolo", ui=ui)
    agent.run_turn("write env")
    assert ui.asked == 1  # forced confirmation even in yolo
    assert (tmp_path / ".env").exists()  # user said yes


def test_guard_blocks_bash_touching_git(tmp_path):
    script = [
        {"tool_calls": [{"id": "t1", "name": "bash",
                         "args": json.dumps({"command": "rm -rf .git"})}]},
        {"text": "done"},
    ]
    agent = make_agent(tmp_path, FakeProvider(script), mode="yolo")
    agent.run_turn("nuke git")
    assert "敏感路径保护" in agent.session.messages[2]["content"]


# ---------- Plan 持久化 ----------

def test_plan_save_list_mark_inject(tmp_path):
    from minicode import plans
    p = plans.save_plan(tmp_path, "1. 修 A\n2. 改 B")
    assert p.exists() and "in-progress" in p.read_text(encoding="utf-8")
    rows = plans.list_plans(tmp_path)
    assert rows[0][2] == "in-progress" and "修 A" in rows[0][3]
    active = plans.latest_active_plan(tmp_path)
    assert "改 B" in active
    plans.mark_plan(tmp_path, 1, "done")
    assert plans.latest_active_plan(tmp_path) is None
    assert plans.list_plans(tmp_path)[0][2] == "done"
    # active plan injected into system prompt
    from minicode.prompts import build_system_prompt
    plans.save_plan(tmp_path, "新计划：重构登录")
    cfg = Config(provider="fake", api_key="", model="fake")
    cfg.cwd = tmp_path
    prompt = build_system_prompt(cfg, tmp_path)
    assert "重构登录" in prompt and "Active plan" in prompt


# ---------- 会话复盘自动入脑 ----------

def test_postmortem_writes_brain_on_repeated_failures(tmp_path):
    from minicode import cli
    from minicode.tools.memory import load_brain_text
    agent = make_agent(tmp_path, FakeProvider([{"text": "x"}]))
    agent.session.add({"role": "user", "content": "go"})
    agent.session.add({"role": "tool", "tool_call_id": "t1", "name": "bash",
                       "content": "Error: boom 1", "is_error": True})
    agent.session.add({"role": "tool", "tool_call_id": "t2", "name": "bash",
                       "content": "Error: boom 2", "is_error": True})
    cli._postmortem(agent, "ok")
    assert "复盘" in load_brain_text(tmp_path)
    assert "boom 1" in load_brain_text(tmp_path)


def test_postmortem_single_failure_no_note(tmp_path):
    from minicode import cli
    from minicode.tools.memory import load_brain_text
    agent = make_agent(tmp_path, FakeProvider([{"text": "x"}]))
    agent.session.add({"role": "user", "content": "go"})
    agent.session.add({"role": "tool", "tool_call_id": "t1", "name": "bash",
                       "content": "Error: once", "is_error": True})
    cli._postmortem(agent, "ok")
    assert load_brain_text(tmp_path) == ""


def test_postmortem_model_error_writes_brain(tmp_path):
    from minicode import cli
    from minicode.tools.memory import load_brain_text
    agent = make_agent(tmp_path, FakeProvider([{"text": "x"}]))
    agent.session.add({"role": "user", "content": "go"})
    cli._postmortem(agent, "error")
    assert "模型调用异常" in load_brain_text(tmp_path)


# ---------- /commit prompt 构造 ----------

def test_commit_prompt_includes_session_files(tmp_path):
    from minicode import cli
    script = [{"text": "committed"}]
    agent = make_agent(tmp_path, FakeProvider(script))
    agent.checkpoints.snapshot(tmp_path / "changed.py", "write_file")
    cli._git_commit(agent, agent.ui, "附带说明")
    first_user = agent.session.messages[0]["content"]
    assert "Conventional Commits" in first_user
    assert "changed.py" in first_user
    assert "附带说明" in first_user


# ---------- 圆桌：并行 + 辩论 ----------

def test_panel_debate_mode(tmp_path):
    calls = []
    lock = threading.Lock()

    def factory(prompt, subagent_type=None):
        with lock:
            calls.append(prompt)
        return f"报告{len(calls)}"

    ctx = ToolContext(cwd=tmp_path, config=Config(), session=Session(),
                      ui=UI(), agent_factory=factory)
    r = ConsultPanelTool().run({"question": "选 A 还是 B？", "debate": True}, ctx)
    assert len(calls) == 6  # 3 perspectives + 3 rebuttals
    assert "辩论后终稿" in r
    # rebuttal prompts include opposing views
    assert any("其他视角的发言" in p for p in calls[3:])


def test_panel_parallel_single_round(tmp_path):
    calls = []
    lock = threading.Lock()

    def factory(prompt, subagent_type=None):
        with lock:
            calls.append(prompt)
        return "r"

    ctx = ToolContext(cwd=tmp_path, config=Config(), session=Session(),
                      ui=UI(), agent_factory=factory)
    r = ConsultPanelTool().run({"question": "q"}, ctx)
    assert len(calls) == 3
    assert "辩论后终稿" not in r


# ---------- 结构化压缩 ----------

def test_structured_compaction(tmp_path):
    agent = make_agent(tmp_path, FakeProvider([{"text": "SUMMARY"}]))
    agent.session.add({"role": "user", "content": "开工"})
    agent.session.add({"role": "assistant", "content": None,
                       "tool_calls": [{"id": "t", "name": "bash",
                                       "args": json.dumps({"command": "pytest -q"})}]})
    agent.checkpoints.snapshot(tmp_path / "a.py", "edit_file")
    agent.session.todos = [{"content": "收尾", "status": "pending"}]
    agent.compact()
    ctx_msg = agent.session.messages[0]["content"]
    assert "本会话修改过的文件" in ctx_msg and "a.py" in ctx_msg
    assert "任务清单" in ctx_msg and "收尾" in ctx_msg
    assert "执行过的关键命令" in ctx_msg and "pytest -q" in ctx_msg
    assert "SUMMARY" in ctx_msg
