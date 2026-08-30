import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from minicode.agent import Agent
from minicode.checkpoints import CheckpointManager
from minicode.config import Config
from minicode.fake import FakeProvider
from minicode.mcp import McpManager
from minicode.plugins import load_plugin_tools
from minicode.server import MinicodeServer
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


# ---------- 本地 Python 插件 ----------

def test_plugin_tools_loaded_and_run(tmp_path):
    d = tmp_path / ".minicode" / "tools"
    d.mkdir(parents=True)
    (d / "shout.py").write_text(
        'TOOL = {"name": "shout", "description": "大写喊话",\n'
        '        "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}},\n'
        '        "kind": "read"}\n'
        'def run(args, ctx):\n'
        '    return "!!!" + str(args.get("text", ""))\n', encoding="utf-8")
    (d / "broken.py").write_text("raise RuntimeError('boom')\n", encoding="utf-8")
    tools, errors = load_plugin_tools(tmp_path)
    names = [t.name for t in tools]
    assert "shout" in names
    assert any(e[0] == "broken.py" for e in errors)
    tool = next(t for t in tools if t.name == "shout")
    ctx = ToolContext(cwd=tmp_path, config=Config(), session=Session(), ui=UI())
    assert tool.run({"text": "hi"}, ctx) == "!!!hi"
    assert tool.kind == "read"


def test_plugin_in_registry(tmp_path):
    d = tmp_path / ".minicode" / "tools"
    d.mkdir(parents=True)
    (d / "hello.py").write_text(
        'def run(args, ctx):\n    return "hello"\n', encoding="utf-8")
    from minicode.plugins import load_plugin_tools
    tools, _ = load_plugin_tools(tmp_path)
    reg = build_registry(ShellState(tmp_path, "bash"))
    from minicode.tools.base import ToolRegistry
    full = ToolRegistry(list(reg.tools.values()) + tools)
    assert full.get("hello") is not None


# ---------- MCP HTTP 传输 ----------

class FakeMcpHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        ln = int(self.headers.get("Content-Length") or 0)
        req = json.loads(self.rfile.read(ln) or b"{}")
        method = req.get("method")
        rid = req.get("id")
        if rid is None:  # notification
            self.send_response(202)
            self.end_headers()
            return
        if method == "initialize":
            result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "http-echo", "version": "0.0.1"}}
        elif method == "tools/list":
            result = {"tools": [{"name": "echo",
                                 "description": "echo over http",
                                 "inputSchema": {"type": "object",
                                                 "properties": {"text": {"type": "string"}},
                                                 "required": ["text"]}}]}
        elif method == "tools/call":
            args = (req.get("params") or {}).get("arguments") or {}
            result = {"content": [{"type": "text",
                                   "text": "http-echo: " + str(args.get("text", ""))}],
                      "isError": False}
        else:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"jsonrpc": "2.0", "id": rid,
                                         "error": {"code": -32601,
                                                   "message": "unknown"}}).encode())
            return
        body = json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Mcp-Session-Id", "sess-42")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def http_mcp_url():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeMcpHandler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/mcp"
    httpd.shutdown()


def test_mcp_http_transport(tmp_path, http_mcp_url):
    mgr = McpManager({"remote": {"url": http_mcp_url}}, tmp_path)
    try:
        tools = mgr.connect_all()
        assert any(t.name == "mcp__remote__echo" for t in tools)
        tool = next(t for t in tools if t.name == "mcp__remote__echo")
        ctx = ToolContext(cwd=tmp_path, config=Config(), session=Session(), ui=UI())
        assert tool.run({"text": "over-http"}, ctx) == "http-echo: over-http"
        assert "connected (1 tools)" in "\n".join(mgr.status_lines())
        assert mgr.clients["remote"].session_id == "sess-42"
    finally:
        mgr.stop_all()


# ---------- minicode serve ----------

@pytest.fixture
def server(tmp_path):
    agent = make_agent(tmp_path, FakeProvider([{"text": "served: ok"}]))
    srv = MinicodeServer(agent, host="127.0.0.1", port=0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv, agent
    srv.shutdown()


def _post(url, payload, token):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json",
                 "X-Minicode-Token": token or ""})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def _get(url, token):
    req = urllib.request.Request(url, method="GET",
                                 headers={"X-Minicode-Token": token or ""})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def test_serve_health_and_auth(server):
    srv, _ = server
    base = f"http://127.0.0.1:{srv.port}"
    code, body = _get(f"{base}/api/health", srv.token)
    assert code == 200 and body["ok"] is True
    # wrong token -> 401
    code, _ = _post(f"{base}/api/turn", {"prompt": "hi"}, "wrong-token")
    assert code == 401


def test_serve_turn_and_status(server):
    srv, agent = server
    base = f"http://127.0.0.1:{srv.port}"
    code, body = _post(f"{base}/api/turn", {"prompt": "hello"}, srv.token)
    assert code == 200
    assert body["result"] == "served: ok"
    code, body = _get(f"{base}/api/status", srv.token)
    assert code == 200
    assert body["busy"] is False and body["messages"] == 2
