import json

from minicode.agent import Agent
from minicode.config import Config
from minicode.fake import FakeProvider
from minicode.session import Session
from minicode.tools import build_registry
from minicode.tools.shell import ShellState
from minicode.ui import UI


def make_agent(tmp_path, provider, ui=None, mode="yolo") -> Agent:
    cfg = Config(provider="fake", api_key="", model="fake", mode=mode)
    cfg.cwd = tmp_path
    ui = ui or UI()
    session = Session()
    agent = Agent(provider, session, ui, cfg,
                  build_registry(ShellState(tmp_path, "bash")))
    agent.system_prompt = "test"
    return agent


def test_agent_runs_tools_and_finishes(tmp_path):
    script = [
        {"tool_calls": [{"id": "t1", "name": "write_file",
                         "args": json.dumps({"path": "x.txt", "content": "hi"})}]},
        {"text": "done"},
    ]
    agent = make_agent(tmp_path, FakeProvider(script))
    out = agent.run_turn("create x.txt")
    assert out == "done"
    assert (tmp_path / "x.txt").read_text() == "hi"
    roles = [m["role"] for m in agent.session.messages]
    assert roles == ["user", "assistant", "tool", "assistant"]


def test_agent_decline(tmp_path):
    class DenyUI(UI):
        def confirm(self, title, preview=None):
            return "n"

    script = [
        {"tool_calls": [{"id": "t1", "name": "write_file",
                         "args": json.dumps({"path": "x.txt", "content": "hi"})}]},
        {"text": "ok, skipped"},
    ]
    agent = make_agent(tmp_path, FakeProvider(script), ui=DenyUI(), mode="default")
    agent.run_turn("write")
    assert not (tmp_path / "x.txt").exists()
    tool_msg = agent.session.messages[2]
    assert "declined" in tool_msg["content"].lower()
    assert not tool_msg["is_error"]


def test_agent_accept_always(tmp_path):
    class AllowUI(UI):
        def confirm(self, title, preview=None):
            return "a"

    script = [
        {"tool_calls": [{"id": "t1", "name": "write_file",
                         "args": json.dumps({"path": "a.txt", "content": "1"})},
                        {"id": "t2", "name": "write_file",
                         "args": json.dumps({"path": "b.txt", "content": "2"})}]},
        {"text": "ok"},
    ]
    agent = make_agent(tmp_path, FakeProvider(script), ui=AllowUI(), mode="default")
    agent.run_turn("write two files")
    assert (tmp_path / "a.txt").exists() and (tmp_path / "b.txt").exists()
    assert "write" in agent.session.auto_approved  # 'a' approved the whole kind


def test_unknown_tool_is_reported_not_fatal(tmp_path):
    script = [
        {"tool_calls": [{"id": "t1", "name": "nope", "args": "{}"}]},
        {"text": "handled"},
    ]
    agent = make_agent(tmp_path, FakeProvider(script))
    agent.run_turn("go")
    assert "unknown tool" in agent.session.messages[2]["content"]
    assert agent.session.messages[2]["is_error"]


def test_invalid_json_args(tmp_path):
    script = [
        {"tool_calls": [{"id": "t1", "name": "write_file", "args": "{bad json"}]},
        {"text": "handled"},
    ]
    agent = make_agent(tmp_path, FakeProvider(script))
    agent.run_turn("go")
    assert "invalid tool arguments" in agent.session.messages[2]["content"]


def test_compact(tmp_path):
    agent = make_agent(tmp_path, FakeProvider([{"text": "SUMMARY"}]))
    agent.session.messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]
    stats = agent.compact()
    assert stats["before"] == 2
    assert len(agent.session.messages) == 1
    assert "SUMMARY" in agent.session.messages[0]["content"]


def test_session_roundtrip(tmp_path):
    s = Session()
    s.add({"role": "user", "content": "你好"})
    s.add({"role": "assistant", "content": None,
           "tool_calls": [{"id": "t", "name": "bash", "args": "{}"}]})
    p = tmp_path / "s.json"
    s.save(p)
    s2 = Session.load(p)
    assert s2.messages == s.messages


def test_session_usage_tracking(tmp_path):
    s = Session()
    s.note_usage({"input": 100, "output": 10})
    s.note_usage({"input": 50, "output": 5})
    assert s.total_usage == {"input": 150, "output": 15}
    assert s.context_tokens() == 50  # last input reflects full prompt
