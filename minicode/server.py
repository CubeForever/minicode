"""minicode serve — expose the agent over a local HTTP API (stdlib only).

Endpoints (all require X-Minicode-Token header, printed at startup):
    GET  /api/health   -> {"ok": true}
    GET  /api/status   -> model / mode / message count / busy
    POST /api/turn     -> {"prompt": "...", "yolo": bool?}  runs one agent turn
    POST /api/clear    -> start a fresh session
    POST /api/compact  -> compact the context

Binds 127.0.0.1 by default. One turn at a time; concurrent requests get 409.
"""
from __future__ import annotations

import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional


class MinicodeServer:
    def __init__(self, agent, host: str = "127.0.0.1", port: int = 8765):
        self.agent = agent
        self.host = host
        self.port = port
        self.token = secrets.token_hex(16)
        self.busy = False
        self._lock = threading.Lock()
        self._httpd: Optional[ThreadingHTTPServer] = None
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silence default stderr logging
                pass

            def _json(self, code: int, payload: dict):
                body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _auth(self) -> bool:
                return self.headers.get("X-Minicode-Token") == outer.token

            def do_GET(self):
                if self.path == "/api/health":
                    return self._json(200, {"ok": True, "version": outer.agent_version()})
                if not self._auth():
                    return self._json(401, {"error": "unauthorized"})
                if self.path == "/api/status":
                    s = outer.agent.session
                    return self._json(200, {
                        "model": outer.agent.config.model,
                        "provider": outer.agent.config.provider,
                        "mode": outer.agent.config.mode,
                        "messages": len(s.messages),
                        "context_tokens": s.context_tokens(),
                        "busy": outer.busy})
                return self._json(404, {"error": "not found"})

            def do_POST(self):
                if not self._auth():
                    return self._json(401, {"error": "unauthorized"})
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(length).decode("utf-8")) \
                        if length else {}
                except (ValueError, json.JSONDecodeError):
                    return self._json(400, {"error": "invalid JSON body"})
                if self.path == "/api/turn":
                    prompt = str(body.get("prompt") or "").strip()
                    if not prompt:
                        return self._json(400, {"error": "prompt is required"})
                    if not outer._acquire():
                        return self._json(409, {"error": "a turn is already running"})
                    try:
                        result = outer._run_turn(prompt, bool(body.get("yolo")))
                        return self._json(200, result)
                    except Exception as e:  # never leave the client with no response
                        return self._json(500, {"error": f"{type(e).__name__}: {e}"})
                    finally:
                        outer._release()
                if self.path == "/api/clear":
                    agent.reset_session()
                    return self._json(200, {"ok": True, "messages": 0})
                if self.path == "/api/compact":
                    stats = agent.compact()
                    return self._json(200, {"ok": True, **stats})
                return self._json(404, {"error": "not found"})

        self._httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self.port = self._httpd.server_address[1]

    # ---------- helpers ----------

    def agent_version(self) -> str:
        from . import __version__
        return __version__

    def _acquire(self) -> bool:
        with self._lock:
            if self.busy:
                return False
            self.busy = True
            return True

    def _release(self):
        with self._lock:
            self.busy = False

    def _run_turn(self, prompt: str, yolo: bool) -> dict:
        agent = self.agent
        prev_mode = agent.config.mode
        if yolo:
            agent.config.mode = "full-access"
        try:
            agent.run_turn(prompt)
            last = next((m for m in reversed(agent.session.messages)
                         if m.get("role") == "assistant" and m.get("content")), {})
            return {"result": last.get("content") or "",
                    "messages": len(agent.session.messages),
                    "usage": agent.session.total_usage}
        finally:
            agent.config.mode = prev_mode

    def serve_forever(self):
        self._httpd.serve_forever()

    def shutdown(self):
        if self._httpd is not None:
            threading.Thread(target=self._httpd.shutdown, daemon=True).start()
