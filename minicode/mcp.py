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
import re
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
MAX_RESOURCES_PER_SERVER = 20
# 渐进披露阈值：MCP 工具+资源总数超过它时不把全部 schema 塞进上下文，
# 改为注册 mcp_search 元工具，模型按关键词检索、命中即动态注册（v0.22）。
MCP_INLINE_LIMIT = 15


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
        self.resources: List[dict] = []
        self.prompts: List[dict] = []
        self.server_info: dict = {}
        self.server_capabilities: dict = {}
        self.restart_count = 0   # 服务器崩溃后自动重启的次数（/mcp 状态可见）
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
            self.resources = self._try_list(self._list_resources)
            self.prompts = self._try_list(self._list_prompts)
            self.status = "connected"
            return True
        except Exception as e:
            self.status = "failed"
            self.error = str(e)[:300]
            self.stop()
            return False

    @staticmethod
    def _try_list(fn) -> list:
        try:
            return fn()
        except McpError:
            return []   # 声明了能力但列取失败：不阻塞连接

    def _initialize(self, protocol_version: str) -> bool:
        try:
            result = self.request("initialize", {
                "protocolVersion": protocol_version, "capabilities": {},
                "clientInfo": {"name": "minicode", "version": _version()}})
            self.server_info = result or {}
            self.server_capabilities = (result or {}).get("capabilities") or {}
            return True
        except McpError:
            return False

    # ---------- resources / prompts ----------

    def _list_resources(self) -> list:
        # 服务器以空对象 {} 声明"支持"——必须检查键存在性而非真值
        if "resources" not in self.server_capabilities:
            return []
        res = self.request("resources/list", {}) or {}
        return [r for r in (res.get("resources") or []) if isinstance(r, dict)]

    def _list_prompts(self) -> list:
        if "prompts" not in self.server_capabilities:
            return []
        res = self.request("prompts/list", {}) or {}
        return [p for p in (res.get("prompts") or []) if isinstance(p, dict)]

    def read_resource(self, uri: str) -> str:
        res = self.request("resources/read", {"uri": uri}) or {}
        parts = res.get("contents") or []
        texts = [p.get("text", "") for p in parts if isinstance(p, dict)
                 and p.get("type") == "text"]
        return "\n".join(t for t in texts if t) or "(empty resource)"

    def get_prompt(self, name: str, arguments: Optional[dict] = None) -> str:
        res = self.request("prompts/get",
                           {"name": name, "arguments": arguments or {}}) or {}
        texts: List[str] = []
        for m in res.get("messages") or []:
            c = m.get("content") if isinstance(m, dict) else None
            if isinstance(c, dict) and c.get("type") == "text":
                texts.append(c.get("text", ""))
            elif isinstance(c, list):
                texts += [b.get("text", "") for b in c
                          if isinstance(b, dict) and b.get("type") == "text"]
        return "\n\n".join(t for t in texts if t)

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
            if not self._initialize(PROTOCOL_VERSION):
                self.status = "failed"
                self.error = "initialize failed"
                self.stop()
                return False
            self.notify("notifications/initialized", {})
            listing = self.request("tools/list", {}) or {}
            self.tools = listing.get("tools") or []
            self.resources = self._try_list(self._list_resources)
            self.prompts = self._try_list(self._list_prompts)
            self.status = "connected"
            return True
        except Exception as e:
            self.status = "failed"
            self.error = str(e)[:300]
            self.stop()
            return False

    # ---------- tools ----------

    def call(self, tool_name: str, args: dict) -> str:
        """调用工具。stdio 服务器意外崩溃时自动重启一次并重试同一调用；
        显式 stop 过的（status=stopped）不重启。"""
        try:
            return self._call_impl(tool_name, args)
        except McpError as e:
            if self.http_url or self.status == "stopped" \
                    or getattr(self, "_retrying", False):
                raise
            crashed = (self.proc is not None and self.proc.poll() is not None) \
                or "closed the stream" in str(e)
            if not crashed:
                raise
            self._retrying = True
            try:
                if self.start():
                    self.restart_count += 1
                    return self._call_impl(tool_name, args)
                raise ToolError(
                    f"mcp:{self.name} 已崩溃且自动重启失败：{self.error}") from None
            finally:
                self._retrying = False

    def _call_impl(self, tool_name: str, args: dict) -> str:
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


