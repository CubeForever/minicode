import base64
import json
import sys
from pathlib import Path

import pytest

from minicode.config import Config
from minicode.mcp import McpManager
from minicode.session import Session
from minicode.tools.base import ToolContext, ToolError
from minicode.tools.fs import EditFileTool, ReadFileTool
from minicode.tools.notebook import NotebookEditTool
from minicode.tools.plan import ExitPlanTool
from minicode.tools.websearch import parse_results
from minicode.agent import rule_matches
from minicode.llm import _to_anthropic_messages, _to_openai_messages

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
    "AAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


def make_ctx(tmp_path):
    cfg = Config(provider="fake", api_key="", model="fake")
    cfg.cwd = tmp_path
    session = Session()
    return ToolContext(cwd=tmp_path, config=cfg, session=session, ui=UI__t())


class UI__t:
    """Minimal UI stub for tool runs."""

    def plain(self, msg=""):
        pass

    def newline(self):
        pass

    def todo_render(self, todos):
        pass

    def choose(self, question, options, multi=False):
        return []


# ---------- MCP ----------

def test_mcp_end_to_end(tmp_path):
    server = Path(__file__).parent / "mcp_echo_server.py"
    mgr = McpManager({"echo": {"command": sys.executable, "args": [str(server)]}},
                     tmp_path)
    try:
        tools = mgr.connect_all()
        assert any(t.name == "mcp__echo__echo" for t in tools)
        tool = next(t for t in tools if t.name == "mcp__echo__echo")
        out = tool.run({"text": "hi"}, make_ctx(tmp_path))
        assert out == "echo: hi"
        status = "\n".join(mgr.status_lines())
        assert "connected (1 tools)" in status
        assert "mcp__echo__echo" in status
    finally:
        mgr.stop_all()


def test_mcp_failed_server(tmp_path):
    mgr = McpManager({"bad": {"command": "definitely-not-a-real-binary-xyz"}}, tmp_path)
    try:
        tools = mgr.connect_all()
        assert tools == []
        assert mgr.clients["bad"].status == "failed"
    finally:
        mgr.stop_all()


# ---------- image input ----------

def test_read_file_returns_image_block(tmp_path):
    ctx = make_ctx(tmp_path)
    (tmp_path / "pixel.png").write_bytes(PNG_1PX)
    result = ReadFileTool().run({"path": "pixel.png"}, ctx)
    assert isinstance(result, dict) and "_blocks" in result
    img = result["_blocks"][0]
    assert img["type"] == "image" and img["media_type"] == "image/png"
    assert base64.b64decode(img["data"]) == PNG_1PX


def test_image_blocks_wire_conversion():
    content = [{"type": "image", "media_type": "image/png", "data": "QUJD"},
               {"type": "text", "text": "a tiny image"}]
    msgs = [{"role": "user", "content": "hi"},
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "t1", "name": "read_file", "args": "{}"}]},
            {"role": "tool", "tool_call_id": "t1", "name": "read_file",
             "content": content, "is_error": False}]
    wire = _to_anthropic_messages(msgs)
    tb = wire[-1]["content"][0]
    assert tb["type"] == "tool_result"
    assert tb["content"][0]["type"] == "image"
    assert tb["content"][0]["source"]["data"] == "QUJD"
    assert tb["content"][1] == {"type": "text", "text": "a tiny image"}

    oai = _to_openai_messages(msgs, None)
    tool_part = oai[-2]
    assert tool_part["role"] == "tool"
    assert all(p["type"] == "text" for p in tool_part["content"])
    relayed = oai[-1]
    assert relayed["role"] == "user"
    assert relayed["content"][1]["type"] == "image_url"
    assert relayed["content"][1]["image_url"]["url"].startswith("data:image/png;base64,QUJD")


# ---------- stale read check ----------

def test_stale_read_rejected(tmp_path):
    ctx = make_ctx(tmp_path)
    p = tmp_path / "s.txt"
    p.write_text("hello world\n", encoding="utf-8")
    ReadFileTool().run({"path": "s.txt"}, ctx)
    p.write_text("changed externally\n", encoding="utf-8")
    os_utime(p)
    with pytest.raises(ToolError, match="modified since"):
        EditFileTool().run({"path": "s.txt", "old_string": "hello", "new_string": "bye"},
                           ctx)
    ReadFileTool().run({"path": "s.txt"}, ctx)  # re-read clears staleness
    EditFileTool().run({"path": "s.txt", "old_string": "changed",
                        "new_string": "fixed"}, ctx)
    assert "fixed" in p.read_text()


def os_utime(p: Path):
    import os
    st = p.stat()
    os.utime(p, (st.st_atime + 20, st.st_mtime + 20))


# ---------- notebook ----------

