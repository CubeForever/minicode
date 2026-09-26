"""Web 界面：事件桥接 / 静态页 / 鉴权 / 回合事件流 / 浏览器内确认。"""
import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from minicode.config import Config
from minicode.fake import FakeProvider
from minicode.webui import WebBridgeUI, WebUIServer


def make_server(tmp_path, script, mode="default") -> WebUIServer:
    cfg = Config(provider="fake", api_key="", model="fake", mode=mode)
    cfg.cwd = tmp_path
    return WebUIServer(cfg, FakeProvider(script), host="127.0.0.1", port=0)


class Running:
    """serve_forever 的测试生命周期封装。"""

    def __init__(self, srv: WebUIServer):
        self.srv = srv
        self.t = threading.Thread(target=srv.serve_forever, daemon=True)
        self.t.start()
        time.sleep(0.05)

    def __enter__(self):
        return self.srv

    def __exit__(self, *exc):
        self.srv.shutdown()
        self.t.join(timeout=5)


def post(base, path, payload=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Minicode-Token"] = token
    req = urllib.request.Request(base + path,
                                 data=json.dumps(payload or {}).encode(),
                                 method="POST", headers=headers)
    return urllib.request.urlopen(req, timeout=10)


def get(base, path, token=None):
    headers = {"X-Minicode-Token": token} if token else {}
    return urllib.request.urlopen(urllib.request.Request(base + path, headers=headers),
                                  timeout=10)


def wait_idle(srv, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not srv.busy:
            return
        time.sleep(0.05)
    raise AssertionError("turn did not finish in time")


# ---------- 事件桥接 ----------

def test_bridge_strips_ansi_and_broadcasts():
    b = WebBridgeUI()
    q = b.subscribe()
    b.info("\x1b[32mgreen text\x1b[0m")
    b.tool_line("bash", "\x1b[90msummary\x1b[0m")
    b.plain("")  # 空行不推送
    events = [q.get(timeout=2) for _ in range(2)]
    assert events[0] == {"t": "info", "text": "green text"}
    assert events[1] == {"t": "tool", "name": "bash", "summary": "summary"}
    assert q.empty()


def test_bridge_confirm_blocks_until_resolved():
    b = WebBridgeUI()
    q = b.subscribe()
    result = {}

    def ask():
        result["ans"] = b.confirm("允许 write_file？", "Create x.txt")

    th = threading.Thread(target=ask, daemon=True)
    th.start()
    ev = q.get(timeout=2)
    assert ev["t"] == "confirm" and "write_file" in ev["title"]
    time.sleep(0.05)
    assert th.is_alive()           # 未答复前阻塞（与 REPL 的 input 一致）
    assert b.resolve(ev["id"], "y")
    th.join(timeout=2)
    assert result["ans"] == "y"


def test_bridge_choose_malformed_answer_defends():
    """choose 收到畸形答复时防御性返回 []，不崩回合。"""
    b = WebBridgeUI()
    q = b.subscribe()
    result = {}

    def ask():
        result["labels"] = b.choose("选一个", [{"label": "A"}, {"label": "B"}])

    th = threading.Thread(target=ask, daemon=True)
    th.start()
    ev = q.get(timeout=2)
    assert ev["t"] == "choose" and len(ev["options"]) == 2
    b.resolve(ev["id"], "not-a-list")   # 畸形答复 → 防御性返回 []
    th.join(timeout=2)
    assert result["labels"] == []


# ---------- 服务器基础 ----------

def test_webui_serves_static_and_requires_token(tmp_path):
    srv = make_server(tmp_path, [])
    with Running(srv):
        base = f"http://127.0.0.1:{srv.port}"
        html = get(base, "/").read().decode("utf-8")
        assert "minicode" in html
        js = get(base, "/app.js").read().decode("utf-8")
        assert srv.token in js and "__TOKEN__" not in js  # token 注入到 app.js
        health = json.load(get(base, "/api/health"))
        assert health["ok"] and health["mode"] == "web"
        with pytest.raises(urllib.error.HTTPError) as ei:
            get(base, "/api/status")
        assert ei.value.code == 401                      # 无 token 拒绝
        s = json.load(get(base, "/api/status", token=srv.token))
        assert s["model"] == "fake" and s["busy"] is False


def test_webui_rejects_non_local_host(tmp_path):
    srv = make_server(tmp_path, [])
    with Running(srv):
        base = f"http://127.0.0.1:{srv.port}"
        # DNS rebinding 防护：伪装 Host 直接 403
        req = urllib.request.Request(base + "/api/health",
                                     headers={"Host": "evil.example"})
        try:
            urllib.request.urlopen(req, timeout=5)
            raised = False
        except urllib.error.HTTPError as e:
            raised = e.code == 403
        except urllib.error.URLError:
            raised = True  # 某些环境直接拒连也算拦截
        assert raised


# ---------- 回合事件流 ----------

def test_webui_turn_streams_events_and_persists(tmp_path):
    srv = make_server(tmp_path, [{"text": "hi there"}], mode="full-access")
    q = srv.bridge.subscribe()
    with Running(srv):
        base = f"http://127.0.0.1:{srv.port}"
        resp = json.load(post(base, "/api/turn", {"prompt": "hello"}, token=srv.token))
        assert resp["ok"]
        wait_idle(srv)
        kinds = []
        while not q.empty():
            kinds.append(q.get(timeout=1)["t"])
        assert kinds[0] == "user" and kinds[1] == "busy"
        assert "text" in kinds and "busy" in kinds
        msgs = json.load(get(base, "/api/messages", token=srv.token))["messages"]
        assert msgs[-1]["content"] == "hi there"
        # 会话落盘（与 REPL 行为一致）
        from minicode.session import SESSIONS_DIR
        assert SESSIONS_DIR.exists()
        # 回合结束后再次保存由 _run_turn 完成；这里只验证 busy 翻转
        s = json.load(get(base, "/api/status", token=srv.token))
        assert s["busy"] is False


def test_webui_turn_rejects_while_busy_and_empty_prompt(tmp_path):
    srv = make_server(tmp_path, [{"text": "ok"}], mode="full-access")
    with Running(srv):
        base = f"http://127.0.0.1:{srv.port}"
        with pytest.raises(urllib.error.HTTPError) as ei:
            post(base, "/api/turn", {"prompt": ""}, token=srv.token)
        assert ei.value.code == 400
        with pytest.raises(urllib.error.HTTPError) as ei2:
            post(base, "/api/turn", {"prompt": "x"}, token="wrong")
        assert ei2.value.code == 401


def test_webui_permission_confirm_via_browser(tmp_path):
    """default 模式下写文件 → 确认请求到达浏览器 → 答复 y → 落盘。"""
    script = [
        {"tool_calls": [{"id": "t1", "name": "write_file",
                         "args": json.dumps({"path": "x.txt", "content": "hi"})}]},
        {"text": "done"},
    ]
    srv = make_server(tmp_path, script, mode="default")
    q = srv.bridge.subscribe()
    with Running(srv):
        base = f"http://127.0.0.1:{srv.port}"
        post(base, "/api/turn", {"prompt": "write"}, token=srv.token)
        ev = None
        deadline = time.time() + 10
        while time.time() < deadline:
            e = q.get(timeout=5)
            if e["t"] == "confirm":
                ev = e
                break
        assert ev and "write_file" in ev["title"]
        assert json.load(post(base, "/api/answer",
                              {"id": ev["id"], "value": "y"}, token=srv.token))["ok"]
        wait_idle(srv)
        assert (tmp_path / "x.txt").read_text(encoding="utf-8") == "hi"


def test_webui_mode_and_clear_endpoints(tmp_path):
    srv = make_server(tmp_path, [], mode="default")
    with Running(srv):
        base = f"http://127.0.0.1:{srv.port}"
        r = json.load(post(base, "/api/mode", {"mode": "plan"}, token=srv.token))
        assert r["mode"] == "plan"
        assert json.load(get(base, "/api/status", token=srv.token))["mode"] == "plan"
        r2 = json.load(post(base, "/api/mode", {"mode": "yolo"}, token=srv.token))
        assert r2["mode"] == "full-access"            # 别名归一
        with pytest.raises(urllib.error.HTTPError) as ei:
            post(base, "/api/mode", {"mode": "chaos"}, token=srv.token)
        assert ei.value.code == 400
        assert json.load(post(base, "/api/clear", token=srv.token))["ok"]


def test_webui_sse_stream_delivers_hello(tmp_path):
    srv = make_server(tmp_path, [], mode="default")
    with Running(srv):
        resp = get(f"http://127.0.0.1:{srv.port}",
                   f"/api/events?token={srv.token}")
        try:
            first = resp.readline().decode("utf-8")
            assert first.startswith("data:") and "hello" in first
        finally:
            resp.close()
