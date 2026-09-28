import json
import re
import time
from pathlib import Path

import pytest

from minicode.agent import Agent, rule_matches, thinking_budget
from minicode.checkpoints import CheckpointManager
from minicode.config import Config
from minicode.fake import FakeProvider
from minicode.llm import AnthropicProvider, OpenAIProvider
from minicode.session import Session
from minicode.tools import build_registry
from minicode.tools.base import ToolContext
from minicode.tools.shell import BashOutputTool, BashKillTool, BashTool, ShellState
from minicode.ui import StreamRenderer, UI
from minicode.tools.webfetch import html_to_text
from minicode.tools.ask_user import AskUserTool


def make_agent(tmp_path, provider, ui=None, mode="yolo", permissions=None) -> Agent:
    cfg = Config(provider="fake", api_key="", model="fake", mode=mode,
                 permissions=permissions or {})
    cfg.cwd = tmp_path
    ui = ui or UI()
    session = Session()
    agent = Agent(provider, session, ui, cfg,
                  build_registry(ShellState(tmp_path, "bash")),
                  checkpoints=CheckpointManager(tmp_path / "ckpts"))
    agent.system_prompt = "test"
    return agent


# ---------- plan mode ----------

def test_plan_mode_blocks_mutations(tmp_path):
    script = [
        {"tool_calls": [{"id": "t1", "name": "write_file",
                         "args": json.dumps({"path": "x.txt", "content": "hi"})}]},
        {"text": "PLAN: do X then Y"},
    ]
    agent = make_agent(tmp_path, FakeProvider(script), mode="plan")
    out = agent.run_turn("plan something")
    assert not (tmp_path / "x.txt").exists()
    tool_msg = agent.session.messages[2]
    assert "Plan mode" in tool_msg["content"]
    assert tool_msg["is_error"]
    assert out == "PLAN: do X then Y"


def test_plan_mode_allows_reads(tmp_path):
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    script = [
        {"tool_calls": [{"id": "t1", "name": "read_file",
                         "args": json.dumps({"path": "a.txt"})}]},
        {"text": "read ok"},
    ]
    agent = make_agent(tmp_path, FakeProvider(script), mode="plan")
    agent.run_turn("read")
    assert "hello" in agent.session.messages[2]["content"]


# ---------- permission rules ----------

def test_permission_allow_skips_confirm(tmp_path):
    confirmed = []

    class SpyUI(UI):
        def confirm(self, title, preview=None):
            confirmed.append(title)
            return "n"

    script = [
        {"tool_calls": [{"id": "t1", "name": "bash",
                         "args": json.dumps({"command": "echo allow-me"})}]},
        {"text": "done"},
    ]
    agent = make_agent(tmp_path, FakeProvider(script), ui=SpyUI(),
                       mode="default", permissions={"allow": ["bash(echo *)"]})
    agent.run_turn("go")
    assert confirmed == []  # never asked
    assert "allow-me" in agent.session.messages[2]["content"]


def test_permission_deny_blocks(tmp_path):
    script = [
        {"tool_calls": [{"id": "t1", "name": "write_file",
                         "args": json.dumps({"path": "x.txt", "content": "hi"})}]},
        {"text": "ok"},
    ]
    agent = make_agent(tmp_path, FakeProvider(script), mode="yolo",
                       permissions={"deny": ["write_file"]})
    agent.run_turn("go")
    assert not (tmp_path / "x.txt").exists()
    assert "denied by permission rule" in agent.session.messages[2]["content"]


def test_rule_matches():
    from minicode.tools.fs import WriteFileTool
    from minicode.tools.shell import BashTool
    w, b = WriteFileTool(), BashTool(ShellState(Path("."), "bash"))
    assert rule_matches("write_file", w, {})
    assert rule_matches("WriteFile", w, {})
    assert rule_matches("Bash(git *)", b, {"command": "git status"})
    assert not rule_matches("Bash(git *)", b, {"command": "rm -rf /"})
    assert rule_matches("WebFetch", type("T", (), {"name": "web_fetch"})(), {})


