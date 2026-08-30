import json
from pathlib import Path

import pytest

from minicode.agent import Agent
from minicode.checkpoints import CheckpointManager
from minicode.config import Config
from minicode.fake import FakeProvider
from minicode.session import Session
from minicode.tools import build_registry
from minicode.tools.base import ToolContext, ToolError
from minicode.tools.plan import ExitPlanTool
from minicode.tools.shell import ShellState
from minicode.tools.skills import (SkillTool, load_skill, skills_catalog,
                                   skills_section_text)
from minicode.ui import UI


def make_ctx(tmp_path):
    cfg = Config(provider="fake", api_key="", model="fake")
    cfg.cwd = tmp_path
    return ToolContext(cwd=tmp_path, config=cfg, session=Session(), ui=UI(),
                       agent_factory=None)


def make_agent(tmp_path, provider, mode="yolo", hooks=None) -> Agent:
    cfg = Config(provider="fake", api_key="", model="fake", mode=mode,
                 hooks=hooks or {})
    cfg.cwd = tmp_path
    session = Session()
    return Agent(provider, session, UI(), cfg,
                 build_registry(ShellState(tmp_path, "bash")),
                 checkpoints=CheckpointManager(tmp_path / "ck"))


# ---------- 内置技能（ZCode 式工作流） ----------

def test_builtin_skills_available():
    cat = skills_catalog(Path(".").parent)  # any cwd; builtin is package-level
    for expected in ("systematic-debugging", "test-driven-development",
                     "verification-before-completion", "writing-plans"):
        assert expected in cat
        assert cat[expected][1] == "内置"


def test_load_builtin_skill_content(tmp_path):
    body = load_skill(tmp_path, "systematic-debugging")
    assert body and "复现" in body and "根因" in body
    assert load_skill(tmp_path, "nope") is None


def test_skill_tool_runs(tmp_path):
    ctx = make_ctx(tmp_path)
    out = SkillTool().run({"name": "test-driven-development"}, ctx)
    assert out.startswith("[skill: test-driven-development]")
    assert "红" in out  # TDD red phase
    with pytest.raises(ToolError, match="unknown skill"):
        SkillTool().run({"name": "nope"}, ctx)


def test_project_skill_overrides_builtin(tmp_path):
    d = tmp_path / ".minicode" / "skills" / "systematic-debugging"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: systematic-debugging\ndescription: 项目定制版\n---\n项目专属调试流程",
        encoding="utf-8")
    cat = skills_catalog(tmp_path)
    assert cat["systematic-debugging"][1] == "项目"
    body = load_skill(tmp_path, "systematic-debugging")
    assert "项目专属调试流程" in body


def test_skills_section_in_system_prompt(tmp_path):
    from minicode.prompts import build_system_prompt
    cfg = Config(provider="fake", api_key="", model="fake")
    cfg.cwd = tmp_path
    prompt = build_system_prompt(cfg, tmp_path,
                                 skills_block=skills_section_text(tmp_path))
    assert "# Skills" in prompt
    assert "systematic-debugging" in prompt
    assert "skill tool" in prompt


def test_skills_registry_includes_skill_tool(tmp_path):
    reg = build_registry(ShellState(tmp_path, "bash"))
    assert reg.get("skill") is not None


def test_exit_plan_allowed_prompts(tmp_path):
    ctx = make_ctx(tmp_path)
    ExitPlanTool().run({"plan": "1. run tests",
                        "allowed_prompts": ["Bash(pytest *)"]}, ctx)
    assert ctx.session.plan_allowed_rules == ["Bash(pytest *)"]


def test_plan_allowed_prompts_authorize_in_accept_edits(tmp_path):
    # simulate: plan approved -> rules active; accept-edits auto-approves edits,
    # but bash normally needs confirm. The plan rule must auto-approve pytest.
    script = [
        {"tool_calls": [{"id": "t", "name": "bash",
                         "args": json.dumps({"command": "pytest -q"})}]},
        {"text": "verified"},
    ]
    agent = make_agent(tmp_path, FakeProvider(script), mode="accept-edits")
    agent.session.plan_allowed_rules.append("Bash(pytest *)")
    agent.run_turn("run tests")
    tool_msg = agent.session.messages[2]
    assert "pytest" in tool_msg["content"]
    assert not tool_msg["is_error"]


def test_rule_matches_pytest_rule():
    from minicode.agent import rule_matches
    from minicode.tools.shell import BashTool
    b = BashTool(ShellState(Path("."), "bash"))
    assert rule_matches("Bash(pytest *)", b, {"command": "pytest -q tests/"})
    assert not rule_matches("Bash(pytest *)", b, {"command": "rm -rf /"})


# ---------- ask_user 自由输入 ----------

class OtherUI(UI):
    def __init__(self, raw):
        super().__init__()
        self.raw = raw

    def choose(self, question, options, multi=False, allow_other=True):
        if not allow_other:
            return []
        return [self.raw] if self.raw else []


def test_ask_user_other_free_text(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.ui = OtherUI("先修构建再谈")
    r = __import__("minicode.tools.ask_user",
                   fromlist=["AskUserTool"]).AskUserTool().run(
        {"question": "先做什么？",
         "options": [{"label": "修构建"}, {"label": "修测试"}]}, ctx)
    assert "先修构建再谈" in r and "custom" in r.lower()


def test_ask_user_other_disabled(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.ui = OtherUI("")
    r = __import__("minicode.tools.ask_user",
                   fromlist=["AskUserTool"]).AskUserTool().run(
        {"question": "q", "options": [{"label": "a"}, {"label": "b"}],
         "allow_other": False}, ctx)
    assert "skipped" in r


# ---------- post-hook 反馈回喂 ----------

def test_post_hook_feedback_appended(tmp_path):
    script = [
        {"tool_calls": [{"id": "t", "name": "bash",
                         "args": json.dumps({"command": "echo work"})}]},
        {"text": "done"},
    ]
    agent = make_agent(tmp_path, FakeProvider(script),
                       hooks={"post_tool_use": "echo 记得跑测试"})
    agent.run_turn("go")
    tool_msg = agent.session.messages[2]["content"]
    assert "[hook feedback]" in tool_msg and "跑测试" in tool_msg


# ---------- todo 优先级 ----------

def test_todo_priority_render_and_validation(tmp_path):
    from minicode.tools.todo import TodoWriteTool
    class TodoUI(UI):
        def __init__(self):
            super().__init__()
            self.rendered = None
        def todo_render(self, todos):
            self.rendered = todos
    ctx = make_ctx(tmp_path)
    ctx.ui = TodoUI()
    TodoWriteTool().run({"todos": [
        {"content": "先修构建", "status": "in_progress", "priority": "high"},
        {"content": "加文档", "status": "pending", "priority": "bogus"},
    ]}, ctx)
    todos = ctx.session.todos
    assert todos[0]["priority"] == "high"
    assert todos[1]["priority"] == "medium"  # invalid normalized
