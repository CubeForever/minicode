import json
from pathlib import Path

import pytest

from minicode.agent import Agent, MODES
from minicode.checkpoints import CheckpointManager
from minicode.cli import _expand_mentions, _resolve_command, _shell_passthrough
from minicode.config import Config, load_config
from minicode.fake import FakeProvider
from minicode.llm import _to_anthropic_messages, _to_openai_messages
from minicode.session import Session
from minicode.tools import build_registry
from minicode.tools.base import ToolContext
from minicode.tools.shell import ShellState
from minicode.ui import UI


def make_agent(tmp_path, provider, mode="yolo") -> Agent:
    cfg = Config(provider="fake", api_key="", model="fake", mode=mode)
    cfg.cwd = tmp_path
    session = Session()
    return Agent(provider, session, UI(), cfg,
                 build_registry(ShellState(tmp_path, "bash")),
                 checkpoints=CheckpointManager(tmp_path / "ck"))


# ---------- ! shell passthrough ----------

def test_shell_passthrough_runs_locally(tmp_path):
    agent = make_agent(tmp_path, FakeProvider([{"text": "should not run"}]))

    class CollectUI(UI):
        def __init__(self):
            super().__init__()
            self.lines = []

        def plain(self, msg=""):
            self.lines.append(msg)

    ui = CollectUI()
    _shell_passthrough(agent, ui, "echo passthrough-ok")
    joined = "\n".join(ui.lines)
    assert "passthrough-ok" in joined
    assert agent.session.messages == []  # no model call happened
    assert "should not run" not in joined


def test_shell_passthrough_empty_shows_usage(tmp_path):
    agent = make_agent(tmp_path, FakeProvider([]))

    class InfoUI(UI):
        def __init__(self):
            super().__init__()
            self.infos = []

        def info(self, m):
            self.infos.append(m)

    ui = InfoUI()
    _shell_passthrough(agent, ui, "")
    assert any("用法" in i for i in ui.infos)


# ---------- @文件 提及 ----------

def test_expand_mentions_attaches_text(tmp_path):
    agent = make_agent(tmp_path, FakeProvider([]))
    (tmp_path / "notes.txt").write_text(" secret value ", encoding="utf-8")
    out = _expand_mentions("看看 @notes.txt 说了什么", agent)
    assert "附文件 notes.txt" in out and "secret value" in out
    assert out.startswith("看看 @notes.txt")


def test_expand_mentions_image_blocks(tmp_path):
    agent = make_agent(tmp_path, FakeProvider([]))
    (tmp_path / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    out = _expand_mentions("@pic.png 这是什么", agent)
    assert isinstance(out, list)
    assert out[0]["type"] == "text" and "这是什么" in out[0]["text"]
    assert out[1]["type"] == "image" and out[1]["media_type"] == "image/png"
    assert out[-1]["type"] == "text" and "pic.png" in out[-1]["text"]


def test_expand_mentions_ignores_missing(tmp_path):
    agent = make_agent(tmp_path, FakeProvider([]))
    out = _expand_mentions("邮件发到 user@example.com 了吗", agent)
    assert out == "邮件发到 user@example.com 了吗"


def test_user_image_message_wire_conversion():
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": "看图"},
        {"type": "image", "media_type": "image/png", "data": "QUJD"}]}]
    oai = _to_openai_messages(msgs, None)
    assert oai[0]["content"][1]["type"] == "image_url"
    assert oai[0]["content"][1]["image_url"]["url"].startswith("data:image/png")
    anthropic = _to_anthropic_messages(msgs)
    assert anthropic[0]["content"][1]["type"] == "image"
    assert anthropic[0]["content"][1]["source"]["data"] == "QUJD"


# ---------- 命令前缀匹配与别名 ----------

def test_resolve_command_aliases():
    class NoUI(UI):
        def error(self, m):
            pass

    name, arg, ok = _resolve_command("/yolo", None, NoUI())
    assert (name, arg, ok) == ("/mode", "full-access", True)
    name, arg, ok = _resolve_command("/plan", None, NoUI())
    assert (name, arg, ok) == ("/mode", "plan", True)
    name, arg, ok = _resolve_command("/edits", None, NoUI())
    assert (name, arg, ok) == ("/mode", "accept-edits", True)