# ---------- checkpoints ----------

def test_checkpoint_undo_modify(tmp_path):
    from minicode.tools.fs import WriteFileTool
    ctx = ToolContext(cwd=tmp_path, config=Config(), session=Session(), ui=UI())
    p = tmp_path / "f.txt"
    p.write_text("v1", encoding="utf-8")
    mgr = CheckpointManager(tmp_path / "ck")
    mgr.snapshot(p, "write_file")
    from minicode.tools.fs import ReadFileTool
    ReadFileTool().run({"path": "f.txt"}, ctx)   # read-before-edit
    WriteFileTool().run({"path": "f.txt", "content": "v2"}, ctx)
    assert p.read_text() == "v2"
    e = mgr.undo()
    assert e["path"] == str(p)
    assert p.read_text() == "v1"
    assert mgr.undo() is None


def test_checkpoint_undo_created_file(tmp_path):
    p = tmp_path / "new.txt"
    mgr = CheckpointManager(tmp_path / "ck")
    mgr.snapshot(p, "write_file")   # file does not exist yet
    p.write_text("created", encoding="utf-8")
    mgr.undo()
    assert not p.exists()


def test_agent_checkpoints_writes(tmp_path):
    script = [
        {"tool_calls": [{"id": "t1", "name": "write_file",
                         "args": json.dumps({"path": "c.txt", "content": "one"})}]},
        {"tool_calls": [{"id": "t2", "name": "write_file",
                         "args": json.dumps({"path": "c.txt", "content": "two"})}]},
        {"text": "done"},
    ]
    agent = make_agent(tmp_path, FakeProvider(script))
    agent.run_turn("go")
    mgr = agent.checkpoints
    assert len(mgr.entries) == 2
    mgr.undo()
    assert (tmp_path / "c.txt").read_text() == "one"


def test_rewind_to(tmp_path):
    mgr = CheckpointManager(tmp_path / "ck")
    a = tmp_path / "a.txt"
    a.write_text("1", encoding="utf-8")
    mgr.snapshot(a, "edit_file")
    a.write_text("2", encoding="utf-8")
    mgr.snapshot(a, "edit_file")
    a.write_text("3", encoding="utf-8")
    shown = mgr.list(10)
    mgr.rewind_to(0, shown)  # back to state after first edit
    assert a.read_text() == "1"


# ---------- extended thinking ----------

def test_thinking_budget_keywords():
    assert thinking_budget("ultrathink this problem") == 31999
    assert thinking_budget("think hard about it") == 10000
    assert thinking_budget("megathink") == 10000
    assert thinking_budget("let me think") == 4000
    assert thinking_budget("hello world") == 0


def test_anthropic_thinking_body():
    p = AnthropicProvider("m", "k", max_tokens=8192)
    body = p._build_body([{"role": "user", "content": "hi"}], None, "sys",
                         thinking=10000)
    assert body["thinking"] == {"type": "enabled", "budget_tokens": 10000}
    assert body["max_tokens"] == 14096
    plain = p._build_body([{"role": "user", "content": "hi"}], None, "sys")
    assert "thinking" not in plain


def test_anthropic_thinking_roundtrip():
    msgs = [{"role": "assistant", "content": None,
             "tool_calls": [{"id": "t", "name": "bash", "args": "{}"}],
             "thinking": [{"thinking": "hmm", "signature": "SIG"}]}]
    from minicode.llm import _to_anthropic_messages
    wire = _to_anthropic_messages(msgs)
    blocks = wire[0]["content"]
    assert blocks[0]["type"] == "thinking"
    assert blocks[0]["signature"] == "SIG"
    assert blocks[1]["type"] == "tool_use"


def test_extra_body_merge():
    p = OpenAIProvider("m", "k", extra_body={"temperature": 0.2, "thinking": {"type": "enabled"}})
    body = p._build_body([{"role": "user", "content": "hi"}], None, None)
    assert body["temperature"] == 0.2
    assert body["thinking"] == {"type": "enabled"}


