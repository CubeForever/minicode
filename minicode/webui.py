"""minicode web ui — 本地浏览器界面（仅标准库，零依赖）。

对标桌面端聊天体验：SSE 流式事件、工具调用卡片、思考过程折叠、
浏览器内权限确认（y/a/n）与 ask_user 选项卡片。
启动：minicode --ui   （仅绑定 127.0.0.1，token 鉴权，防 DNS rebinding）

事件流（SSE GET /api/events）：
    user / text / reason / tool / result / info / warn / error / plain
    tokens / todos / busy / confirm / choose / cleared
交互回传（POST /api/answer {id, value}）：value 为 "y"/"a"/"n" 或选项列表。
"""
from __future__ import annotations

import hmac
import json
import re
import secrets
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Empty, Queue
from typing import List
from urllib.parse import parse_qs, urlparse

from . import session as session_mod
from .agent import MODES
from .session import Session
from .ui import UI

WEB_DIR = Path(__file__).parent / "web"
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
}


def _clean(text) -> str:
    """UI 层的字符串可能带 ANSI 颜色码，浏览器里要去掉。"""
    return _ANSI.sub("", str(text or ""))


class WebBridgeUI(UI):
    """UI 实现：渲染调用 → JSON 事件广播给所有 SSE 订阅者；
    confirm/choose → 浏览器交互卡片，阻塞等答复后按 REPL 同样的语义返回。"""

    def __init__(self):
        super().__init__()
        self._subs: List[Queue] = []
        self._lock = threading.Lock()
        self._pending = {}  # id -> (Event, {"value": ...})

    # ---------- 事件总线 ----------
    def subscribe(self) -> Queue:
        q: Queue = Queue(maxsize=20000)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def _emit(self, event: dict) -> None:
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except Exception:
                pass  # 单个订阅者慢/断开不影响其他订阅者和回合推进

    # ---------- 浏览器交互（confirm / choose） ----------
    def _ask(self, kind: str, payload: dict):
        ask_id = uuid.uuid4().hex
        box = {"value": None}
        ev = threading.Event()
        with self._lock:
            self._pending[ask_id] = (ev, box)
        self._emit({"t": kind, "id": ask_id, **payload})
        ev.wait()  # 与 REPL 的 input() 一样阻塞，直到浏览器答复
        with self._lock:
            self._pending.pop(ask_id, None)
        return box["value"]

    def resolve(self, ask_id: str, value) -> bool:
        with self._lock:
            pending = self._pending.get(ask_id)
        if not pending:
            return False
        ev, box = pending
        box["value"] = value
        ev.set()
        return True

    # ---------- UI 覆写：渲染 → 事件 ----------
    def stream_text(self, text):
        self._emit({"t": "text", "text": text})

    def stream_reasoning(self, text):
        self._emit({"t": "reason", "text": text})

    def tool_line(self, name, summary="", prefix="  ● "):
        self._emit({"t": "tool", "name": name, "summary": _clean(summary)})

    def tool_result_note(self, text):
        self._emit({"t": "result", "text": _clean(text)})

    def info(self, msg):
        self._emit({"t": "info", "text": _clean(msg)})

    def warn(self, msg):
        self._emit({"t": "warn", "text": _clean(msg)})

    def error(self, msg):
        self._emit({"t": "error", "text": _clean(msg)})

    def plain(self, msg=""):
        if msg:
            self._emit({"t": "plain", "text": _clean(msg)})

    def token_note(self, in_tok, out_tok, pct=None):
        self._emit({"t": "tokens", "in": in_tok, "out": out_tok, "pct": pct})

    def todo_render(self, todos):
        self._emit({"t": "todos", "todos": list(todos or [])})

    def confirm(self, title, preview=None):
        ans = self._ask("confirm", {"title": _clean(title),
                                    "preview": _clean(preview or "")})
        return ans if ans in ("y", "a", "n") else "n"

    def choose(self, question, options, multi=False, allow_other=True):
        opts = []
        for o in options or []:
            if not isinstance(o, dict):
                o = {"label": str(o)}
            opts.append({"label": str(o.get("label", "")),
                         "description": str(o.get("description", "")),
                         "preview": str(o.get("preview") or "")[:2000]})
        labels = self._ask("choose", {"question": _clean(question), "options": opts,
                                      "multi": bool(multi),
                                      "allow_other": bool(allow_other)})
        return labels if isinstance(labels, list) else []


