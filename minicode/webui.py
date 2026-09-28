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
import shutil
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Empty, Queue
from typing import List, Optional
from urllib.parse import parse_qs, urlparse

from . import session as session_mod
from .agent import MODES
from .llm import LLMError
from .session import Session
from .ui import UI

WEB_DIR = Path(__file__).parent / "web"
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_WEB = ("index.html", "text/html; charset=utf-8")
_STATIC = {
    "/": _WEB,
    "/index.html": _WEB,
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
}
WORKSPACES_FILE = Path.home() / ".minicode" / "workspaces.json"
_SESSION_NAME = re.compile(r"[\w\-]{1,120}")
COOKIE_NAME = "minicode_token"


def _load_workspaces() -> dict:
    try:
        data = json.loads(WORKSPACES_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_workspaces(data: dict) -> None:
    WORKSPACES_FILE.parent.mkdir(parents=True, exist_ok=True)
    WORKSPACES_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                               encoding="utf-8")


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
        self._stop_check = None  # callable -> bool，回合停止时解除确认阻塞

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
        while not ev.wait(0.2):
            # 回合被停止时，未答复的卡片按「拒绝」处理，避免 agent 线程卡死
            if self._stop_check is not None and self._stop_check():
                box["value"] = "n" if kind == "confirm" else []
                break
        with self._lock:
            self._pending.pop(ask_id, None)
        return box["value"]

    def cancel_pending(self):
        """停止回合：所有待答复卡片立即按拒绝落定。"""
        with self._lock:
            pending = list(self._pending.values())
        for ev, box in pending:
            if box["value"] is None:
                box["value"] = "n"
            ev.set()

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

    def token_note(self, in_tok, out_tok, pct=None, cache_read: int = 0):
        self._emit({"t": "tokens", "in": in_tok, "out": out_tok, "pct": pct,
                    "cache_read": cache_read})

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
        self.cfg.workspace_lock = True   # Web 模式：智能体锁定在当前工作区
        self._mcp = mcp_manager
        self.bridge = WebBridgeUI()
        self.agent = _build_agent(cfg, provider, session or Session(),
                                  self.bridge, mcp_manager)
        self.bridge._stop_check = lambda: self.agent.interrupt_event.is_set()
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
                self._maybe_set_cookie()
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _forbidden(self):
                self._json(403, {"error": "forbidden host"})

            def _host_ok(self) -> bool:
                # 防 DNS rebinding：只接受本机 Host
                host = (self.headers.get("Host") or "").split(":")[0]
                return host in ("127.0.0.1", "localhost")

            def _cookie_token(self) -> str:
                for part in (self.headers.get("Cookie") or "").split(";"):
                    k, _, v = part.strip().partition("=")
                    if k == COOKIE_NAME:
                        return v
                return ""

            def _auth(self, query: dict) -> bool:
                """token 鉴权：Header / query / HttpOnly cookie 三选一。
                经 Header 或 query 通过时种下 cookie，浏览器后续请求不再
                需要携带明文 token（配合 SameSite=Strict 防 CSRF）。"""
                self._fresh_cookie = False
                for cand in (self.headers.get("X-Minicode-Token"),
                             (query.get("token") or [""])[0]):
                    if cand and hmac.compare_digest(str(cand), outer.token):
                        self._fresh_cookie = True
                        return True
                cookie = self._cookie_token()
                return bool(cookie and hmac.compare_digest(cookie, outer.token))

            def _maybe_set_cookie(self):
                if getattr(self, "_fresh_cookie", False):
                    self.send_header(
                        "Set-Cookie",
                        f"{COOKIE_NAME}={outer.token}; Path=/; Max-Age=604800; "
                        "HttpOnly; SameSite=Strict")
                    self._fresh_cookie = False

            def _unauthorized_page(self):
                """浏览器直接打开未带 token 的根路径：给出指引而不是泄露任何资源。"""
                body = ("<!doctype html><meta charset='utf-8'>"
                        "<title>minicode</title>"
                        "<body style='margin:0;height:100vh;display:grid;place-items:center;"
                        "background:#121110;color:#e9e5dd;font-family:monospace'>"
                        "<p>缺少访问令牌——请使用终端启动时打印的链接（含 ?token=…）</p>").encode("utf-8")
                self.send_response(401)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

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
                if path == "/api/health":
                    return self._json(200, {"ok": True, "mode": "web"})
                if not self._auth(query):
                    if path in ("/", "/index.html"):
                        return self._unauthorized_page()
                    return self._json(401, {"error": "unauthorized"})
                if path in _STATIC:
                    fname, ctype = _STATIC[path]
                    return self._static(fname, ctype)
                if path == "/api/events":
                    return self._sse()
                if path == "/api/status":
                    s = outer.agent.session
                    return self._json(200, {
                        "model": outer.cfg.model,
                        "provider": getattr(outer.agent.provider, "name",
                                            outer.cfg.provider),
                        "mode": outer.cfg.mode,
                        "cwd": str(outer.cfg.cwd or ""),
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
                    return self._json(200, {"sessions": outer._list_sessions(),
                                            "archived": outer._list_archived()})
                if path == "/api/workspaces":
                    ws = _load_workspaces().get("workspaces") or []
                    current = str(outer.cfg.cwd or "")
                    ordered = [current] + [w for w in ws if w != current]
                    return self._json(200, {"current": current, "list": ordered})
                if path == "/api/config":
                    cfg = outer.cfg
                    return self._json(200, {
                        "provider": cfg.provider,
                        "base_url": cfg.base_url,
                        "model": cfg.model,
                        "api_key_set": bool(cfg.api_key),
                        "api_key_tail": cfg.api_key[-4:] if cfg.api_key else "",
                        "max_tokens": cfg.max_tokens,
                        "context_limit": cfg.context_limit,
                        "reasoning_effort": cfg.reasoning_effort,
                        "timeout": cfg.timeout})
                if path == "/api/extensions":
                    return self._json(200, outer._extensions())
                if path == "/api/fs/list":
                    return self._json(200, outer._fs_list(query.get("path", [""])[0]))
                if path == "/api/ext/detail":
                    return self._json(*outer._ext_detail(
                        query.get("type", [""])[0], query.get("name", [""])[0]))
                return self._json(404, {"error": "not found"})

            def _static(self, fname: str, ctype: str):
                page = (WEB_DIR / fname).read_text(encoding="utf-8")
                # 安全：静态资源不再内嵌 token —— 浏览器凭 HttpOnly cookie 访问
                if "__VERSION__" in page:
                    page = page.replace("__VERSION__", outer._version())
                body = page.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self._maybe_set_cookie()
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _sse(self):
                q = outer.bridge.subscribe()
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self._maybe_set_cookie()
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
                if not self._auth(query):
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
                if path == "/api/stop":
                    if not outer.busy:
                        return self._json(409, {"error": "no turn is running"})
                    outer.agent.interrupt_event.set()
                    outer.bridge.cancel_pending()
                    outer._emit({"t": "info", "text": "正在停止当前回合…"})
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
                    if not _SESSION_NAME.fullmatch(name):
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
                if path in ("/api/session/delete", "/api/session/archive",
                            "/api/session/unarchive"):
                    return self._json(*outer._session_manage(path, body))
                if path == "/api/session/rename":
                    return self._json(*outer._session_rename(body))
                if path == "/api/workspace/add":
                    return self._json(*outer._workspace_add(body))
                if path == "/api/workspace/remove":
                    return self._json(*outer._workspace_remove(body))
                if path == "/api/workspace/switch":
                    return self._json(*outer._workspace_switch(body))
                if path == "/api/config":
                    return self._json(*outer._config_update(body))
                if path == "/api/ext/reload":
                    if outer.busy:
                        return self._json(409, {"error": "a turn is running"})
                    outer._rebuild(new_session=False)
                    outer._emit({"t": "info", "text": "扩展已重载（技能/插件/命令/子智能体）"})
                    return self._json(200, {"ok": True})
                if path == "/api/fs/pick":
                    return self._json(200, outer._native_pick(body))
                if path == "/api/ext/import":
                    return self._json(*outer._ext_import(body))
                if path.startswith("/api/ext/"):
                    return self._json(*outer._ext_manage(path, body))
                if path == "/api/probe":
                    if outer.busy:
                        return self._json(409, {"error": "a turn is running"})
                    from .llm import probe_provider
                    rows = probe_provider(outer.agent.provider)
                    return self._json(200, {"rows": [[n, ok, d, round(sec, 1)]
                                                     for n, ok, d, sec in rows]})
                if path == "/api/command":
                    line = str(body.get("line") or "").strip()
                    if not line:
                        return self._json(400, {"error": "command is required"})
                    try:
                        return self._json(200, outer._dispatch_command(line))
                    except LLMError as e:
                        return self._json(400, {"error": str(e)})
                    except Exception as e:
                        return self._json(500, {"error": f"{type(e).__name__}: {e}"})
                if path == "/api/shell":
                    command = str(body.get("command") or "").strip()
                    if not command:
                        return self._json(400, {"error": "command is required"})
                    bash = outer.agent.registry.get("bash")
                    if bash is None:
                        return self._json(400, {"error": "bash tool unavailable"})
                    out = bash.run({"command": command}, outer.agent.ctx)
                    outer._emit({"t": "plain", "text": f"$ {command}"})
                    outer._emit({"t": "plain", "text": out})
                    return self._json(200, {"ok": True})
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
        return WebUIServer._list_session_dir(session_mod.SESSIONS_DIR, limit)

    @staticmethod
    def _list_archived(limit: int = 50) -> list:
        return WebUIServer._list_session_dir(session_mod.SESSIONS_DIR / "archived",
                                             limit)

    @staticmethod
    def _list_session_dir(base: Path, limit: int) -> list:
        out = []
        if not base.is_dir():
            return out
        files = sorted(base.glob("*.json"),
                       key=lambda p: p.stat().st_mtime if p.exists() else 0,
                       reverse=True)
        for p in files[:limit]:
            stamp, _, slug = p.stem.partition("_")
            title = slug if slug and slug != "session" else stamp
            try:
                ts = int(p.stat().st_mtime)
                when = time.strftime("%m-%d %H:%M", time.localtime(ts))
            except OSError:
                ts, when = 0, ""
            out.append({"name": p.stem, "title": title[:60], "time": when,
                        "ts": ts})
        return out

    def _session_manage(self, path: str, body: dict):
        name = str(body.get("name") or "")
        if not _SESSION_NAME.fullmatch(name):
            return 400, {"error": "invalid session name"}
        archived_dir = session_mod.SESSIONS_DIR / "archived"
        base = archived_dir if path == "/api/session/unarchive" or \
            body.get("archived") else session_mod.SESSIONS_DIR
        f = base / f"{name}.json"
        if not f.exists():
            return 404, {"error": "session not found"}
        try:
            if path == "/api/session/delete":
                f.unlink()
                return 200, {"ok": True, "deleted": name}
            if path == "/api/session/archive":
                archived_dir.mkdir(parents=True, exist_ok=True)
                f.rename(archived_dir / f.name)
                return 200, {"ok": True, "archived": name}
            if path == "/api/session/unarchive":
                f.rename(session_mod.SESSIONS_DIR / f.name)
                return 200, {"ok": True, "unarchived": name}
        except OSError as e:
            return 500, {"error": str(e)}
        return 404, {"error": "not found"}

    def _session_rename(self, body: dict):
        name = str(body.get("name") or "")
        title = str(body.get("title") or "").strip()
        archived = bool(body.get("archived"))
        if not _SESSION_NAME.fullmatch(name) or not title:
            return 400, {"error": "name and title are required"}
        base = (session_mod.SESSIONS_DIR / "archived") if archived \
            else session_mod.SESSIONS_DIR
        f = base / f"{name}.json"
        if not f.exists():
            return 404, {"error": "session not found"}
        stamp = name.partition("_")[0]
        slug = re.sub(r"[^\w\-]+", "-", title).strip("-") or "session"
        target = base / f"{stamp}_{slug}.json"
        if target.exists():
            return 409, {"error": "target name already exists"}
        f.rename(target)
        return 200, {"ok": True, "name": target.stem}

    def _workspace_add(self, body: dict):
        raw = str(body.get("path") or "").strip()
        p = Path(raw).expanduser()
        if not p.is_absolute():
            return 400, {"error": "需要绝对路径"}
        p = p.resolve()
        if not p.is_dir():
            return 400, {"error": f"目录不存在：{p}"}
        data = _load_workspaces()
        ws = data.setdefault("workspaces", [])
        if str(p) not in ws:
            ws.append(str(p))
            _save_workspaces(data)
        return 200, {"ok": True, "list": ws}

    def _workspace_remove(self, body: dict):
        raw = str(body.get("path") or "").strip()
        p = str(Path(raw).expanduser().resolve())
        if p == str(self.cfg.cwd or ""):
            return 400, {"error": "不能移除当前工作区"}
        data = _load_workspaces()
        ws = [w for w in (data.get("workspaces") or []) if w != p]
        data["workspaces"] = ws
        _save_workspaces(data)
        return 200, {"ok": True, "list": ws}

    def _workspace_switch(self, body: dict):
        raw = str(body.get("path") or "").strip()
        p = Path(raw).expanduser().resolve()
        if not p.is_dir():
            return 400, {"error": f"目录不存在：{p}"}
        with self._lock:
            if self.busy:
                return 409, {"error": "a turn is running"}
        data = _load_workspaces()
        ws = data.setdefault("workspaces", [])
        if str(p) not in ws:
            ws.append(str(p))
        data["last"] = str(p)
        _save_workspaces(data)
        self._rebuild(cwd=p, new_session=True)
        self._emit({"t": "workspace", "path": str(p)})
        return 200, {"ok": True, "cwd": str(p)}

    def _config_update(self, body: dict):
        from .config import USER_CONFIG
        cfg = self.cfg
        with self._lock:
            if self.busy:
                return 409, {"error": "a turn is running"}
        if body.get("provider") in ("openai", "anthropic"):
            cfg.provider = body["provider"]
        if body.get("base_url") is not None:
            cfg.base_url = str(body["base_url"]).strip()
        if body.get("api_key"):
            cfg.api_key = str(body["api_key"]).strip()
        if body.get("model"):
            cfg.model = str(body["model"]).strip()
        if body.get("max_tokens"):
            cfg.max_tokens = int(body["max_tokens"])
        if body.get("context_limit"):
            cfg.context_limit = int(body["context_limit"])
        if body.get("reasoning_effort") in ("", "low", "medium", "high"):
            cfg.reasoning_effort = body["reasoning_effort"]
        if body.get("save"):
            try:
                data = {}
                if USER_CONFIG.exists():
                    data = json.loads(USER_CONFIG.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    data = {}
                data.update({"provider": cfg.provider, "base_url": cfg.base_url,
                             "model": cfg.model,
                             "max_tokens": cfg.max_tokens,
                             "context_limit": cfg.context_limit,
                             "reasoning_effort": cfg.reasoning_effort})
                if body.get("api_key"):
                    data["api_key"] = cfg.api_key
                USER_CONFIG.parent.mkdir(parents=True, exist_ok=True)
                USER_CONFIG.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8")
            except OSError as e:
                return 500, {"error": f"保存配置失败：{e}"}
        self._rebuild(new_session=False)   # 保留当前会话，重建 provider/agent
        self._emit({"t": "info",
                    "text": f"模型配置已更新：{cfg.provider} · {cfg.model}"
                            + ("（已保存到 ~/.minicode.json）" if body.get("save") else "")})
        return 200, {"ok": True, "model": cfg.model}

    def _rebuild(self, cwd: Optional[Path] = None, new_session: bool = False) -> None:
        """切换工作区 / 更新模型配置后重建 agent（provider、系统提示、
        brain、检查点随之刷新）。new_session=False 时保留当前会话与检查点。"""
        from .cli import _build_agent
        from .llm import make_provider
        old = self.agent
        if self._mcp is not None:
            self._mcp.stop_all()
        if cwd is not None:
            self.cfg.cwd = Path(cwd)
        provider = make_provider(self.cfg)
        session = Session() if new_session else old.session
        self.agent = _build_agent(self.cfg, provider, session,
                                  self.bridge, self._mcp)
        if not new_session:
            self.agent.checkpoints = old.checkpoints   # 保留 /undo 历史

    def _dispatch_command(self, line: str) -> dict:
        """终端斜杠命令的 Web 分发器。返回 {output} / {list} / {turn}。"""
        from .cli import (_custom_commands, _parse_size, COMMIT_PROMPT,
                          INIT_PROMPT, PR_PROMPT)
        from .llm import list_models
        from .prompts import REVIEW_PROMPT, build_system_prompt
        from .tools.memory import brain_path, load_brain_text
        agent, cfg = self.agent, self.cfg
        s = agent.session
        name, _, arg = line.lstrip("/").partition(" ")
        name, arg = name.lower().strip(), arg.strip()

        aliases = {"yolo": "full-access", "plan": "plan", "edits": "accept-edits"}
        if name in aliases:
            arg = aliases[name]
            name = "mode"
        if name == "mode":
            if arg in MODES:
                cfg.mode = arg
            return {"output": f"权限模式：{cfg.mode}"}
        if name == "undo":
            e = agent.checkpoints.undo() if agent.checkpoints else None
            return {"output": f"已撤销 {e['tool']} 对 {e['path']} 的修改。" if e
                    else "没有可撤销的更改。"}
        if name == "rewind":
            shown = agent.checkpoints.list(10) if agent.checkpoints else []
            if arg.isdigit():
                if not (1 <= int(arg) <= len(shown)):
                    return {"output": f"序号超出范围（1-{len(shown)}）"}
                undone = agent.checkpoints.rewind_to(int(arg) - 1, shown)
                return {"output": f"已回退 {len(undone)} 步文件修改。"}
            return {"list": [f"{i}. {e['time']} {e['tool']} {e['path']}"
                             for i, e in enumerate(shown, 1)],
                    "hint": "点击条目回退，或用 /rewind <序号>"}
        if name == "diff":
            diffs = agent.checkpoints.session_diff() if agent.checkpoints else []
            if not diffs:
                return {"output": "本次会话没有未还原的文件改动。"}
            out = []
            for path_, diff in diffs:
                out.append(path_ + ":")
                out.extend("  " + ln for ln in diff.splitlines()[:60])
            return {"output": "\n".join(out)}
        if name == "limit":
            if not arg:
                return {"output": f"上下文长度：{cfg.context_limit:,} tok"}
            n = _parse_size(arg)
            if not n or n < 1000:
                return {"output": "无法解析——示例：/limit 1M、/limit 500k"}
            cfg.context_limit = n
            return {"output": f"上下文长度已设置为 {n:,} tok"}
        if name == "reasoning":
            order = ["", "low", "medium", "high"]
            if arg in order:
                cfg.reasoning_effort = arg
            else:
                cur = order.index(cfg.reasoning_effort or "")
                cfg.reasoning_effort = order[(cur + 1) % len(order)]
            if hasattr(agent.provider, "reasoning_effort"):
                agent.provider.reasoning_effort = cfg.reasoning_effort
            return {"output": f"reasoning effort：{cfg.reasoning_effort or '默认'}"}
        if name == "cost":
            t = s.total_usage
            cached = t.get("cache_read") or 0
            extra = f" · 缓存命中 {cached:,}" if cached else ""
            return {"output": f"累计 tokens：in {t['input']:,} · out {t['output']:,}{extra}"}
        if name == "context":
            by = {}
            for m in s.messages:
                k = m.get("role") or "?"
                by[k] = by.get(k, 0) + s._content_chars(m.get("content"))
                for tc in m.get("tool_calls") or []:
                    by[k] += len(tc.get("args") or "")
            lines = [f"{k:<10} ~{v // 3:,} tok" for k, v in by.items()]
            lines.append(f"合计 ~{s.context_tokens():,} tok "
                         f"(上限 {cfg.context_limit:,})")
            return {"output": "\n".join(lines) or "(空)"}
        if name == "tools":
            return {"output": "\n".join(
                f"{t.name:<22} [{t.kind}] {t.description.splitlines()[0][:60]}"
                for t in agent.registry.tools.values())}
        if name == "todos":
            if not s.todos:
                return {"output": "当前没有任务清单。"}
            return {"output": "\n".join(f"[{t.get('status')}] {t.get('content')}"
                                        for t in s.todos)}
        if name == "brain":
            if arg == "clear":
                p = brain_path(cfg.cwd)
                if p.exists():
                    p.unlink()
                agent.system_prompt = build_system_prompt(
                    cfg, agent._prompt_cwd, agent._custom_agents)
                return {"output": "项目大脑已清空。"}
            text = load_brain_text(cfg.cwd).strip()
            return {"output": text[:3000] or "大脑还是空的。"}
        if name == "memory":
            p = cfg.cwd / "MINICODE.md"
            if not p.exists():
                return {"output": "MINICODE.md 不存在（/init 可生成）。"}
            return {"output": p.read_text(encoding="utf-8",
                                          errors="replace")[:4000]}
        if name == "export":
            out = cfg.cwd / f"minicode-chat-{time.strftime('%Y%m%d-%H%M%S')}.md"
            s.export_markdown(out)
            return {"output": f"已导出到 {out}"}
        if name == "transcript":
            rows = []
            for m in s.messages[-16:]:
                body = m.get("content")
                text = body if isinstance(body, str) else "(多部分内容)"
                rows.append(f"[{m.get('role')}] {' '.join((text or '').split())[:120]}")
            return {"output": "\n".join(rows) or "会话为空。"}
        if name == "plans":
            from . import plans as _plans
            rows = _plans.list_plans(cfg.cwd)
            if not rows:
                return {"output": "还没有存档计划。"}
            return {"output": "\n".join(f"[{i}] ({st}) {t}"
                                        for i, p, st, t in rows)}
        if name == "agents":
            rows = ["dispatch_agent（内置只读调研）"]
            rows += [f"{n}：{info.get('description', '')}"
                     for n, info in (agent._custom_agents or {}).items()]
            return {"output": "\n".join(rows)}
        if name == "skills":
            from .tools.skills import skills_catalog
            catalog = skills_catalog(cfg.cwd)
            return {"output": "\n".join(f"{n}：{desc}" for n, (desc, _) in
                                        catalog.items()) or "没有可用技能。"}
        if name == "mcp":
            if agent.mcp is None:
                return {"output": "未配置 MCP。"}
            return {"output": "\n".join(agent.mcp.status_lines())}
        if name == "model":
            if arg:
                cfg.model = arg
                agent.provider.model = arg
                return {"output": f"模型已切换为 {arg}"}
            return {"output": f"当前模型：{cfg.model}（provider: {cfg.provider}）"}
        if name == "models":
            ids = list_models(agent.provider)
            return {"output": f"{len(ids)} 个模型：\n" + "\n".join(ids[:30])}
        if name == "add-dir":
            p = Path(arg).expanduser().resolve()
            if not p.is_dir():
                return {"output": f"目录不存在：{p}"}
            cfg.extra_dirs.append(str(p))
            agent.system_prompt = build_system_prompt(
                cfg, agent._prompt_cwd, agent._custom_agents)
            return {"output": f"已授权访问目录：{p}"}
        if name == "verify":
            if not arg:
                return {"output": f"自检命令：{cfg.verify_command or '未设置'}"
                        "（/verify pytest -q 开启；/verify off 关闭）"}
            if arg.lower() == "off":
                cfg.verify_command = ""
                return {"output": "自检已关闭。"}
            cfg.verify_command = arg
            return {"output": f"自检命令已设置：{arg} —— 每次文件改动后自动执行"}
        if name == "output-style":
            style = arg if arg in ("default", "explanatory") else (
                "explanatory" if cfg.output_style == "default" else "default")
            cfg.output_style = style
            agent.system_prompt = build_system_prompt(
                cfg, agent._prompt_cwd, agent._custom_agents)
            return {"output": f"输出风格：{style}"}
        if name == "doctor":
            import shutil as _shutil
            from .lineinput import HAS_READLINE
            rows = [f"python      {sys.version_info.major}.{sys.version_info.minor}"
                    f".{sys.version_info.micro}"]
            shell_ok = bool(_shutil.which(cfg.shell_name)) if cfg.shell_name != "cmd" \
                else bool(_shutil.which("cmd"))
            rows.append(f"shell       {cfg.shell_name} {'✓' if shell_ok else '✗'}")
            rows.append(f"api_key     {'已配置' if cfg.api_key else '未配置'}")
            rows.append(f"model       {cfg.model} @ {cfg.base_url or '（默认）'}")
            ctx_file = next((n for n in ("MINICODE.md", "AGENTS.md", "CLAUDE.md")
                             if (cfg.cwd / n).exists()), None)
            rows.append(f"项目记忆    {ctx_file or '未找到（/init 可生成）'}")
            rows.append(f"tab 补全    {'可用' if HAS_READLINE else '不可用'}")
            if agent.mcp is not None:
                for n, c in agent.mcp.clients.items():
                    rows.append(f"mcp:{n}     {c.status}"
                                + (f" ({len(c.tools)} tools)" if c.status == "connected" else ""))
            return {"output": "\n".join(rows)}
        if name == "stats":
            import json as _json
            from .session import SESSIONS_DIR
            files = list(SESSIONS_DIR.glob("*.json"))
            total_msgs = total_tools = errors = 0
            tool_counts = {}
            for f in files:
                try:
                    d = _json.loads(f.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                for m in d.get("messages", []):
                    total_msgs += 1
                    if m.get("role") == "tool":
                        total_tools += 1
                        n = m.get("name") or "?"
                        tool_counts[n] = tool_counts.get(n, 0) + 1
                        if m.get("is_error"):
                            errors += 1
            rate = (errors / total_tools * 100) if total_tools else 0
            top = sorted(tool_counts.items(), key=lambda kv: -kv[1])[:5]
            lines = [f"会话 {len(files)} · 消息 {total_msgs} · 工具调用 {total_tools}"
                     f" · 工具错误 {errors}（错误率 {rate:.1f}%）"]
            if top:
                lines.append("最常用工具：" + "、".join(f"{n}×{c}" for n, c in top))
            return {"output": "\n".join(lines)}
        if name == "help":
            return {"output": "\n".join(
                "/mode /undo /rewind /diff /limit /reasoning /cost /context "
                "/tools /todos /brain /memory /export /transcript /plans "
                "/agents /skills /mcp /model /models /add-dir /verify "
                "/init /commit /pr /review /help —— /init /commit /pr /review "
                "会发起一个回合；!cmd 直通本地执行")}
        if name in ("init", "commit", "pr", "review"):
            prompts = {"init": INIT_PROMPT,
                       "commit": COMMIT_PROMPT.format(files_hint="", extra=""),
                       "pr": PR_PROMPT.format(base=arg or "main"),
                       "review": REVIEW_PROMPT.format(target="本会话改动", extra="")}
            return {"turn": prompts[name]}
        custom = _custom_commands().get(name)
        if custom:
            return {"turn": custom[1].replace("$ARGUMENTS", arg)
                    .replace("{args}", arg)}
        return {"error": f"未知命令 /{name}（/help 查看可用命令）"}

    _pick_lock = threading.Lock()

    def _native_pick(self, body: dict) -> dict:
        """弹出系统原生选择器（任意盘符/任意位置）。kind: folder|file。"""
        kind = body.get("kind") or "folder"
        title = str(body.get("title") or "选择位置")
        initial = str(body.get("initial") or "") or None
        multi = bool(body.get("multi"))
        try:
            import tkinter as tk
            from tkinter import filedialog
        except ImportError:
            return {"error": "本机 Python 缺少 tkinter，无法弹出系统对话框"}

        with self._pick_lock:   # 串行化：同一时刻只弹一个系统对话框
            result = {"path": None, "paths": None}
            done = threading.Event()

            def worker():
                root = None
                try:
                    root = tk.Tk()
                    root.withdraw()
                    root.attributes("-topmost", True)   # 置顶，不被浏览器遮挡
                    if kind == "folder":
                        p = filedialog.askdirectory(title=title,
                                                    initialdir=initial)
                        result["path"] = p or None
                    else:
                        exts = body.get("ext") or []
                        filetypes = ([("相关文件", "*" + " *".join(exts))]
                                     if exts else [("所有文件", "*.*")])
                        if multi:
                            ps = filedialog.askopenfilenames(
                                title=title, initialdir=initial,
                                filetypes=filetypes)
                            result["paths"] = list(ps) or None
                        else:
                            p = filedialog.askopenfilename(
                                title=title, initialdir=initial,
                                filetypes=filetypes)
                            result["path"] = p or None
                except Exception as e:
                    result["error"] = f"{type(e).__name__}: {e}"
                finally:
                    try:
                        if root is not None:
                            root.destroy()
                    except Exception:
                        pass
                    done.set()

            threading.Thread(target=worker, daemon=True).start()
            if not done.wait(timeout=1800):   # 用户可能挑选很久
                return {"error": "对话框超时未响应"}
            return result

    def _fs_list(self, raw: str) -> dict:
        """服务端目录浏览器：列出某路径下的子目录（供工作区选择弹窗）。"""
        import os
        raw = (raw or "").strip()
        if os.name == "nt" and not raw:
            drives = []
            for d in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
                dp = Path(f"{d}:\\")
                if dp.exists():
                    drives.append(f"{d}:\\")
            return {"path": "", "parent": None, "dirs": drives, "isDrives": True}
        p = Path(raw).expanduser()
        if not p.is_absolute():
            return {"error": "需要绝对路径"}
        try:
            p = p.resolve()
            if not p.is_dir():
                return {"error": f"目录不存在：{p}"}
            dirs = []
            for e in p.iterdir():
                if e.is_dir() and not e.name.startswith("."):
                    dirs.append(e.name)
            dirs.sort(key=str.lower)
            parent = None
            if p.parent != p:
                parent = str(p.parent)
            return {"path": str(p), "parent": parent, "dirs": dirs[:500],
                    "isDrives": False}
        except OSError as e:
            return {"error": str(e)}

    @staticmethod
    def _slug(raw: str) -> str:
        return re.sub(r"[^\w\-]+", "-", str(raw or "")).strip("-")[:64]

    def _ext_manage(self, path: str, body: dict):
        """扩展自由增删：/api/ext/<type>/<create|delete>。写入限于
        工作区 .minicode/** 与 ~/.minicode/**（内置资源只读）。"""
        kind = path[len("/api/ext/"):].split("/")[0]
        action = path[len("/api/ext/"):].split("/")[1]
        name = self._slug(body.get("name"))
        if not name:
            return 400, {"error": "name is required"}
        project_ext = self.cfg.cwd / ".minicode"

        def _rm(p: Path) -> tuple:
            try:
                if p.is_dir():
                    shutil.rmtree(p)
                else:
                    p.unlink()
                return 200, {"ok": True, "deleted": name}
            except OSError as e:
                return 500, {"error": str(e)}

        try:
            if kind == "skill":
                base = project_ext / "skills"
                if action == "create":
                    d = base / name
                    if d.exists():
                        return 409, {"error": f"技能 {name} 已存在"}
                    d.mkdir(parents=True)
                    desc = str(body.get("desc") or "").strip()
                    content = str(body.get("content") or "")
                    (d / "SKILL.md").write_text(
                        f"---\nname: {name}\ndescription: {desc}\n---\n\n{content}\n",
                        encoding="utf-8")
                    return 200, {"ok": True, "name": name}
                if action == "delete":
                    source = body.get("source")
                    if source == "内置":
                        return 400, {"error": "内置技能不可删除"}
                    base = (Path.home() / ".minicode" / "skills") \
                        if source == "用户" else base
                    d = (base / name).resolve()
                    if base.resolve() not in d.parents or not d.exists():
                        return 404, {"error": "skill not found"}
                    return _rm(d)
            if kind == "command":
                base = project_ext / "commands"
                f = base / f"{name}.md"
                if action == "create":
                    if f.exists():
                        return 409, {"error": f"命令 {name} 已存在"}
                    base.mkdir(parents=True, exist_ok=True)
                    desc = str(body.get("desc") or "").strip()
                    content = str(body.get("content") or "")
                    f.write_text(
                        f"---\ndescription: {desc}\n---\n\n{content}\n",
                        encoding="utf-8")
                    return 200, {"ok": True, "name": name}
                if action == "delete":
                    if not f.exists():
                        return 404, {"error": "command not found"}
                    return _rm(f)
            if kind == "agent":
                base = project_ext / "agents"
                f = base / f"{name}.md"
                if action == "create":
                    if f.exists():
                        return 409, {"error": f"子智能体 {name} 已存在"}
                    base.mkdir(parents=True, exist_ok=True)
                    desc = str(body.get("desc") or "").strip()
                    tools = str(body.get("tools") or "").strip()
                    model = str(body.get("model") or "").strip()
                    prompt = str(body.get("prompt") or "").strip()
                    head = f"---\ndescription: {desc}\n"
                    if tools:
                        head += f"tools: {tools}\n"
                    if model:
                        head += f"model: {model}\n"
                    head += "---\n"
                    f.write_text(head + "\n" + prompt + "\n", encoding="utf-8")
                    return 200, {"ok": True, "name": name}
                if action == "delete":
                    if not f.exists():
                        return 404, {"error": "agent not found"}
                    return _rm(f)
            if kind == "plugin":
                base = project_ext / "tools"
                f = base / f"{name}.py"
                if action == "create":
                    if f.exists():
                        return 409, {"error": f"插件 {name} 已存在"}
                    base.mkdir(parents=True, exist_ok=True)
                    desc = str(body.get("desc") or f"{name} 插件")
                    f.write_text(
                        f'TOOL = {{\n    "name": "{name}",\n'
                        f'    "description": "{desc}",\n'
                        f'    "kind": "meta",\n'
                        f'    "input_schema": {{"type": "object", "properties": {{}}}},\n'
                        f'}}\n\n\n'
                        f'def run(args: dict, ctx) -> str:\n'
                        f'    return "hello from {name}"\n',
                        encoding="utf-8")
                    return 200, {"ok": True, "name": name,
                                 "note": "重载扩展后生效"}
                if action == "delete":
                    if not f.exists():
                        return 404, {"error": "plugin not found"}
                    return _rm(f)
        except OSError as e:
            return 500, {"error": str(e)}
        return 404, {"error": f"unknown extension op: {path}"}

    def _ext_import(self, body: dict):
        """直接导入本机文件为扩展：技能(文件夹或 .md) / 插件(.py) /
        命令与子智能体(.md)。导入后自动重载扩展。"""
        kind = body.get("kind")
        paths = body.get("paths") or ([body.get("path")] if body.get("path") else [])
        overwrite = bool(body.get("overwrite"))
        if kind not in ("skill", "plugin", "command", "agent") or not paths:
            return 400, {"error": "kind 和 paths 是必填项"}
        if self.busy:
            return 409, {"error": "a turn is running"}
        project_ext = self.cfg.cwd / ".minicode"
        imported, skipped, failed = [], [], []

        for raw in paths:
            src = Path(raw).expanduser()
            if not src.exists():
                failed.append({"path": str(src), "error": "文件/目录不存在"})
                continue
            if kind == "skill":
                base = project_ext / "skills"
                if src.is_dir():
                    name = self._slug(src.name)
                    dest = base / name
                    if not (src / "SKILL.md").exists():
                        failed.append({"path": str(src),
                                       "error": "技能文件夹必须包含 SKILL.md"})
                        continue
                elif src.suffix.lower() == ".md":
                    name = self._slug(src.stem)
                    dest = base / name
                else:
                    failed.append({"path": str(src),
                                   "error": "技能需要文件夹（含 SKILL.md）或 .md 文件"})
                    continue
                if dest.exists() and not overwrite:
                    skipped.append({"name": name, "error": "同名技能已存在"})
                    continue
                try:
                    if dest.exists():
                        shutil.rmtree(dest)
                    base.mkdir(parents=True, exist_ok=True)
                    if src.is_dir():
                        shutil.copytree(src, dest)
                    else:
                        dest.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src, dest / "SKILL.md")
                    imported.append(name)
                except OSError as e:
                    failed.append({"path": str(src), "error": str(e)})
            elif kind == "plugin":
                if src.suffix.lower() != ".py" or not src.is_file():
                    failed.append({"path": str(src), "error": "插件必须是 .py 文件"})
                    continue
                base = project_ext / "tools"
                base.mkdir(parents=True, exist_ok=True)
                dest = base / src.name
                if dest.exists() and not overwrite:
                    skipped.append({"name": src.stem, "error": "同名插件已存在"})
                    continue
                try:
                    shutil.copy2(src, dest)
                    imported.append(src.stem)
                except OSError as e:
                    failed.append({"path": str(src), "error": str(e)})
            elif kind in ("command", "agent"):
                if src.suffix.lower() != ".md" or not src.is_file():
                    failed.append({"path": str(src), "error": "必须是 .md 文件"})
                    continue
                base = project_ext / ("commands" if kind == "command" else "agents")
                base.mkdir(parents=True, exist_ok=True)
                dest = base / f"{self._slug(src.stem)}.md"
                if dest.exists() and not overwrite:
                    skipped.append({"name": src.stem, "error": "同名已存在"})
                    continue
                try:
                    shutil.copy2(src, dest)
                    imported.append(self._slug(src.stem))
                except OSError as e:
                    failed.append({"path": str(src), "error": str(e)})

        if imported:
            self._rebuild(new_session=False)   # 让新扩展立即可用
        return 200, {"ok": True, "imported": imported,
                     "skipped": skipped, "failed": failed}

    def _ext_detail(self, kind: str, name: str):
        """扩展详情：技能/命令/子智能体的全文，插件的源码，MCP 的工具清单。"""
        kind = (kind or "").strip()
        name = (name or "").strip()
        if not name or len(name) > 120 or "/" in name or "\\" in name:
            return 400, {"error": "invalid name"}

        def _frontmatter(text: str) -> tuple:
            """解析 --- frontmatter 中的简单键值对，返回 (meta, body)。"""
            meta, body = {}, text
            if text.startswith("---"):
                parts = text.split("---", 2)
                if len(parts) == 3:
                    for ln in parts[1].splitlines():
                        if ":" in ln:
                            k, _, v = ln.partition(":")
                            meta[k.strip().lower()] = v.strip()
                    body = parts[2]
            return meta, body

        if kind == "skills":
            from .tools.skills import _parse, _skill_dirs
            for label, d in zip(["内置", "用户", "项目"], _skill_dirs(self.cfg.cwd)):
                f = d / name / "SKILL.md"
                if not f.exists():
                    continue
                n, desc, body = _parse(f)
                return 200, {"kind": kind, "name": n, "desc": desc,
                             "source": label, "path": str(f), "content": body}
            return 404, {"error": f"技能 {name} 不存在"}
        if kind == "plugins":
            for base in (self.cfg.cwd / ".minicode" / "tools",
                         Path.home() / ".minicode" / "tools"):
                f = base / f"{name}.py"
                if f.exists():
                    return 200, {"kind": kind, "name": name,
                                 "desc": "本地 Python 插件工具",
                                 "path": str(f),
                                 "content": f.read_text(encoding="utf-8",
                                                        errors="replace")}
            return 404, {"error": f"插件 {name} 不存在"}
        if kind == "agents":
            base = self.cfg.cwd / ".minicode" / "agents"
            f = base / f"{name}.md"
            if not f.exists():
                return 404, {"error": f"子智能体 {name} 不存在"}
            meta, body = _frontmatter(f.read_text(encoding="utf-8",
                                                  errors="replace"))
            return 200, {"kind": kind, "name": name,
                         "desc": meta.get("description", ""),
                         "tools": meta.get("tools", ""),
                         "model": meta.get("model", ""),
                         "path": str(f), "content": body.strip()}
        if kind == "commands":
            base = self.cfg.cwd / ".minicode" / "commands"
            f = base / f"{name}.md"
            if not f.exists():
                return 404, {"error": f"命令 {name} 不存在"}
            meta, body = _frontmatter(f.read_text(encoding="utf-8",
                                                  errors="replace"))
            return 200, {"kind": kind, "name": name,
                         "desc": meta.get("description", ""),
                         "path": str(f), "content": body.strip()}
        if kind == "mcp":
            client = (self.agent.mcp.clients.get(name)
                      if (self.agent.mcp is not None) else None)
            if client is None:
                return 404, {"error": f"MCP 服务器 {name} 不存在"}
            tools = [{"name": t.get("name", ""),
                      "desc": (t.get("description") or "")[:300],
                      "schema": json.dumps(t.get("inputSchema") or {},
                                           ensure_ascii=False)[:1200]}
                     for t in client.tools if isinstance(t, dict)]
            return 200, {"kind": "mcp", "name": name, "status": client.status,
                         "error": client.error or "", "tools": tools}
        return 400, {"error": f"unknown type: {kind}"}

    def _extensions(self) -> dict:
        """扩展体系全目录：技能 / 插件 / 自定义子智能体 / 自定义命令 / MCP。"""
        from .cli import BUILTIN_COMMANDS, _custom_commands
        from .plugins import PluginTool
        from .tools.skills import skills_catalog
        plugins = [t.name for t in self.agent.registry.tools.values()
                   if isinstance(t, PluginTool)]
        mcp_tools = []
        if self.agent.mcp is not None:
            for name, c in self.agent.mcp.clients.items():
                mcp_tools.append({"server": name, "status": c.status,
                                  "tools": len(c.tools),
                                  "error": c.error or ""})
        return {
            "skills": [{"name": n, "desc": d, "source": src}
                       for n, (d, src) in skills_catalog(self.cfg.cwd).items()],
            "plugins": plugins,
            "agents": [{"name": n, "desc": info.get("description", ""),
                        "tools": info.get("tools", ""),
                        "model": info.get("model", "")}
                       for n, info in (self.agent._custom_agents or {}).items()],
            "commands": [{"name": n, "desc": d}
                         for n, (d, _) in _custom_commands().items()
                         if n not in BUILTIN_COMMANDS],
            "mcp": mcp_tools,
        }

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
