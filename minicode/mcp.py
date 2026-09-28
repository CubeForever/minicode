"""MCP (Model Context Protocol) stdio client.

Connects to servers configured under ``mcpServers`` (same shape as Claude Code),
discovers their tools and exposes them as ``mcp__<server>__<tool>``.
JSON-RPC 2.0 over the server's stdin/stdout, one request at a time.

v0.16：协议版本 2025-06-18（被拒自动回退旧版）；每服务器可配 timeout；
stdio 读取移入后台线程（服务器挂起只会超时，不再卡死整个回合）。
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .tools.base import Tool, ToolError

PROTOCOL_VERSION = "2025-06-18"
LEGACY_PROTOCOL_VERSION = "2024-11-05"
DEFAULT_TIMEOUT = 30


def _version() -> str:
    from . import __version__
    return __version__


class McpError(RuntimeError):
    pass


class McpClient:
    def __init__(self, name: str, cfg: dict, cwd: Path):
        self.name = name
        self.cfg = cfg or {}
        self.cwd = Path(cwd)
        self.proc: Optional[subprocess.Popen] = None
        self.http_url = self.cfg.get("url")  # streamable HTTP transport when set
        self.http_headers = dict(self.cfg.get("headers") or {})
        self.session_id: Optional[str] = None
        self.status = "not started"
        self.error = ""
        self.tools: List[dict] = []
        self._id = 0
        try:
            self.timeout = max(1, int(self.cfg.get("timeout") or DEFAULT_TIMEOUT))
        except (TypeError, ValueError):
            self.timeout = DEFAULT_TIMEOUT
        self._lines: Optional[queue.Queue] = None

    # ---------- lifecycle ----------

    def start(self) -> bool:
        if self.http_url:
            return self._start_http()
        command = self.cfg.get("command")
        if not command:
            self.status = "failed"
            self.error = "missing 'command' or 'url'"
            return False
        try:
            env = dict(os.environ)
            env.update(self.cfg.get("env") or {})
            self.proc = subprocess.Popen(
                [str(command)] + [str(a) for a in (self.cfg.get("args") or [])],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1,
                cwd=str(self.cfg.get("cwd") or self.cwd), env=env)
        except Exception as e:
            self.status = "failed"
            self.error = str(e)[:300]
            return False
        self._start_reader()
        try:
            if not self._initialize(PROTOCOL_VERSION):
                # 旧服务器可能拒绝新版本：按规范用旧版重协商一次
                if not self._initialize(LEGACY_PROTOCOL_VERSION):
                    self.status = "failed"
                    self.error = "initialize failed (both protocol versions)"
                    self.stop()
                    return False
            self.notify("notifications/initialized", {})
            result = self.request("tools/list", {}) or {}
            self.tools = result.get("tools") or []
            self.status = "connected"
            return True
        except Exception as e:
            self.status = "failed"
            self.error = str(e)[:300]
            self.stop()
            return False

    def _initialize(self, protocol_version: str) -> bool:
        try:
            self.request("initialize", {
                "protocolVersion": protocol_version, "capabilities": {},
                "clientInfo": {"name": "minicode", "version": _version()}})
            return True
        except McpError:
            return False

    def _start_reader(self):
        """stdout 读线程：服务器挂起时 request() 按超时返回，而不是永远阻塞。"""
        self._lines = queue.Queue()
        t = threading.Thread(target=self._read_loop, daemon=True)
        t.start()

    def _read_loop(self):
        try:
            for line in self.proc.stdout:
                self._lines.put(line)
        except Exception:
            pass
        finally:
            self._lines.put(None)   # EOF 哨兵

    def stop(self):
        if self.http_url:
            self.status = "stopped"
            return
        if self.proc is not None and self.proc.poll() is None:
            try:
                try:
                    self.proc.stdin.close()
                except Exception:
                    pass
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            except Exception:
                pass
        if self.proc is not None and self.status == "connected":
            self.status = "stopped"

    # ---------- json-rpc ----------

    def _send(self, obj: dict):
        self.proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def _post_http(self, payload: dict) -> Tuple[Optional[dict], Optional[dict]]:
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream",
                   **self.http_headers}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        req = urllib.request.Request(self.http_url,
                                     data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                     method="POST", headers=headers)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            sid = resp.headers.get("Mcp-Session-Id")
            ctype = resp.headers.get("Content-Type", "")
            raw = resp.read().decode("utf-8", "replace")
            if not raw.strip():
                return None, sid
            if "text/event-stream" in ctype:
                for line in raw.splitlines():
                    line = line.strip()
                    if line.startswith("data:"):
                        try:
                            msg = json.loads(line[5:].strip())
                        except json.JSONDecodeError:
                            continue
                        if isinstance(msg, dict) and "id" in msg:
                            return msg, sid
                return None, sid
            try:
                return json.loads(raw), sid
            except json.JSONDecodeError:
                raise McpError(f"mcp:{self.name}: invalid HTTP response")

    def _http_rpc(self, method: str, params: dict) -> Optional[dict]:
        self._id += 1
        rid = self._id
        try:
            msg, sid = self._post_http(
                {"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        except urllib.error.HTTPError as e:
            raise McpError(f"mcp:{self.name}: HTTP {e.code}") from None
        except (urllib.error.URLError, OSError) as e:
            raise McpError(f"mcp:{self.name}: {e}") from None
        self.session_id = sid or self.session_id
        if msg is None:
            raise McpError(f"mcp:{self.name}: no response for {method}")
        if "error" in msg:
            err = msg["error"] or {}
            raise McpError(f"mcp:{self.name}: {err.get('message') or err}")
        return msg.get("result")

    def _http_notify(self, method: str, params: dict):
        try:
            _, sid = self._post_http({"jsonrpc": "2.0", "method": method,
                                      "params": params})
            self.session_id = sid or self.session_id
        except Exception:
            pass  # notifications are fire-and-forget

    def request(self, method: str, params: dict) -> Optional[dict]:
        if self.http_url:
            return self._http_rpc(method, params)
        if self.proc is None or self.proc.poll() is not None:
            raise McpError(f"mcp:{self.name} is not running")
        self._id += 1
        rid = self._id
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        deadline = time.time() + self.timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise McpError(f"mcp:{self.name}: timeout waiting for {method}")
            try:
                line = self._lines.get(timeout=remaining)
            except queue.Empty:
                raise McpError(
                    f"mcp:{self.name}: timeout waiting for {method}") from None
            if line is None:
                raise McpError(f"mcp:{self.name} closed the stream")
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == rid:
                if "error" in msg:
                    err = msg["error"] or {}
                    raise McpError(f"mcp:{self.name}: {err.get('message') or err}")
                return msg.get("result")
            # 其他 id 的响应 / 服务器通知：当前不处理，跳过继续等

    def notify(self, method: str, params: dict):
        if self.http_url:
            self._http_notify(method, params)
            return
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _start_http(self) -> bool:
        try:
            self._http_rpc("initialize", {
                "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                "clientInfo": {"name": "minicode", "version": _version()}})
            self.notify("notifications/initialized", {})
            listing = self.request("tools/list", {}) or {}
            self.tools = listing.get("tools") or []
            self.status = "connected"
            return True
        except Exception as e:
            self.status = "failed"
            self.error = str(e)[:300]
            self.stop()
            return False

    # ---------- tools ----------

    def call(self, tool_name: str, args: dict) -> str:
        result = self.request("tools/call", {"name": tool_name, "arguments": args}) or {}
        parts = result.get("content") or []
        texts = [p.get("text", "") for p in parts if isinstance(p, dict)
                 and p.get("type") == "text"]
        out = "\n".join(t for t in texts if t)
        if result.get("isError"):
            raise ToolError(out or f"mcp:{self.name}:{tool_name} returned an error")
        return out or "(no output)"


class McpTool(Tool):
    kind = "mcp"  # confirmed in default mode, like Claude Code

    def __init__(self, client: McpClient, tool_name: str, schema: dict):
        self._client = client
        self._tool_name = tool_name
        self.name = f"mcp__{client.name}__{tool_name}"
        self.description = (f"[MCP:{client.name}] " + (schema.get("description")
                            or f"Tool {tool_name} from MCP server {client.name}.")).strip()
        self.input_schema = schema.get("inputSchema") or {"type": "object", "properties": {}}

    def describe_call(self, args: dict) -> str:
        return json.dumps(args, ensure_ascii=False)[:100]

    def run(self, args: dict, ctx) -> str:
        return self._client.call(self._tool_name, args)


class McpManager:
    """Owns all configured servers; connect_all() returns usable tools."""

    def __init__(self, servers_cfg: Optional[dict], cwd: Path):
        self.clients: Dict[str, McpClient] = {}
        for name, cfg in (servers_cfg or {}).items():
            if isinstance(cfg, dict):
                self.clients[name] = McpClient(name, cfg, cwd)

    def connect_all(self) -> List[Tool]:
        tools: List[Tool] = []
        for client in self.clients.values():
            if client.start():
                for schema in client.tools:
                    if isinstance(schema, dict) and schema.get("name"):
                        tools.append(McpTool(client, schema["name"], schema))
        return tools

    def stop_all(self):
        for client in self.clients.values():
            client.stop()

    def status_lines(self) -> List[str]:
        lines = []
        for name, c in self.clients.items():
            state = c.status if c.status != "connected" else \
                f"connected ({len(c.tools)} tools)"
            lines.append(f"  {name:<20} {state}" + (f" — {c.error}" if c.error else ""))
            if c.status == "connected":
                for t in c.tools:
                    lines.append(f"    · mcp__{name}__{t.get('name')}")
        return lines or ["  （未配置 mcpServers）"]