# ---------- background shells ----------

@pytest.fixture
def bash_ctx(tmp_path):
    cfg = Config(provider="fake", api_key="", model="fake")
    cfg.cwd = tmp_path
    return ToolContext(cwd=tmp_path, config=cfg, session=Session(), ui=UI())


def test_background_bash_lifecycle(tmp_path, bash_ctx):
    from minicode.tools.shell import detect_shell
    if detect_shell(None) != "bash":
        pytest.skip("bash-specific test")
    st = ShellState(tmp_path, "bash")
    tool = BashTool(st)
    r = tool.run({"command": "echo bg-hello", "run_in_background": True}, bash_ctx)
    m = re.search(r"bash_(\d+)", r)
    assert m, r
    bid = m.group(1)
    out_tool = BashOutputTool(st)
    out = ""
    acc = ""
    deadline = time.time() + 10
    while time.time() < deadline:
        out = out_tool.run({"id": bid}, bash_ctx)
        acc += out
        if "exited" in out:
            break
        time.sleep(0.1)
    assert "bg-hello" in acc
    assert "exited with code 0" in acc

    # kill a long-running one
    r2 = tool.run({"command": "sleep 30", "run_in_background": True}, bash_ctx)
    bid2 = re.search(r"bash_(\d+)", r2).group(1)
    kr = BashKillTool(st).run({"id": bid2}, bash_ctx)
    assert "killed" in kr
    out2 = BashOutputTool(st).run({"id": bid2}, bash_ctx)
    assert "exited" in out2 or "killed" in out2


# ---------- custom slash commands ----------

def test_custom_commands(tmp_path, monkeypatch):
    d = tmp_path / ".minicode" / "commands"
    d.mkdir(parents=True)
    (d / "deploy.md").write_text(
        "---\ndescription: 部署到指定环境\n---\n把服务部署到 $ARGUMENTS 环境", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    from minicode.cli import _custom_commands
    cmds = _custom_commands()
    assert "deploy" in cmds
    desc, body = cmds["deploy"]
    assert desc == "部署到指定环境"
    assert body.replace("$ARGUMENTS", "prod") == "把服务部署到 prod 环境"


# ---------- streaming renderer ----------

def test_stream_renderer_preserves_text():
    chunks_out = []
    r = StreamRenderer(chunks_out.append)
    r.feed("```py\nx = 1\n")
    r.feed("y = 2\n```\n")
    r.feed("done")
    r.flush()
    assert "".join(chunks_out) == "```py\nx = 1\ny = 2\n```\ndone"


# ---------- ask_user ----------

class ChooseUI(UI):
    def __init__(self, answer):
        super().__init__()
        self.answer = answer
        self.asked = None

    def choose(self, question, options, multi=False, allow_other=True):
        self.asked = (question, options, multi)
        return self.answer


def test_ask_user_selected(tmp_path):
    ui = ChooseUI(["方案 A"])
    ctx = ToolContext(cwd=tmp_path, config=Config(), session=Session(), ui=ui)
    r = AskUserTool().run({"question": "选哪个？",
                           "options": [{"label": "方案 A"}, {"label": "方案 B"}]}, ctx)
    assert "方案 A" in r
    assert ui.asked[0] == "选哪个？"


def test_ask_user_skipped(tmp_path):
    ui = ChooseUI([])
    ctx = ToolContext(cwd=tmp_path, config=Config(), session=Session(), ui=ui)
    r = AskUserTool().run({"question": "q", "options": [{"label": "a"}, {"label": "b"}]}, ctx)
    assert "skipped" in r


# ---------- webfetch html extraction ----------

def test_html_to_text():
    html = ("<html><head><title>My Page</title></head><body>"
            "<script>var x = 'hidden';</script><style>.a{}</style>"
            "<h1>Title</h1><p>hello</p><p>world</p></body></html>")
    text = html_to_text(html)
    assert "hello" in text and "world" in text
    assert "hidden" not in text
