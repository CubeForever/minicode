"""Tests for agent smart-behavior upgrades: tool-aware result summarization,
failure attribution with repeat detection, and semantic microcompaction."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from minicode.agent import Agent
from minicode.checkpoints import CheckpointManager
from minicode.config import Config
from minicode.fake import FakeProvider
from minicode.session import Session
from minicode.tools import build_registry
from minicode.tools.base import (summarize_result, truncate_middle,
                                 _summarize_read_file, _summarize_grep,
                                 _summarize_bash, _summarize_listing)
from minicode.tools.shell import ShellState
from minicode.ui import UI


def make_agent(tmp_path, provider, mode="yolo", **cfg_kwargs) -> Agent:
    cfg = Config(provider="fake", api_key="", model="fake", mode=mode, **cfg_kwargs)
    cfg.cwd = Path(tmp_path)
    session = Session()
    agent = Agent(provider, session, UI(), cfg,
                  build_registry(ShellState(Path(tmp_path), "bash")),
                  checkpoints=CheckpointManager(Path(tmp_path) / "ck"))
    agent.system_prompt = "test"
    return agent


# ---------- 1. tool-aware result summarization ----------

def test_summarize_short_result_unchanged():
    short = "hello world"
    assert summarize_result("read_file", short, 30000) == short


def test_summarize_unknown_tool_falls_back_to_truncate():
    big = "x" * 40000
    out = summarize_result("unknown_tool", big, 30000)
    assert "truncated" in out
    assert len(out) < len(big)


def test_summarize_read_file_folds_by_line():
    lines = [f"{i:>6}| line content {i}" for i in range(1, 200)]
    text = "\n".join(lines)
    out = _summarize_read_file(text, 30000)
    assert "lines folded" in out
    assert "line content 1" in out
    assert "line content 199" in out
    assert "line content 100" not in out


def test_summarize_grep_groups_by_file():
    lines = []
    for f in ("a.py", "b.py", "c.py"):
        for i in range(50):  # 150 total lines > 100 threshold
            lines.append(f"{f}:{i}: match_{i}")
    text = "\n".join(lines)
    out = _summarize_grep(text, 30000)
    assert "a.py" in out and "b.py" in out and "c.py" in out
    assert "matches" in out
    assert "more in this file" in out


def test_summarize_bash_keeps_error_lines():
    lines = [f"normal line {i}" for i in range(150)]
    lines.insert(80, "ERROR: something failed badly")
    lines.insert(81, "Traceback (most recent call last):")
    text = "\n".join(lines)
    out = _summarize_bash(text, 30000)
    assert "ERROR" in out
    assert "Traceback" in out
    assert "error-like lines kept" in out


def test_summarize_listing_counts_entries():
    lines = [f"file_{i}.py" for i in range(100)]
    text = "\n".join(lines)
    out = _summarize_listing(text, 30000)
    assert "100 entries total" in out
    assert "more entries" in out


def test_maybe_summarize_large_read_file(tmp_path):
    """_maybe_summarize should fold a >30k char read_file result."""
    agent = make_agent(tmp_path, FakeProvider([{"text": "x"}]))
    big_text = "\n".join(f"{i:>6}| payload line {i} with extra padding"
                         for i in range(1, 3000))  # ~90k chars
    out = agent._maybe_summarize("read_file", big_text)
    assert "lines folded" in out
    assert len(out) < len(big_text)


def test_maybe_summarize_passthrough_non_string(tmp_path):
    agent = make_agent(tmp_path, FakeProvider([{"text": "x"}]))
    blocks = {"blocks": [{"type": "text", "text": "hi"}]}
    assert agent._maybe_summarize("read_file", blocks) is blocks


# ---------- 2. failure attribution + repeat detection ----------

def test_attribute_failure_file_not_found_suggests_glob(tmp_path):
    agent = make_agent(tmp_path, FakeProvider([{"text": "x"}]))
    out = agent._attribute_failure("read_file", "Error: no such file or directory: foo.py")
    assert "glob" in out or "list_dir" in out
    assert "归因建议" in out


def test_attribute_failure_permission_suggests_add_dir(tmp_path):
    agent = make_agent(tmp_path, FakeProvider([{"text": "x"}]))
    out = agent._attribute_failure("bash", "Error: Permission denied: /etc/shadow")
    assert "add-dir" in out or "权限" in out


def test_attribute_failure_timeout_suggests_background(tmp_path):
    agent = make_agent(tmp_path, FakeProvider([{"text": "x"}]))
    out = agent._attribute_failure("bash", "Error: command timed out after 30s")
    assert "background" in out or "后台" in out


def test_attribute_failure_stale_suggests_reread(tmp_path):
    agent = make_agent(tmp_path, FakeProvider([{"text": "x"}]))
    out = agent._attribute_failure("edit_file", "Error: file modified since last read")
    assert "read_file" in out or "重新" in out


def test_attribute_failure_repeat_three_times_nudges_strategy_change(tmp_path):
    agent = make_agent(tmp_path, FakeProvider([{"text": "x"}]))
    err = "Error: no such file: missing.py"
    agent._attribute_failure("read_file", err)
    agent._attribute_failure("read_file", err)
    out3 = agent._attribute_failure("read_file", err)
    assert "换策略" in out3 or "停止重试" in out3 or "ask_user" in out3


def test_attribute_failure_no_suggestion_for_unknown_error(tmp_path):
    agent = make_agent(tmp_path, FakeProvider([{"text": "x"}]))
    out = agent._attribute_failure("bash", "Error: something weird happened")
    assert "归因建议" not in out


def test_attribute_failure_appended_in_agent_turn(tmp_path):
    """ToolError from a tool should carry attribution suggestions in the session."""
    script = [
        {"tool_calls": [{"id": "t", "name": "read_file",
                         "args": json.dumps({"path": "does_not_exist.txt"})}]},
        {"text": "ok"},
    ]
    agent = make_agent(tmp_path, FakeProvider(script))
    agent.run_turn("read missing")
    tool_msg = agent.session.messages[2]
    assert "归因建议" in tool_msg["content"]


# ---------- 3. semantic microcompaction ----------

def test_elide_high_value_tool_keeps_more():
    s = Session()
    long_write = "w" * 2000
    long_read = "r" * 2000
    s.messages = [
        {"role": "tool", "name": "write_file", "content": long_write},
        {"role": "tool", "name": "list_dir", "content": long_read},
        {"role": "assistant", "content": "recent"},
    ]
    for _ in range(10):
        s.messages.append({"role": "user", "content": "x"})
    changed = s.elide_old_tool_results(keep_recent=8)
    assert changed == 2
    write_elided = s.messages[0]["content"]
    list_elided = s.messages[1]["content"]
    assert write_elided.count("w") > list_elided.count("r")


def test_elide_error_result_keeps_more():
    s = Session()
    ok_result = "o" * 2000
    err_result = "e" * 2000
    s.messages = [
        {"role": "tool", "name": "bash", "content": ok_result, "is_error": False},
        {"role": "tool", "name": "bash", "content": err_result, "is_error": True},
    ]
    for _ in range(10):
        s.messages.append({"role": "user", "content": "x"})
    s.elide_old_tool_results(keep_recent=8)
    assert s.messages[1]["content"].count("e") > s.messages[0]["content"].count("o")


def test_elide_skips_recent_messages():
    s = Session()
    s.messages = [{"role": "tool", "name": "list_dir", "content": "x" * 2000}]
    for _ in range(5):
        s.messages.append({"role": "user", "content": "y"})
    changed = s.elide_old_tool_results(keep_recent=8)
    assert changed == 0


def test_elide_budget_constants():
    s = Session()
    assert s._elide_budget("write_file", False) == 800
    assert s._elide_budget("edit_file", False) == 800
    assert s._elide_budget("bash", False) == 300
    assert s._elide_budget("list_dir", False) == 180
    assert s._elide_budget("anything", True) == 400
