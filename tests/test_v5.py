import json
import sys
from pathlib import Path

import pytest

from minicode.agent import Agent
from minicode.checkpoints import CheckpointManager
from minicode.config import Config
from minicode.fake import FakeProvider
from minicode.session import Session
from minicode.tools import build_registry
from minicode.tools.base import ToolContext, ToolError
from minicode.tools.memory import (BrainWriteTool, append_brain,
                                   brain_path, load_brain_text, render_brain)
from minicode.tools.panel import ConsultPanelTool
from minicode.tools.shell import ShellState
from minicode.ui import UI


def make_agent(tmp_path, provider, mode="yolo", ui=None, **cfg_kwargs) -> Agent:
    cfg = Config(provider="fake", api_key="", model="fake", mode=mode, **cfg_kwargs)
    cfg.cwd = tmp_path
    session = Session()
    agent = Agent(provider, session, ui or UI(), cfg,
                  build_registry(ShellState(tmp_path, "bash")),
                  checkpoints=CheckpointManager(tmp_path / "ck"))
    agent.system_prompt = "test"
    return agent


def make_ctx(tmp_path):
    cfg = Config(provider="fake", api_key="", model="fake")
    cfg.cwd = tmp_path
    return ToolContext(cwd=tmp_path, config=cfg, session=Session(),
                       ui=UI(), agent_factory=None)


# ---------- 项目大脑 ----------

def test_brain_append_dedup_and_render(tmp_path):
    assert render_brain(tmp_path) == ""
    added, msg = append_brain(tmp_path, "fact", "测试命令是 pytest -q")
    assert added
    added2, msg2 = append_brain(tmp_path, "fact", "测试命令是 pytest -q")
    assert not added2 and "already" in msg2
    append_brain(tmp_path, "gotcha", "直接跑 main.py 会覆盖数据")
    append_brain(tmp_path, "failed", "用正则解析 HTML — 嵌套标签会漏")
    text = load_brain_text(tmp_path)
    assert "## Facts" in text and "## Gotchas" in text and "## Failed approaches" in text
    assert "pytest -q" in text
    assert render_brain(tmp_path).count("## ") == 3
    # section rotation keeps it bounded
    for i in range(55):
        append_brain(tmp_path, "decision", f"decision-{i}")
    assert len(load_brain_text(tmp_path).splitlines()) < 120


def test_brain_write_tool(tmp_path):
    ctx = make_ctx(tmp_path)
    r = BrainWriteTool().run({"kind": "decision", "content": "用 sqlite 而不是 json 存状态"},
                             ctx)
    assert "Decisions" in load_brain_text(tmp_path) or "recorded" in r
    assert "future sessions" in r
    with pytest.raises(ToolError):
        BrainWriteTool().run({"kind": "bad", "content": "x"}, ctx)
    with pytest.raises(ToolError):
        BrainWriteTool().run({"kind": "fact", "content": ""}, ctx)


def test_brain_in_system_prompt(tmp_path):
    append_brain(tmp_path, "gotcha", "never touch data/ by hand")
    from minicode.prompts import build_system_prompt
    cfg = Config(provider="fake", api_key="", model="fake")
    cfg.cwd = tmp_path
    prompt = build_system_prompt(cfg, tmp_path)
    assert "never touch data/ by hand" in prompt
    assert "brain" in prompt.lower()


def test_brain_survives_custom_sections(tmp_path):
    append_brain(tmp_path, "fact", "a fact")
    p = brain_path(tmp_path)
    p.write_text(p.read_text(encoding="utf-8")
                 + "\n## Team notes\n- 用户偏好中文注释\n", encoding="utf-8")
    append_brain(tmp_path, "gotcha", "a gotcha")
    text = load_brain_text(tmp_path)
    assert "## Team notes" in text and "用户偏好中文注释" in text
    assert "a gotcha" in text and "a fact" in text


def test_agent_writes_brain_via_tool(tmp_path):
    script = [
        {"tool_calls": [{"id": "t", "name": "brain_write",
                         "args": json.dumps({"kind": "fact",
                                             "content": "构建命令: make all"})}]},
        {"text": "记住了"},
    ]
    agent = make_agent(tmp_path, FakeProvider(script))
    agent.run_turn("learn")
    assert "make all" in load_brain_text(tmp_path)


# ---------- 圆桌模式 ----------

def test_consult_panel_roundtrip(tmp_path):
    calls = []

    def factory(prompt, subagent_type=None):
        calls.append(prompt)
        return f"report-{len(calls)}"

    ctx = make_ctx(tmp_path)
    ctx.agent_factory = factory
    r = ConsultPanelTool().run({"question": "should we migrate to async?"}, ctx)
    assert len(calls) == 3
    assert "务实派" in calls[0] and "架构派" in calls[1] and "风险猎手" in calls[2]
    assert "report-1" in r and "report-3" in r
    assert "综合" in r


def test_consult_panel_custom_perspectives(tmp_path):
    seen = []

    def factory(prompt, subagent_type=None):
        seen.append(prompt)
        return "ok"

    ctx = make_ctx(tmp_path)
    ctx.agent_factory = factory
    ConsultPanelTool().run({"question": "q", "perspectives": ["性能", "安全"]}, ctx)
    assert len(seen) == 2
    assert "性能" in seen[0] and "安全" in seen[1]