class WebUIServer:
    """本地 Web 界面服务器：静态页 + SSE 事件流 + 回合/确认 API。"""

    def __init__(self, cfg, provider, session=None, mcp_manager=None,
                 host="127.0.0.1", port=8765):
        from .cli import _build_agent  # 延迟导入避免循环
        self.cfg = cfg
        self.bridge = WebBridgeUI()
        self.agent = _build_agent(cfg, provider, session or Session(),
                                  self.bridge, mcp_manager)
        self.host = host
        self.busy = False
        self.token = secrets.token_hex(16)
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            # ---------- 基础 ----------
            def _json(self, code: int, payload: dict):
                body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _forbidden(self):
                self._json(403, {"error": "forbidden host"})

            def _host_ok(self) -> bool:
                # 防 DNS rebinding：只接受本机 Host
                host = (self.headers.get("Host") or "").split(":")[0]
                return host in ("127.0.0.1", "localhost")

            def _token_ok(self, query: dict) -> bool:
                supplied = (self.headers.get("X-Minicode-Token")
                            or (query.get("token") or [""])[0])
                return hmac.compare_digest(str(supplied or ""), outer.token)

            def _body(self) -> dict:
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = 0
                if not length:
                    return {}
                try:
                    data = json.loads(self.rfile.read(length).decode("utf-8"))
                    return data if isinstance(data, dict) else {}
                except (ValueError, json.JSONDecodeError):
                    return {}

            def _query(self) -> dict:
                return parse_qs(urlparse(self.path).query)

            # ---------- GET ----------
            def do_GET(self):
                if not self._host_ok():
                    return self._forbidden()
                parsed = urlparse(self.path)
                path, query = parsed.path, self._query()
                if path in _STATIC:
                    fname, ctype = _STATIC[path]
                    return self._static(fname, ctype)
                if path == "/api/health":
                    return self._json(200, {"ok": True, "mode": "web"})
                if not self._token_ok(query):
                    return self._json(401, {"error": "unauthorized"})
                if path == "/api/events":
                    return self._sse()
                if path == "/api/status":
                    s = outer.agent.session
                    return self._json(200, {
                        "model": outer.cfg.model,
                        "provider": outer.cfg.provider,
                        "mode": outer.cfg.mode,
                        "messages": len(s.messages),
                        "context_tokens": s.context_tokens(),
                        "context_limit": outer.cfg.context_limit,
                        "usage": dict(s.total_usage),
                        "busy": outer.busy,
                        "version": outer._version()})
                if path == "/api/messages":
                    s = outer.agent.session
                    return self._json(200, {"messages": s.messages,
                                            "todos": s.todos})
                if path == "/api/sessions":
                    return self._json(200, {"sessions": outer._list_sessions()})
                return self._json(404, {"error": "not found"})

            def _static(self, fname: str, ctype: str):
                page = (WEB_DIR / fname).read_text(encoding="utf-8")
                if "__TOKEN__" in page:  # 每个进程注入随机 token（占位符在 app.js）
                    page = page.replace("__TOKEN__", outer.token)
                if "__VERSION__" in page:
                    page = page.replace("__VERSION__", outer._version())
                body = page.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _sse(self):
                q = outer.bridge.subscribe()
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    self.wfile.write(b"data: {\"t\": \"hello\"}\n\n")
                    self.wfile.flush()
                    while True:
                        try:
                            ev = q.get(timeout=15)
                        except Empty:
                            ev = None
                            self.wfile.write(b": ping\n\n")
                        if ev is not None:
                            line = json.dumps(ev, ensure_ascii=False, default=str)
                            self.wfile.write(f"data: {line}\n\n".encode("utf-8"))
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
                    pass
                finally:
                    outer.bridge.unsubscribe(q)

            # ---------- POST ----------
            def do_POST(self):
                if not self._host_ok():
                    return self._forbidden()
                path, query = urlparse(self.path).path, self._query()
                if path == "/api/health":
                    return self._json(200, {"ok": True})
                if not self._token_ok(query):
                    return self._json(401, {"error": "unauthorized"})
                body = self._body()
                if path == "/api/turn":
                    prompt = str(body.get("prompt") or "").strip()
                    if not prompt:
                        return self._json(400, {"error": "prompt is required"})
                    with outer._lock:
                        if outer.busy:
                            return self._json(409, {"error": "a turn is already running"})
                        outer.busy = True
                    threading.Thread(target=outer._work, args=(prompt,),
                                     daemon=True).start()
                    return self._json(200, {"ok": True})
                if path == "/api/answer":
                    ask_id = str(body.get("id") or "")
                    value = body.get("value")
                    if outer.bridge.resolve(ask_id, value):
                        return self._json(200, {"ok": True})
                    return self._json(404, {"error": "unknown ask id"})
                if path == "/api/mode":
                    mode = str(body.get("mode") or "")
                    if mode == "yolo":
                        mode = "full-access"
                    if mode not in MODES:
                        return self._json(400, {"error": f"unknown mode: {mode}"})
                    outer.cfg.mode = mode
                    outer._emit({"t": "info",
                                 "text": f"权限模式：{mode}"})
                    return self._json(200, {"ok": True, "mode": mode})
                if path == "/api/clear":
                    outer.agent.reset_session()
                    outer._emit({"t": "cleared"})
                    return self._json(200, {"ok": True})
                if path == "/api/session/open":
                    name = str(body.get("name") or "")
                    if not re.fullmatch(r"[\w\-]{1,120}", name):
                        return self._json(400, {"error": "invalid session name"})
                    path_ = session_mod.SESSIONS_DIR / f"{name}.json"
                    if not path_.exists():
                        return self._json(404, {"error": "session not found"})
                    try:
                        loaded = Session.load(path_)
                    except (OSError, ValueError) as e:
                        return self._json(500, {"error": f"load failed: {e}"})
                    if outer.busy:
                        return self._json(409, {"error": "a turn is running"})
                    outer.agent.replace_session(loaded)
                    return self._json(200, {"ok": True, "name": name,
                                            "messages": len(loaded.messages)})
                if path == "/api/compact":
                    if outer.busy:
                        return self._json(409, {"error": "a turn is running"})
                    try:
                        stats = outer.agent.compact()
                        return self._json(200, {"ok": True, **stats})
                    except Exception as e:
                        return self._json(500, {"error": str(e)})
                return self._json(404, {"error": "not found"})

        self._httpd = ThreadingHTTPServer((self.host, port), Handler)
        self.port = self._httpd.server_address[1]

    # ---------- helpers ----------
    @staticmethod
    def _version() -> str:
        from . import __version__
        return __version__

    @staticmethod
    def _list_sessions(limit: int = 50) -> list:
        """~/.minicode/sessions 里的历史会话（新→旧），供侧边栏。"""
        out = []
        for p in session_mod.Session.list_sessions(limit):
            stamp, _, slug = p.stem.partition("_")
            title = slug if slug and slug != "session" else stamp
            try:
                when = time.strftime("%m-%d %H:%M",
                                     time.localtime(p.stat().st_mtime))
            except OSError:
                when = ""
            out.append({"name": p.stem, "title": title[:60], "time": when})
        return out

    def _emit(self, event: dict) -> None:
        self.bridge._emit(event)

    def _work(self, prompt: str) -> None:
        """回合线程：复用 REPL 的 _run_turn（保存会话/自检门禁/复盘/
        自动压缩全套行为保持一致）。"""
        from .cli import _expand_mentions, _run_turn
        self._emit({"t": "user", "text": prompt})
        self._emit({"t": "busy", "busy": True})
        try:
            _run_turn(self.agent, _expand_mentions(prompt, self.agent))
        except Exception as e:  # _run_turn 已兜底，这里防桥接层意外
            self._emit({"t": "error", "text": f"{type(e).__name__}: {e}"})
        finally:
            self.busy = False
            self._emit({"t": "busy", "busy": False})

    def serve_forever(self):
        self._httpd.serve_forever()

    def shutdown(self):
        threading.Thread(target=self._httpd.shutdown, daemon=True).start()