class McpResourceTool(Tool):
    """MCP resource exposed as a read-only tool: `mcp__<server>__get__<name>`."""
    kind = "read"

    def __init__(self, client: "McpClient", uri: str, slug: str, meta: dict):
        self._client = client
        self._uri = uri
        self.name = f"mcp__{client.name}__get__{slug}"
        desc = (meta.get("description") or meta.get("mimeType")
                or "MCP resource")
        self.description = (f"[MCP:{client.name}] Read resource "
                            f"'{meta.get('name') or uri}': {desc}").strip()[:300]
        self.input_schema = {"type": "object", "properties": {}}

    def describe_call(self, args: dict) -> str:
        return str(self._uri)

    def run(self, args: dict, ctx) -> str:
        return self._client.read_resource(self._uri)


class McpSearchTool(Tool):
    """渐进披露元工具（对标 Anthropic Tool Search）。

    MCP 工具总数超过 MCP_INLINE_LIMIT 时，不把全部 schema 塞进 system，
    只注册本工具；模型按关键词检索，命中的真实工具随即动态注册进
    registry，下一轮即可正常调用。会话内加载一次后永久可用（registry
    持有），重复检索自动跳过已加载项。kind=meta：不触发权限确认。
    """
    name = "mcp_search"
    kind = "meta"

    def __init__(self, pool: List[Tool], registry: "object"):
        self._pool = pool            # connect_all() 的全部工具（已构建）
        self._registry = registry    # ToolRegistry，命中后 register
        self.description = (
            f"Search and load MCP tools on demand ({len(pool)} available across "
            "configured servers). Returns the loaded tool names — call them "
            "directly afterwards. Use when no mcp__ tool you need is loaded yet.")
        self.input_schema = {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "Keywords from the task, e.g. 'jira issue create'."},
            },
            "required": ["query"],
        }

    def describe_call(self, args: dict) -> str:
        return str(args.get("query") or "")

    def run(self, args: dict, ctx) -> str:
        query = str(args.get("query") or "")
        terms = set(re.findall(r"[a-z0-9_]{2,}", query.lower()))
        scored = []
        for t in self._pool:
            if t.name in self._registry.tools:
                continue   # 已加载
            hay = (t.name + " " + t.description).lower()
            score = sum(1 for term in terms if term in hay)
            scored.append((score, t))
        scored.sort(key=lambda st: -st[0])
        picked = [t for s, t in scored[:8] if s > 0] or \
                 [t for _s, t in scored[:3]]
        if not picked:
            loaded = sum(1 for t in self._pool if t.name in self._registry.tools)
            return (f"没有更多可加载的 MCP 工具（总数 {len(self._pool)}，"
                    f"已加载 {loaded}）。")
        for t in picked:
            try:
                self._registry.register(t)
            except ValueError:
                pass
        return ("已加载以下 MCP 工具，现在可以直接调用：\n"
                + "\n".join(f"- {t.name}: {t.description[:140]}" for t in picked))


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
            if not client.start():
                continue
            for schema in client.tools:
                if isinstance(schema, dict) and schema.get("name"):
                    tools.append(McpTool(client, schema["name"], schema))
            for i, res in enumerate((client.resources or [])[:MAX_RESOURCES_PER_SERVER]):
                slug = re.sub(r"[^A-Za-z0-9_\-]", "_",
                              str(res.get("name") or res.get("uri") or f"res{i}"))[:48]
                tools.append(McpResourceTool(client, str(res.get("uri") or ""),
                                             slug, res))
        return tools

    def stop_all(self):
        for client in self.clients.values():
            client.stop()

    def status_lines(self) -> List[str]:
        lines = []
        for name, c in self.clients.items():
            state = c.status if c.status != "connected" else \
                f"connected ({len(c.tools)} tools)"
            if c.restart_count:
                state += f" · 自动重启 {c.restart_count} 次"
            lines.append(f"  {name:<20} {state}" + (f" — {c.error}" if c.error else ""))
            if c.status == "connected":
                for t in c.tools:
                    lines.append(f"    · mcp__{name}__{t.get('name')}")
                if c.resources:
                    lines.append(f"    · {len(c.resources)} resources"
                                 f"（mcp__{name}__get__* 只读工具）")
                if c.prompts:
                    lines.append(f"    · {len(c.prompts)} prompts"
                                 f"（/prompt {name} <名称> [键=值] 调用）")
        return lines or ["  （未配置 mcpServers）"]