def test_resolve_command_prefix(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # no custom commands

    class NoUI(UI):
        def __init__(self):
            super().__init__()
            self.errors = []

        def error(self, m):
            self.errors.append(m)

    ui = NoUI()
    name, arg, ok = _resolve_command("/compact", agent=None, ui=ui)
    assert (name, ok) == ("/compact", True)
    name, arg, ok = _resolve_command("/statu", agent=None, ui=ui)
    assert (name, ok) == ("/status", True)
    name, arg, ok = _resolve_command("/stat", agent=None, ui=ui)
    assert ok is False and ui.errors  # /stats 与 /status 歧义
    name, arg, ok = _resolve_command("/m", agent=None, ui=ui)
    assert ok is False and ui.errors


# ---------- /model 序号 ----------

def test_model_switch_by_index(tmp_path):
    class FakeProv:
        model = "glm-5.2"

        def available_models(self, force=False):
            return ["a-model", "b-model", "c-model"]

    from minicode import cli

    class InfoUI(UI):
        def __init__(self):
            super().__init__()
            self.infos = []

        def info(self, m):
            self.infos.append(m)

    cfg = Config(provider="fake", api_key="", model="a-model")
    cfg.cwd = tmp_path
    agent = Agent(FakeProv(), Session(), InfoUI(), cfg,
                  build_registry(ShellState(tmp_path, "bash")))
    ui = agent.ui
    cli._command("/model 3", agent, ui, tmp_path)
    assert agent.provider.model == "c-model"
    assert any("c-model" in i for i in ui.infos)


# ---------- 完全访问模式 ----------

def test_full_access_auto_runs_everything(tmp_path):
    script = [
        {"tool_calls": [
            {"id": "t1", "name": "write_file",
             "args": json.dumps({"path": "x.txt", "content": "hi"})},
            {"id": "t2", "name": "bash",
             "args": json.dumps({"command": "echo done"})}]},
        {"text": "ok"},
    ]
    agent = make_agent(tmp_path, FakeProvider(script), mode="full-access")
    out = agent.run_turn("go")
    assert out == "ok"
    assert (tmp_path / "x.txt").exists()
    assert "done" in agent.session.messages[3]["content"]


def test_full_access_high_risk_requires_confirm_noninteractive(tmp_path):
    script = [
        {"tool_calls": [{"id": "t1", "name": "bash",
                         "args": json.dumps({"command": "rm -rf ./build"})}]},
        {"text": "ok"},
    ]
    (tmp_path / "build").mkdir()
    agent = make_agent(tmp_path, FakeProvider(script), mode="full-access")
    agent.run_turn("clean")
    tool_msg = agent.session.messages[2]
    assert "高危操作" in tool_msg["content"]
    assert "非交互模式自动拒绝" in tool_msg["content"]
    assert tool_msg["is_error"]


def test_full_access_sensitive_paths_still_guarded(tmp_path):
    script = [
        {"tool_calls": [{"id": "t1", "name": "write_file",
                         "args": json.dumps({"path": ".env", "content": "K=1"})}]},
        {"text": "ok"},
    ]
    agent = make_agent(tmp_path, FakeProvider(script), mode="full-access")
    agent.run_turn("write env")
    tool_msg = agent.session.messages[2]
    assert "敏感路径保护" in tool_msg["content"]


def test_mode_alias_yolo_still_works(tmp_path):
    agent = make_agent(tmp_path, FakeProvider([{"text": "ok"}]), mode="yolo")
    assert agent.mode == "yolo"  # legacy accepted
    # and behaves like full-access
    script = [{"tool_calls": [{"id": "t", "name": "bash",
                               "args": json.dumps({"command": "rm -rf ./b"})}]},
              {"text": "x"}]
    (tmp_path / "b").mkdir()
    agent2 = make_agent(tmp_path, FakeProvider(script), mode="yolo")
    agent2.run_turn("go")
    assert "高危操作" in agent2.session.messages[2]["content"]


def test_config_normalizes_yolo_to_full_access(tmp_path, monkeypatch):
    conf = tmp_path / "c.json"
    conf.write_text(json.dumps({"provider": "openai", "api_key": "k",
                                "mode": "yolo"}), encoding="utf-8")
    monkeypatch.setattr("minicode.config.USER_CONFIG", conf)
    monkeypatch.setattr("minicode.config.PROJECT_CONFIG", tmp_path / "none.json")
    from types import SimpleNamespace
    cfg = load_config(SimpleNamespace(profile=None, provider=None, model=None,
                                      yolo=False))
    assert cfg.mode == "full-access"
    assert "full-access" in MODES and "yolo" not in MODES


# ---------- 上下文余量条 ----------

def test_context_bar_renders_remaining():
    class BarUI(UI):
        def __init__(self):
            super().__init__()
            self.out = []

        def _write(self, s):
            self.out.append(s)

    ui = BarUI()
    ui.context_bar(32000, 128000, "glm-5.2")
    text = "".join(ui.out)
    assert "ctx" in text and "剩余 75%" in text
    assert "32k/128k" in text
    assert "glm-5.2" in text
    ui.context_bar(120000, 128000)
    assert "剩余 6%" in "".join(ui.out)


def test_context_bar_1m_format():
    class BarUI(UI):
        def __init__(self):
            super().__init__()
            self.out = []

        def _write(self, s):
            self.out.append(s)

    ui = BarUI()
    ui.context_bar(158000, 1_000_000, "glm-5.2")
    text = "".join(ui.out)
    assert "158k/1M" in text and "剩余 84%" in text


# ---------- /limit 自定义上下文长度 ----------

def test_parse_size():
    from minicode.cli import _parse_size
    assert _parse_size("1M") == 1_000_000
    assert _parse_size("1m") == 1_000_000
    assert _parse_size("500k") == 500_000
    assert _parse_size("200000") == 200_000
    assert _parse_size("1.5M") == 1_500_000
    assert _parse_size("abc") is None
    assert _parse_size("") is None


def test_limit_command_sets_context_limit(tmp_path):
    from minicode import cli

    class InfoUI(UI):
        def __init__(self):
            super().__init__()
            self.infos = []

        def info(self, m):
            self.infos.append(m)

    cfg = Config(provider="fake", api_key="", model="fake")
    cfg.cwd = tmp_path
    agent = Agent(FakeProvider([]), Session(), InfoUI(), cfg,
                  build_registry(ShellState(tmp_path, "bash")))
    ui = agent.ui
    cli._command("/limit 500k", agent, ui, tmp_path)
    assert agent.config.context_limit == 500_000
    assert any("500k" in i for i in ui.infos)
    cli._command("/limit", agent, ui, tmp_path)
    assert any("500,000" in i for i in ui.infos)
    cli._command("/limit bogus", agent, ui, tmp_path)
    assert agent.config.context_limit == 500_000  # unchanged


def test_default_context_limit_1m(tmp_path):
    cfg = Config(provider="fake", api_key="", model="fake")
    assert cfg.context_limit == 128_000  # dataclass default untouched
    from minicode.config import PROVIDER_DEFAULTS
    assert PROVIDER_DEFAULTS["openai"]["context_limit"] == 1_000_000
    assert PROVIDER_DEFAULTS["anthropic"]["context_limit"] == 200_000
