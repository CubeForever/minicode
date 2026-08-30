import json
import threading
import types
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from minicode.agent import Agent
from minicode.checkpoints import CheckpointManager
from minicode.config import Config
from minicode.session import Session
from minicode.tools import build_registry
from minicode.tools.base import ToolContext, ToolError
from minicode.tools.fs import EditFileTool, ReadFileTool, WriteFileTool
from minicode.tools.patch import ApplyPatchTool

from minicode.tools.shell import BackgroundShell, ShellState
from minicode.tools.webfetch import _assert_public_host
from minicode.ui import UI


def make_ctx(tmp_path, **cfg_kwargs):
    cfg = Config(provider="fake", api_key="", model="fake", **cfg_kwargs)
    cfg.cwd = tmp_path
    return ToolContext(cwd=tmp_path, config=cfg, session=Session(), ui=UI())


def make_agent(tmp_path, provider, mode="yolo", **cfg_kwargs) -> Agent:
    cfg = Config(provider="fake", api_key="", model="fake", mode=mode, **cfg_kwargs)
    cfg.cwd = tmp_path
    session = Session()
    return Agent(provider, session, UI(), cfg,
                 build_registry(ShellState(tmp_path, "bash")),
                 checkpoints=CheckpointManager(tmp_path / "ck"))


# ---------- CRLF 修复（Windows 关键 bug） ----------

def test_edit_file_on_crlf_file(tmp_path):
    ctx = make_ctx(tmp_path)
    p = tmp_path / "win.py"
    p.write_bytes("x = 1\r\nold line\r\ny = 2\r\n".encode())
    EditFileTool().run({"path": "win.py", "old_string": "old line",
                        "new_string": "new line"}, ctx)
    data = p.read_bytes()
    assert b"new line" in data and b"old line" not in data
    assert data.count(b"\r\n") == 3  # CRLF style fully preserved


def test_write_file_overwrite_preserves_crlf(tmp_path):
    ctx = make_ctx(tmp_path)
    p = tmp_path / "w.txt"
    p.write_bytes("a\r\nb\r\n".encode())
    WriteFileTool().run({"path": "w.txt", "content": "c\nd\n"}, ctx)
    assert p.read_bytes() == b"c\r\nd\r\n"


def test_apply_patch_on_crlf_file(tmp_path):
    ctx = make_ctx(tmp_path)
    p = tmp_path / "m.py"
    p.write_bytes("keep\r\nREMOVE ME\r\nkeep2\r\n".encode())
    patch = ("*** Begin Patch\n*** Update File: m.py\n"
             "-REMOVE ME\n+inserted\n*** End Patch")
    ApplyPatchTool().run({"patch": patch}, ctx)
    data = p.read_bytes()
    assert b"inserted" in data and b"REMOVE ME" not in data
    assert data.count(b"\r\n") == 3


def test_read_file_displays_crlf_cleanly(tmp_path):
    ctx = make_ctx(tmp_path)
    (tmp_path / "r.txt").write_bytes("one\r\ntwo\r\n".encode())
    out = ReadFileTool().run({"path": "r.txt"}, ctx)
    assert "one" in out and "\r" not in out


# ---------- web_fetch SSRF 防护 ----------

def test_ssrf_blocks_loopback(monkeypatch):
    import socket
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda host, port, **k: [(2, 1, 6, "", ("127.0.0.1", 0))])
    with pytest.raises(ToolError, match="private/loopback"):
        _assert_public_host("http://internal.example/", allow_private=False)


def test_ssrf_blocks_metadata_and_private_ranges(monkeypatch):
    import socket
    cases = [
        ("169.254.169.254", "cloud metadata"),
        ("10.0.0.5", "lan"),
        ("192.168.1.1", "lan"),
    ]
    for ip, _tag in cases:
        monkeypatch.setattr(socket, "getaddrinfo",
                            lambda host, port, _ip=ip, **k: [(2, 1, 6, "", (_ip, 0))])
        with pytest.raises(ToolError):
            _assert_public_host("http://host.example/", allow_private=False)


def test_ssrf_allows_public_and_respects_allow_private(monkeypatch):
    import socket
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda host, port, **k: [(2, 1, 6, "", ("93.184.216.34", 0))])
    assert _assert_public_host("http://example.com/", allow_private=False)
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda host, port, **k: [(2, 1, 6, "", ("127.0.0.1", 0))])
    assert _assert_public_host("http://local.dev/", allow_private=True) == "local.dev"


def test_ssrf_blocks_redirect_to_private(tmp_path, monkeypatch):
    """http server on loopback that redirects to another loopback URL must be blocked."""
    calls = []

    class RedirectHandler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            calls.append(self.path)
            if self.path == "/start":
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:9/metadata")
                self.end_headers()
            else:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"secret")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    port = httpd.server_address[1]
    try:
        tool = __import__("minicode.tools.webfetch", fromlist=["WebFetchTool"]).WebFetchTool()
        ctx = make_ctx(tmp_path, webfetch_allow_private=False)
        with pytest.raises(ToolError):
            tool.run({"url": f"http://127.0.0.1:{port}/start"}, ctx)
    finally:
        httpd.shutdown()


# ---------- 后台输出缓冲上限 ----------

