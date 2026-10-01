"""v0.19：/hooks 查看面板、MCP stdio 服务器崩溃自动重启。"""
import sys
import textwrap

import pytest

from minicode.agent import Agent
from minicode.config import Config
from minicode.fake import FakeProvider
from minicode.mcp import McpManager
from minicode.session import Session
from minicode.tools import build_registry
from minicode.tools.shell import ShellState
from minicode.ui import UI


def make_agent(tmp_path, provider, mode="default", **cfg_kwargs) -> Agent:
    cfg = Config(provider="fake", api_key="", model="fake", mode=mode, **cfg_kwargs)
    cfg.cwd = tmp_path
    return Agent(provider, Session(), UI(), cfg,
                 build_registry(ShellState(tmp_path, "bash")))


# ---------- /hooks 查看面板 ----------

def test_hooks_panel_lists_rules(tmp_path, capsys):
    agent = make_agent(tmp_path, FakeProvider([{"text": "ok"}]), hooks={
        "pre_tool_use": [
            {"matcher": "Bash|edit_file", "command": "guard.py", "timeout": 10},
        ],
        "stop": "notify.sh",
    })
    from minicode.cli import _command
    _command("/hooks", agent, agent.ui, tmp_path)
    out = capsys.readouterr().out
    assert "pre_tool_use" in out and "stop" in out
    assert "guard.py" in out and "matcher: Bash|edit_file" in out
    assert "timeout: 10s" in out and "notify.sh" in out
    assert "session_start" not in out          # 未配置的事件不显示


def test_hooks_panel_empty_shows_guide(tmp_path, capsys):
    agent = make_agent(tmp_path, FakeProvider([{"text": "ok"}]))
    from minicode.cli import _command
    _command("/hooks", agent, agent.ui, tmp_path)
    out = capsys.readouterr().out
    assert "未配置任何 hooks" in out
    assert "pre_compact" in out and "block" in out   # 事件清单 + 决策协议提示


def test_hooks_panel_web_dispatcher(tmp_path):
    """Web 端 /hooks 走 _dispatch_command，返回纯文本（无 ANSI）。"""
    from minicode.webui import WebUIServer
    cfg = Config(provider="fake", api_key="", model="fake", mode="default",
                 hooks={"post_tool_use": "log.sh"})
    cfg.cwd = tmp_path
    srv = WebUIServer(cfg, FakeProvider([]), host="127.0.0.1", port=0)
    try:
        r = srv._dispatch_command("hooks")
        assert "post_tool_use" in r["output"] and "log.sh" in r["output"]
        assert "\x1b" not in r["output"]
    finally:
        srv.shutdown()


# ---------- MCP 自动重连 ----------

_MCP_ECHO = textwrap.dedent("""
    import json, sys
    def send(obj):
        sys.stdout.write(json.dumps(obj) + "\\n")
        sys.stdout.flush()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        rid = msg.get("id")
        if rid is None:
            continue
        method = msg.get("method")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}}}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": rid, "result": {"tools": [
                {"name": "echo", "description": "echo",
                 "inputSchema": {"type": "object", "properties": {}}}]}})
        elif method == "tools/call":
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": "echo-ok"}], "isError": False}})
    """)


def test_mcp_auto_restart_after_crash(tmp_path):
    """服务器进程被杀 → 下一次工具调用透明重启并成功（此前会永久失败）。"""
    stub = tmp_path / "echo_server.py"
    stub.write_text(_MCP_ECHO, encoding="utf-8")
    mgr = McpManager({"flaky": {"command": sys.executable,
                                "args": [str(stub)], "timeout": 8}}, tmp_path)
    try:
        tools = mgr.connect_all()
        tool = next(t for t in tools if t.name == "mcp__flaky__echo")
        client = mgr.clients["flaky"]
        assert client.restart_count == 0
        client.proc.kill()                      # 模拟服务器崩溃
        client.proc.wait(timeout=5)
        out = tool.run({}, None)                # 自动重启并重试
        assert out == "echo-ok"
        assert client.restart_count == 1
        assert "自动重启 1 次" in "\n".join(mgr.status_lines())
    finally:
        mgr.stop_all()


def test_mcp_no_restart_after_explicit_stop(tmp_path):
    """显式 stop 后不再自动重启（避免退出时复活进程）。"""
    stub = tmp_path / "echo_server.py"
    stub.write_text(_MCP_ECHO, encoding="utf-8")
    mgr = McpManager({"x": {"command": sys.executable,
                            "args": [str(stub)], "timeout": 8}}, tmp_path)
    try:
        tools = mgr.connect_all()
        tool = next(t for t in tools if t.name == "mcp__x__echo")
        client = mgr.clients["x"]
        mgr.stop_all()                          # 显式停止
        with pytest.raises(Exception):
            tool.run({}, None)
        assert client.restart_count == 0
    finally:
        mgr.stop_all()