def _notebook(tmp_path):
    nb = {"cells": [
        {"cell_type": "code", "id": "c1", "metadata": {}, "execution_count": None,
         "outputs": [], "source": ["print(1)"]},
        {"cell_type": "markdown", "id": "c2", "metadata": {}, "source": ["# hi"]},
    ], "metadata": {}, "nbformat": 4, "nbformat_minor": 5}
    p = tmp_path / "n.ipynb"
    p.write_text(json.dumps(nb), encoding="utf-8")
    return p


def test_notebook_replace_insert_delete(tmp_path):
    ctx = make_ctx(tmp_path)
    p = _notebook(tmp_path)
    NotebookEditTool().run({"notebook_path": "n.ipynb", "cell_id": "c1",
                            "new_source": "print(42)"}, ctx)
    nb = json.loads(p.read_text(encoding="utf-8"))
    assert "".join(nb["cells"][0]["source"]) == "print(42)"
    assert nb["cells"][0]["outputs"] == []

    NotebookEditTool().run({"notebook_path": "n.ipynb", "cell_id": "c1",
                            "new_source": "# doc", "cell_type": "markdown",
                            "edit_mode": "insert"}, ctx)
    nb = json.loads(p.read_text(encoding="utf-8"))
    assert len(nb["cells"]) == 3
    assert nb["cells"][1]["cell_type"] == "markdown"

    NotebookEditTool().run({"notebook_path": "n.ipynb", "cell_id": "c1",
                            "new_source": "", "edit_mode": "delete"}, ctx)
    nb = json.loads(p.read_text(encoding="utf-8"))
    assert len(nb["cells"]) == 2


# ---------- websearch parser ----------

def test_parse_ddg_results():
    html = """
    <div class="result">
      <a rel="nofollow" class="result__a"
         href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa&rut=x">Result One</a>
      <a class="result__snippet" href="#">Snippet one text</a>
    </div>
    <div class="result">
      <a class="result__a" href="https://direct.com/b">Result Two</a>
      <a class="result__snippet" href="#">Second snippet</a>
    </div>
    """
    results = parse_results(html)
    assert len(results) == 2
    assert results[0]["title"] == "Result One"
    assert results[0]["url"] == "https://example.com/a"
    assert "Snippet one" in results[0]["snippet"]
    assert results[1]["url"] == "https://direct.com/b"


# ---------- memory @imports ----------

def test_memory_imports(tmp_path, monkeypatch):
    (tmp_path / "inc.md").write_text("导入的规则内容", encoding="utf-8")
    (tmp_path / "MINICODE.md").write_text("# 项目\n@inc.md\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    from minicode.prompts import build_system_prompt
    cfg = Config(provider="fake", api_key="", model="fake")
    cfg.cwd = tmp_path
    prompt = build_system_prompt(cfg, tmp_path)
    assert "导入的规则内容" in prompt


# ---------- custom agents ----------

def test_custom_agents_loader(tmp_path, monkeypatch):
    d = tmp_path / ".minicode" / "agents"
    d.mkdir(parents=True)
    (d / "scout.md").write_text(
        "---\ndescription: 代码侦察兵\ntools: read_file, grep\nmodel: glm-4.6\n---\n"
        "你是侦察兵，快速摸清代码结构。", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    from minicode.cli import _custom_agents
    agents = _custom_agents()
    assert "scout" in agents
    a = agents["scout"]
    assert a["description"] == "代码侦察兵"
    assert a["tools"] == "read_file, grep"
    assert a["model"] == "glm-4.6"
    assert "侦察兵" in a["prompt"]


# ---------- exit_plan tool ----------

def test_exit_plan_tool(tmp_path):
    ctx = make_ctx(tmp_path)
    r = ExitPlanTool().run({"plan": "1. 改 a.py\n2. 跑测试"}, ctx)
    assert "Stop now" in r
    with pytest.raises(ToolError):
        ExitPlanTool().run({"plan": ""}, ctx)


# ---------- permission wildcard for mcp ----------

class FakeTool:
    def __init__(self, name):
        self.name = name


def test_rule_matches_mcp_wildcard():
    t = FakeTool("mcp__echo__echo")
    assert rule_matches("mcp__echo__*", t, {})
    assert rule_matches("mcp__echo__echo", t, {})
    assert not rule_matches("mcp__other__*", t, {})


# ---------- headless json output ----------

def test_print_output_format_json(tmp_path, monkeypatch, capsys):
    script = tmp_path / "fake.json"
    script.write_text(json.dumps([{"text": "hello json"}]), encoding="utf-8")
    monkeypatch.setenv("MINICODE_FAKE_LLM", str(script))
    monkeypatch.setenv("OPENAI_API_KEY", "dummy")  # not used, fake wins
    monkeypatch.chdir(tmp_path)
    from minicode import cli
    rc = cli.main(["-p", "go", "--yolo", "--output-format", "json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["result"] == "hello json"
    assert payload["messages"][0]["role"] == "user"