def test_background_buffer_capped():
    sh = BackgroundShell.__new__(BackgroundShell)
    sh.buffer = []
    sh._size = 0
    sh.MAX_BUFFER = 1000
    sh.lock = threading.Lock()
    for i in range(200):
        sh._append("x" * 100)
    total = sum(len(x) for x in sh.buffer)
    assert total <= 1000 + 100  # keeps at most ~MAX_BUFFER chars


# ---------- POINTER 路径校验 ----------

def test_pointer_outside_sessions_dir_rejected(tmp_path, monkeypatch):
    from minicode import session as sess_mod
    evil = tmp_path / "evil.json"
    evil.write_text(json.dumps({"messages": [{"role": "user", "content": "injected"}]}),
                    encoding="utf-8")
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(sess_mod, "SESSIONS_DIR", sessions)
    monkeypatch.setattr(sess_mod, "POINTER", tmp_path / "LAST")
    (tmp_path / "LAST").write_text(str(evil), encoding="utf-8")
    s = Session.load_last()
    assert s is None  # outside pointer ignored


def test_pointer_inside_sessions_dir_works(tmp_path, monkeypatch):
    from minicode import session as sess_mod
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    good = sessions / "good.json"
    Session(messages=[{"role": "user", "content": "ok"}]).save(good)
    monkeypatch.setattr(sess_mod, "SESSIONS_DIR", sessions)
    monkeypatch.setattr(sess_mod, "POINTER", tmp_path / "LAST")
    (tmp_path / "LAST").write_text(str(good), encoding="utf-8")
    s = Session.load_last()
    assert s is not None and s.messages[0]["content"] == "ok"


# ---------- serve 未捕获异常返回 500 ----------

class CrashingAgent:
    class config:
        model = "m"
        provider = "openai"
        mode = "yolo"

    class session:
        todos = []
        messages = []

        @staticmethod
        def context_tokens():
            return 0

    @staticmethod
    def run_turn(prompt):
        raise RuntimeError("boom")


def test_server_returns_500_on_crash(tmp_path):
    from minicode.server import MinicodeServer
    srv = MinicodeServer(CrashingAgent(), host="127.0.0.1", port=0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{srv.port}"
    try:
        req = urllib.request.Request(
            f"{base}/api/turn", data=json.dumps({"prompt": "x"}).encode(),
            method="POST", headers={"X-Minicode-Token": srv.token,
                                    "Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(req, timeout=15)
        assert ei.value.code == 500
    finally:
        srv.shutdown()


import urllib.error  # noqa: E402  (kept at bottom to keep fixtures above readable)


# ---------- 信任门禁 ----------

def test_trust_gate_noninteractive_rejects(tmp_path, monkeypatch):
    from minicode import cli
    d = tmp_path / ".minicode" / "tools"
    d.mkdir(parents=True)
    (d / "p.py").write_text("def run(args, ctx):\n    return 'x'\n", encoding="utf-8")
    monkeypatch.setattr("minicode.cli.sys.stdin.isatty", lambda: False)
    monkeypatch.chdir(tmp_path)
    cfg = Config(provider="fake", api_key="", model="fake")
    cfg.cwd = tmp_path
    ui = UI()
    allow, _ = cli._project_trust_gate(cfg, ui)
    assert allow is False
    tools, errors = __import__("minicode.plugins",
                               fromlist=["load_plugin_tools"]).load_plugin_tools(tmp_path)
    assert len(tools) == 1  # loader itself works; cli gate decides whether to load


def test_trust_gate_interactive_accept_writes_marker(tmp_path, monkeypatch):
    from minicode import cli
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    d = tmp_path / ".minicode" / "tools"
    d.mkdir(parents=True)
    (d / "p.py").write_text("def run(args, ctx):\n    return 'x'\n", encoding="utf-8")
    fake_sys = types.SimpleNamespace(
        stdin=types.SimpleNamespace(isatty=lambda: True),
        stdout=types.SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("minicode.cli.sys", fake_sys)
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    monkeypatch.chdir(tmp_path)
    cfg = Config(provider="fake", api_key="", model="fake")
    cfg.cwd = tmp_path
    allow, trusted_now = cli._project_trust_gate(cfg, UI())
    assert allow is True and trusted_now is True
    markers = list((home / ".minicode" / "trusted").glob("*.json"))
    assert len(markers) == 1
    # second call: marker present -> silently allowed
    allow2, _ = cli._project_trust_gate(cfg, UI())
    assert allow2 is True


def test_trust_gate_strips_project_hooks_on_decline(tmp_path, monkeypatch):
    from minicode import cli
    monkeypatch.setattr("minicode.cli.sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: "n")
    monkeypatch.chdir(tmp_path)
    cfg = Config(provider="fake", api_key="", model="fake")
    cfg.cwd = tmp_path
    cfg.hooks = {"pre_tool_use": "evil.py", "post_tool_use": "user-own-hook"}
    cfg.hooks_from_project = {"pre_tool_use": "evil.py"}
    allow, _ = cli._project_trust_gate(cfg, UI())
    assert allow is False
    assert "pre_tool_use" not in cfg.hooks      # project hook stripped
    assert cfg.hooks.get("post_tool_use") == "user-own-hook"  # user hook kept