def test_consult_panel_requires_factory(tmp_path):
    ctx = make_ctx(tmp_path)  # agent_factory=None
    with pytest.raises(ToolError, match="not available"):
        ConsultPanelTool().run({"question": "q"}, ctx)
    with pytest.raises(ToolError, match="2-4"):
        ConsultPanelTool().run({"question": "q",
                                "perspectives": ["only-one"]},
                               ToolContext(cwd=tmp_path, config=Config(),
                                           session=Session(), ui=UI(),
                                           agent_factory=lambda p, t=None: "x"))


def test_consult_panel_inside_agent_turn(tmp_path):
    script = [
        {"tool_calls": [{"id": "t", "name": "consult_panel",
                         "args": json.dumps({"question": "哪种缓存策略更合适？"})}]},
        {"text": "结论：选 B 方案"},
    ]
    agent = make_agent(tmp_path, FakeProvider(script))
    agent.ctx.agent_factory = lambda prompt, subagent_type=None: \
        f"独立分析：{prompt[:20]}……"
    out = agent.run_turn("问一下圆桌")
    assert out == "结论：选 B 方案"
    tool_msg = agent.session.messages[2]["content"]
    assert "视角 1" in tool_msg and "视角 3" in tool_msg


# ---------- 自检回路 ----------

def _make_verify_script(tmp_path) -> str:
    checker = tmp_path / "verify_check.py"
    checker.write_text('import sys, os\n'
                       'sys.exit(0 if os.path.exists("FIXED") else 1)\n',
                       encoding="utf-8")
    return f'"{sys.executable}" "{checker}"'


def test_self_verify_gate_auto_fixes(tmp_path):
    from minicode import cli
    verify_cmd = _make_verify_script(tmp_path)
    script = [
        {"tool_calls": [{"id": "t1", "name": "write_file",
                         "args": json.dumps({"path": "FIXED",
                                             "content": "fixed"})}]},
        {"text": "修复完成"},
    ]
    agent = make_agent(tmp_path, FakeProvider(script),
                       verify_command=verify_cmd)
    class SpyUI(UI):
        def __init__(self):
            super().__init__()
            self.infos = []
            self.warnings = []
        def info(self, msg):
            self.infos.append(msg)
        def warn(self, msg):
            self.warnings.append(msg)
    agent.ui = SpyUI()
    agent.checkpoints.snapshot(tmp_path / "whatever", "write_file")  # simulate mutation
    cli._self_verify(agent, mutated=True)
    assert (tmp_path / "FIXED").exists()
    assert any("自检通过" in i for i in agent.ui.infos)


def test_self_verify_skipped_without_mutation(tmp_path):
    from minicode import cli
    agent = make_agent(tmp_path, FakeProvider([{"text": "hi"}]),
                       verify_command="exit 1")
    agent.checkpoints.snapshot(tmp_path / "f.txt", "write_file")
    cli._self_verify(agent, mutated=False)  # no mutation → gate skipped entirely
    assert not [m for m in agent.session.messages if m["role"] == "tool"]


def test_self_verify_gives_up_after_two_rounds(tmp_path):
    from minicode import cli
    cmd = f'"{sys.executable}" -c "import sys; sys.exit(1)"'
    # the "fix" turns never satisfy the always-failing checker
    agent = make_agent(tmp_path, FakeProvider([
        {"text": "try 1"}, {"text": "try 2"}, {"text": "try 3"},
    ]), verify_command=cmd)
    agent.checkpoints.snapshot(tmp_path / "f.txt", "write_file")
    cli._self_verify(agent, mutated=True)
    # exactly 2 auto-fix rounds were attempted
    assert len([m for m in agent.session.messages if m["role"] == "user"]) == 2


# ---------- 预算护栏 ----------

def test_turn_budget_stops_execution(tmp_path):
    # FakeProvider usage: input 150, output 40 on the first call → 190 tokens
    script = [
        {"tool_calls": [{"id": "t", "name": "bash",
                         "args": json.dumps({"command": "echo hi"})}]},
        {"text": "never reached"},
    ]
    agent = make_agent(tmp_path, FakeProvider(script), turn_budget=150)
    out = agent.run_turn("go")
    assert out == ""
    roles = [m["role"] for m in agent.session.messages]
    assert roles == ["user", "assistant"]  # tool never executed


def test_no_budget_runs_to_completion(tmp_path):
    script = [
        {"tool_calls": [{"id": "t", "name": "bash",
                         "args": json.dumps({"command": "echo hi"})}]},
        {"text": "done"},
    ]
    agent = make_agent(tmp_path, FakeProvider(script), turn_budget=0)
    assert agent.run_turn("go") == "done"


# ---------- 断点续跑 ----------

def test_pending_todo_detection():
    s = Session(todos=[{"content": "a", "status": "completed"},
                       {"content": "b", "status": "pending"},
                       {"content": "c", "status": "in_progress"}])
    pending = [t for t in s.todos if t.get("status") != "completed"]
    assert [t["content"] for t in pending] == ["b", "c"]
