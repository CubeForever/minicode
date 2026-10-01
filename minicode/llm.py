"""LLM providers with a normalized streaming interface.

Providers turn provider-neutral messages into wire format and yield
normalized stream events:

    {"type": "text_delta", "text": str}
    {"type": "reasoning_delta", "text": str}
    {"type": "tool_call", "index": int, "id": str, "name": str}
    {"type": "tool_call_delta", "index": int, "args_delta": str}
    {"type": "finish", "stop_reason": str|None,
     "usage": {"input": int, "output": int}|None,
     "message": dict}              # final neutral assistant message

Neutral message shapes (stored in Session, persisted as JSON):
    user:      {"role": "user", "content": str}
    assistant: {"role": "assistant", "content": str|None,
                "tool_calls": [{"id", "name", "args"}],
                "thinking": [{"thinking", "signature"}]}   # anthropic, optional
    tool:      {"role": "tool", "tool_call_id": str, "name": str,
                "content": str|list, "is_error": bool}

Multi-model compatibility (learned from real relays):
    - 429/5xx retried with growing backoff (2s/5s/15s/30s), Retry-After honored
    - server rejects max_tokens / temperature / tools -> request auto-adjusted
    - model without tool support -> ToolUnsupportedError (agent degrades)
    - broken or empty SSE stream -> automatic non-stream fallback
    - tool_calls without index / dict arguments -> normalized
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Iterator, List, Optional, Tuple

RETRYABLE_CODES = {429, 500, 502, 503, 529}
RETRY_DELAYS = (2, 5, 15, 30)

# reasoning_effort 档位对应的近似思考预算（用于关键词→档位换算）
_EFFORT_TOKENS = {"low": 4000, "medium": 10000, "high": 31999}

TOOL_UNSUPPORTED_PATTERNS = [
    "tools is not supported", "tool is not supported", "tools parameter",
    "function calling", "tools are not", "tool use is not",
    "does not support tool", "unsupported.*tools", "invalid.*tools",
]


def _looks_like_tools_unsupported(msg: str) -> bool:
    low = msg.lower()
    return any(p in low for p in TOOL_UNSUPPORTED_PATTERNS)


class LLMError(RuntimeError):
    pass


class ToolUnsupportedError(LLMError):
    """The endpoint/model rejected the tools parameter."""


# ---------- neutral message builders ----------

def user_message(content: str) -> dict:
    return {"role": "user", "content": content}


def assistant_message(content: Optional[str] = None, tool_calls=None,
                      thinking: Optional[List[dict]] = None,
                      reasoning: Optional[str] = None) -> dict:
    msg = {"role": "assistant", "content": content, "tool_calls": list(tool_calls or [])}
    if thinking:
        msg["thinking"] = thinking
    if reasoning:
        msg["reasoning"] = reasoning  # OpenAI-compat thinking models require pass-back
    return msg


def tool_message(call_id: str, name: str, content, is_error: bool = False) -> dict:
    return {"role": "tool", "tool_call_id": call_id, "name": name,
            "content": content, "is_error": bool(is_error)}


# ---------- shared HTTP / SSE ----------

def _raise_http_error(e: urllib.error.HTTPError):
    raw = b""
    try:
        raw = e.read()
    except Exception:
        pass
    detail = raw.decode("utf-8", "replace")
    try:
        j = json.loads(detail)
        detail = j.get("error", {}).get("message") or j.get("message") or detail
    except Exception:
        pass
    low = detail.lower()
    if "model" in low and ("not found" in low or "does not exist" in low
                           or "not_exist" in low or "invalid model" in low):
        detail += "\n（提示：/models 查看端点可用模型，/model <名称> 切换）"
    return LLMError(f"HTTP {e.code}: {detail[:2000]}")


def _open_with_retry(req: urllib.request.Request, timeout: int,
                     retries: int = len(RETRY_DELAYS)):
    attempt = 0
    while True:
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code in RETRYABLE_CODES and attempt < retries:
                delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
                try:
                    delay = min(int(e.headers.get("Retry-After") or delay), 60)
                except (TypeError, ValueError, AttributeError):
                    pass
                time.sleep(delay)
                attempt += 1
                continue
            raise _raise_http_error(e) from None
        except urllib.error.URLError as e:
            raise LLMError(f"无法连接到 API（{e.reason}）") from None


def _post_stream(url: str, headers: dict, body: dict, timeout: int = 600):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json", "Accept": "text/event-stream", **headers})
    return _open_with_retry(req, timeout)


def _post_json(url: str, headers: dict, body: dict, timeout: int = 600) -> dict:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json", **headers})
    with _open_with_retry(req, timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _sse_data(resp) -> Iterator[str]:
    """Yield ``data:`` payload strings from an SSE byte stream."""
    for raw in resp:
        line = raw.decode("utf-8", errors="replace").strip("\r\n")
        if not line or line.startswith(":"):
            continue
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload == "[DONE]":
                return
            if payload:
                yield payload
        # "event:" lines are ignored: every Anthropic payload carries its own "type"


# ---------- wire-format conversion ----------

def _to_openai_messages(messages: List[dict], system: Optional[str]) -> List[dict]:
    out: List[dict] = []
    pending_images: List[dict] = []

    def flush_images():
        if pending_images:
            content = [{"type": "text",
                        "text": "[附图：以上工具返回的图像，供视觉分析]"}]
            for img in pending_images:
                content.append({"type": "image_url",
                                "image_url": {"url": f"data:{img.get('media_type', 'image/png')};base64,{img.get('data', '')}"}})
            out.append({"role": "user", "content": content})
            pending_images.clear()

    if system:
        out.append({"role": "system", "content": system})
    for m in messages:
        role = m["role"]
        if role != "tool":
            flush_images()
        if role == "user":
            ucontent = m.get("content")
            if isinstance(ucontent, list):  # user message with @-attached images
                parts = []
                for b in ucontent:
                    if b.get("type") == "text":
                        parts.append({"type": "text", "text": b.get("text", "")})
                    elif b.get("type") == "image":
                        parts.append({"type": "image_url",
                                      "image_url": {"url": f"data:{b.get('media_type', 'image/png')};base64,{b.get('data', '')}"}})
                out.append({"role": "user", "content": parts})
            else:
                out.append({"role": "user", "content": ucontent or ""})
        elif role == "assistant":
            entry = {"role": "assistant", "content": m.get("content") or None}
            if m.get("reasoning"):
                # thinking models (DeepSeek/GLM) require reasoning_content pass-back
                entry["reasoning_content"] = m["reasoning"]
            tcs = m.get("tool_calls") or []
            if tcs:
                entry["tool_calls"] = [
                    {"id": tc["id"], "type": "function",
                     "function": {"name": tc["name"], "arguments": tc.get("args") or "{}"}}
                    for tc in tcs]
            out.append(entry)
        elif role == "tool":
            content = m.get("content")
            if isinstance(content, list):  # multi-part tool result (may contain images)
                parts = []
                for b in content:
                    if b.get("type") == "text":
                        parts.append({"type": "text", "text": b.get("text", "")})
                    elif b.get("type") == "image":
                        # OpenAI tool messages can't carry images; relay them as a
                        # user message right after the tool batch (vision models)
                        pending_images.append({"media_type": b.get("media_type",
                                                                     "image/png"),
                                               "data": b.get("data", "")})
                out.append({"role": "tool", "tool_call_id": m["tool_call_id"],
                            "content": parts})
                continue
            text = content or ""
            if m.get("is_error"):
                text = f"[tool error] {text}"
            out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": text})
    flush_images()
    return out


_CACHEABLE_BLOCK_TYPES = {"text", "tool_result", "tool_use", "image"}


def _mark_cache_break(msg: dict) -> None:
    """在消息的最后一个可缓存内容块上打 ephemeral 断点。"""
    content = msg.get("content")
    if not isinstance(content, list):
        return
    for block in reversed(content):
        if isinstance(block, dict) and block.get("type") in _CACHEABLE_BLOCK_TYPES:
            block["cache_control"] = {"type": "ephemeral"}
            return


def _to_anthropic_messages(messages: List[dict],
                           cache_turn_breaks: bool = False) -> List[dict]:
    out: List[dict] = []
    turn_starts: List[int] = []   # out 下标：每条有内容的用户消息开启一个新回合
    for m in messages:
        role = m["role"]
        if role == "user":
            content = m.get("content")
            if isinstance(content, list):
                blocks = []
                for b in content:
                    if b.get("type") == "text":
                        blocks.append({"type": "text", "text": b.get("text", "")})
                    elif b.get("type") == "image":
                        blocks.append({"type": "image",
                                       "source": {"type": "base64",
                                                  "media_type": b.get("media_type",
                                                                      "image/png"),
                                                  "data": b.get("data", "")}})
                if blocks:
                    turn_starts.append(len(out))
                    out.append({"role": "user", "content": blocks})
            else:
                text = content or ""
                if text:
                    turn_starts.append(len(out))
                    out.append({"role": "user",
                                "content": [{"type": "text", "text": text}]})
        elif role == "assistant":
            blocks = []
            for tb in m.get("thinking") or []:
                blocks.append({"type": "thinking", "thinking": tb.get("thinking", ""),
                               "signature": tb.get("signature", "")})
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            for tc in m.get("tool_calls") or []:
                try:
                    inp = json.loads(tc.get("args") or "{}")
                    if not isinstance(inp, dict):
                        inp = {"_raw": inp}
                except json.JSONDecodeError:
                    inp = {"_raw": tc.get("args") or ""}
                blocks.append({"type": "tool_use", "id": tc["id"],
                               "name": tc["name"], "input": inp})
            if blocks:
                out.append({"role": "assistant", "content": blocks})
        elif role == "tool":
            content = m.get("content")
            if isinstance(content, list):
                blocks = []
                for b in content:
                    if b.get("type") == "text":
                        blocks.append({"type": "text", "text": b.get("text", "")})
                    elif b.get("type") == "image":
                        blocks.append({"type": "image",
                                       "source": {"type": "base64",
                                                  "media_type": b.get("media_type",
                                                                      "image/png"),
                                                  "data": b.get("data", "")}})
            else:
                blocks = [{"type": "text", "text": content or ""}]
            block = {"type": "tool_result", "tool_use_id": m["tool_call_id"],
                     "content": blocks}
            if m.get("is_error"):
                block["is_error"] = True
            prev = out[-1] if out else None
            if (prev is not None and prev["role"] == "user" and prev["content"]
                    and prev["content"][0].get("type") == "tool_result"):
                prev["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
    if cache_turn_breaks:
        # 递增 prompt 缓存断点（Anthropic 上限 4 个：system + tools 占 2，
        # 对话消息最多 2 个）。钉在最近两个已完成回合的末尾——最后一条
        # 用户消息是当前回合的提示词，它之前各回合的消息在本回合内不再
        # 变化，后续每次调用的前缀都能命中缓存；进入新回合时断点自然
        # 前移一个回合，只增量写入一小段。
        for start in turn_starts[-2:]:
            if start > 0:
                _mark_cache_break(out[start - 1])
    return out


def _norm_tool_args(args) -> str:
    """Some servers send arguments as an object instead of a string."""
    if isinstance(args, (dict, list)):
        try:
            return json.dumps(args, ensure_ascii=False)
        except (TypeError, ValueError):
            return "{}"
    return args or "{}"


# ---------- providers ----------

class Provider:
    name = "base"

    def __init__(self, model: str, api_key: str, base_url: str = "", max_tokens=None,
                 extra_body: Optional[dict] = None):
        self.model = model
        self.api_key = api_key
        self.base_url = (base_url or "").rstrip("/")
        self.max_tokens = max_tokens
        self.extra_body = extra_body or {}

    def stream(self, messages, tools, system, thinking: int = 0) -> Iterator[dict]:
        raise NotImplementedError

    def stream_text(self, messages, system) -> str:
        """Collect a plain text completion (used for compaction)."""
        parts: List[str] = []
        for ev in self.stream(messages, None, system):
            if ev["type"] == "text_delta":
                parts.append(ev["text"])
            elif ev["type"] == "finish":
                return "".join(parts)
        return "".join(parts)


class OpenAIProvider(Provider):
    """Works with OpenAI and every compatible endpoint
    (GLM, DeepSeek, Kimi, Qwen, relays, OpenRouter, vLLM, Ollama, ...)."""

    name = "openai"

    def __init__(self, model, api_key, base_url="", max_tokens=None,
                 include_usage=True, timeout: int = 600, extra_body=None,
                 reasoning_effort: str = "", stream_mode: str = "auto"):
        super().__init__(model, api_key, base_url or "https://api.openai.com/v1",
                         max_tokens, extra_body)
        self.include_usage = include_usage
        self.timeout = timeout
        self.reasoning_effort = reasoning_effort
        self.stream_mode = stream_mode  # auto | off
        self._models_cache: Optional[List[str]] = None
        self._models_ts = 0.0

    def _url(self) -> str:
        return self.base_url + "/chat/completions"

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"}

    def available_models(self, force: bool = False) -> List[str]:
        """Model ids from the endpoint, cached for 5 minutes (OpenAI-compat)."""
        if not force and self._models_cache and time.time() - self._models_ts < 300:
            return self._models_cache
        ids = list_models(self)
        self._models_cache = ids
        self._models_ts = time.time()
        return ids

    def _build_body(self, messages, tools, system, thinking: int = 0) -> dict:
        body = {
            "model": self.model,
            "messages": _to_openai_messages(messages, system),
            "stream": True,
        }
        if tools:
            body["tools"] = [
                {"type": "function",
                 "function": {"name": t["name"], "description": t["description"],
                              "parameters": t.get("input_schema")
                              or {"type": "object", "properties": {}}}}
                for t in tools]
        if self.max_tokens:
            body["max_tokens"] = int(self.max_tokens)
        effort = self.reasoning_effort
        if thinking and thinking > _EFFORT_TOKENS.get(effort or "", 0):
            # think/ultrathink 关键词在 OpenAI 兼容端落到 reasoning_effort
            effort = "high" if thinking >= 10000 else "medium"
        if effort:
            body["reasoning_effort"] = effort
        body.update(self.extra_body)
        return body

    def stream(self, messages, tools, system, thinking: int = 0):
        body = self._build_body(messages, tools, system, thinking)
        use_stream = self.stream_mode != "off"
        fell_back = False
        yielded = [False]
        while True:
            current = dict(body)
            if use_stream:
                current["stream"] = True
                if self.include_usage:
                    current["stream_options"] = {"include_usage": True}
            else:
                current["stream"] = False
                current.pop("stream_options", None)
            try:
                if use_stream:
                    resp = _post_stream(self._url(), self._headers(), current,
                                        self.timeout)
                    try:
                        for ev in self._iter_stream(resp, yielded):
                            yield ev
                        return
                    finally:
                        resp.close()
                else:
                    data = _post_json(self._url(), self._headers(), current,
                                      self.timeout)
                    yield from self._events_from_json(data)
                    return
            except LLMError as e:
                if yielded[0]:
                    raise  # never replay after partial output
                msg = str(e)
                low = msg.lower()
                if "max_completion_tokens" in low and "max_tokens" in body:
                    body["max_completion_tokens"] = body.pop("max_tokens")
                    continue
                if "max_tokens" in low and "max_tokens" in body:
                    body.pop("max_tokens", None)
                    continue
                if _looks_like_tools_unsupported(low) and body.get("tools"):
                    raise ToolUnsupportedError(msg) from None
                if "reasoning_effort" in low and "reasoning_effort" in body:
                    body.pop("reasoning_effort", None)
                    continue
                if "temperature" in low and "temperature" in current:
                    body.pop("temperature", None)
                    continue
                if (use_stream and not fell_back and self.stream_mode == "auto"
                        and ("stream" in low or "event-stream" in low
                             or "empty stream" in low or "finish event" in low)):
                    use_stream = False
                    fell_back = True
                    continue
                raise

    def _iter_stream(self, resp, yielded: Optional[list] = None) -> Iterator[dict]:
        content: List[str] = []
        reasoning_parts: List[str] = []
        slots: dict = {}
        id_to_index: dict = {}
        next_idx = 0
        usage = None
        stop = None
        saw_any = False
        saw_finish = False
        for payload in _sse_data(resp):
            saw_any = True
            try:
                chunk = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if isinstance(chunk.get("usage"), dict):
                usage = chunk["usage"]
            choices = chunk.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            if delta.get("reasoning_content"):
                if yielded is not None:
                    yielded[0] = True
                yield {"type": "reasoning_delta", "text": delta["reasoning_content"]}
                reasoning_parts.append(delta["reasoning_content"])
            if delta.get("content"):
                if yielded is not None:
                    yielded[0] = True
                yield {"type": "text_delta", "text": delta["content"]}
                content.append(delta["content"])
            for tc in delta.get("tool_calls") or []:
                if tc.get("index") is not None:
                    idx = int(tc["index"])
                    next_idx = max(next_idx, idx + 1)
                elif tc.get("id") and tc["id"] in id_to_index:
                    idx = id_to_index[tc["id"]]
                elif tc.get("id"):
                    idx = next_idx
                    id_to_index[tc["id"]] = idx
                    next_idx += 1
                else:
                    idx = max(0, next_idx - 1)
                slot = slots.setdefault(idx, {"id": "", "name": "", "args": ""})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    name = fn["name"]
                    if not slot["name"]:
                        slot["name"] = name
                    elif name != slot["name"] and name.startswith(slot["name"]):
                        slot["name"] = name  # name streamed progressively
                    if slot.get("_announced") != slot["name"]:
                        if yielded is not None:
                            yielded[0] = True
                        yield {"type": "tool_call", "index": idx,
                               "id": slot["id"], "name": slot["name"]}
                        slot["_announced"] = slot["name"]
                if fn.get("arguments"):
                    args_delta = _norm_tool_args(fn["arguments"]) \
                        if isinstance(fn["arguments"], (dict, list)) else fn["arguments"]
                    slot["args"] += args_delta
                    if yielded is not None:
                        yielded[0] = True
                    yield {"type": "tool_call_delta", "index": idx,
                           "args_delta": args_delta}
            rf = choices[0].get("finish_reason")
            if rf:
                stop = rf
                saw_finish = True
        if not saw_any:
            raise LLMError("empty stream response")
        if not saw_finish and not slots and not content:
            raise LLMError("stream ended without finish event")
        tool_calls = [{"id": s["id"] or f"call_{i}", "name": s["name"], "args": s["args"]}
                      for i, s in sorted(slots.items())]
        usage_n = None
        if usage:
            usage_n = {"input": int(usage.get("prompt_tokens") or 0),
                       "output": int(usage.get("completion_tokens") or 0)}
            cached = int(((usage.get("prompt_tokens_details") or {})
                          .get("cached_tokens")) or 0)
            if cached:
                usage_n["cache_read"] = cached
        if yielded is not None:
            yielded[0] = True
        yield {"type": "finish", "stop_reason": stop, "usage": usage_n,
               "message": assistant_message("".join(content) or None, tool_calls,
                                            reasoning="".join(reasoning_parts) or None)}

    def _events_from_json(self, data: dict) -> Iterator[dict]:
        choices = data.get("choices") or [{}]
        ch = choices[0]
        msg = ch.get("message") or {}
        rc = msg.get("reasoning_content") or msg.get("reasoning")
        if rc:
            yield {"type": "reasoning_delta", "text": rc}
        content = msg.get("content")
        if content:
            yield {"type": "text_delta", "text": content}
        tool_calls = []
        for i, tc in enumerate(msg.get("tool_calls") or []):
            fn = tc.get("function") or {}
            tc_id = tc.get("id") or f"call_{i}"
            tool_calls.append({"id": tc_id, "name": fn.get("name") or "",
                               "args": _norm_tool_args(fn.get("arguments"))})
            yield {"type": "tool_call", "index": i, "id": tc_id,
                   "name": fn.get("name") or ""}
        usage = data.get("usage")
        usage_n = None
        if isinstance(usage, dict):
            usage_n = {"input": int(usage.get("prompt_tokens") or 0),
                       "output": int(usage.get("completion_tokens") or 0)}
            cached = int(((usage.get("prompt_tokens_details") or {})
                          .get("cached_tokens")) or 0)
            if cached:
                usage_n["cache_read"] = cached
        yield {"type": "finish", "stop_reason": ch.get("finish_reason"),
               "usage": usage_n,
               "message": assistant_message(content or None, tool_calls, reasoning=rc)}


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(self, model, api_key, base_url="", max_tokens=None,
                 timeout: int = 600, extra_body=None):
        super().__init__(model, api_key, base_url or "https://api.anthropic.com",
                         max_tokens or 8192, extra_body)
        self.timeout = timeout

    def _url(self) -> str:
        return self.base_url + "/v1/messages"

    def _headers(self) -> dict:
        return {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"}

    def _build_body(self, messages, tools, system, thinking: int = 0) -> dict:
        max_tokens = int(self.max_tokens or 8192)
        body = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": _to_anthropic_messages(messages, cache_turn_breaks=True),
            "stream": True,
        }
        if system:  # prompt caching: system + tools are the stable prefix
            body["system"] = [{"type": "text", "text": system,
                               "cache_control": {"type": "ephemeral"}}]
        if tools:
            tool_list = [{"name": t["name"], "description": t["description"],
                          "input_schema": t.get("input_schema")
                          or {"type": "object", "properties": {}}}
                         for t in tools]
            tool_list[-1]["cache_control"] = {"type": "ephemeral"}
            body["tools"] = tool_list
        if thinking:
            body["thinking"] = {"type": "enabled", "budget_tokens": int(thinking)}
            body["max_tokens"] = max(max_tokens, int(thinking) + 4096)
        body.update(self.extra_body)
        return body

    def stream(self, messages, tools, system, thinking: int = 0):
        """带请求体自适应的流式调用（与 OpenAI 路径同一套鲁棒性语义）：
        thinking / tools / cache_control 被端点拒绝时逐级降级；流式不可用
        自动回退非流式。已产出事件后绝不重放。"""
        body = self._build_body(messages, tools, system, thinking)
        use_stream = True
        yielded = [False]
        while True:
            current = dict(body)
            current["stream"] = use_stream
            try:
                if use_stream:
                    resp = _post_stream(self._url(), self._headers(), current,
                                        self.timeout)
                    try:
                        yield from self._iter_stream(resp, yielded)
                        return
                    finally:
                        resp.close()
                else:
                    data = _post_json(self._url(), self._headers(), current,
                                      self.timeout)
                    yield from self._events_from_json(data)
                    return
            except LLMError as e:
                if yielded[0]:
                    raise   # 部分输出已流出——绝不重放
                msg = str(e)
                low = msg.lower()
                if "thinking" in low and body.get("thinking"):
                    body.pop("thinking", None)
                    continue
                if _looks_like_tools_unsupported(low) and body.get("tools"):
                    raise ToolUnsupportedError(msg) from None
                if ("cache_control" in low or "ephemeral" in low) and \
                        self._has_cache_control(body):
                    body = self._strip_cache_control(body)
                    continue
                if use_stream and ("stream" in low or "event-stream" in low
                                   or "empty stream" in low):
                    use_stream = False
                    continue
                raise

    @staticmethod
    def _has_cache_control(body: dict) -> bool:
        def scan(obj) -> bool:
            if isinstance(obj, dict):
                if "cache_control" in obj:
                    return True
                return any(scan(v) for v in obj.values())
            if isinstance(obj, list):
                return any(scan(v) for v in obj)
            return False
        return scan(body)

    @staticmethod
    def _strip_cache_control(body: dict) -> dict:
        def clean(obj):
            if isinstance(obj, dict):
                return {k: clean(v) for k, v in obj.items() if k != "cache_control"}
            if isinstance(obj, list):
                return [clean(v) for v in obj]
            return obj
        return {k: clean(v) for k, v in body.items()}

    def _iter_stream(self, resp, yielded: Optional[list] = None) -> Iterator[dict]:
        blocks: dict = {}
        usage_in = 0
        cache_read = 0
        cache_create = 0
        usage_out = 0
        stop = None
        for payload in _sse_data(resp):
            try:
                ev = json.loads(payload)
            except json.JSONDecodeError:
                continue
            t = ev.get("type")
            if t == "message_start":
                u = (ev.get("message") or {}).get("usage") or {}
                cache_read = int(u.get("cache_read_input_tokens") or 0)
                cache_create = int(u.get("cache_creation_input_tokens") or 0)
                usage_in = (int(u.get("input_tokens") or 0)
                            + cache_read + cache_create)
            elif t == "content_block_start":
                cb = ev.get("content_block") or {}
                idx = ev.get("index", 0)
                ctype = cb.get("type")
                if ctype == "tool_use":
                    blocks[idx] = {"type": "tool_use", "id": cb.get("id", ""),
                                   "name": cb.get("name", ""), "args": ""}
                    if yielded is not None:
                        yielded[0] = True
                    yield {"type": "tool_call", "index": idx,
                           "id": cb.get("id", ""), "name": cb.get("name", "")}
                elif ctype == "thinking":
                    blocks[idx] = {"type": "thinking", "thinking": "", "signature": ""}
                else:
                    blocks[idx] = {"type": "text", "text": ""}
            elif t == "content_block_delta":
                d = ev.get("delta") or {}
                idx = ev.get("index", 0)
                dtype = d.get("type")
                if dtype == "text_delta":
                    b = blocks.setdefault(idx, {"type": "text", "text": ""})
                    b["text"] = b.get("text", "") + d.get("text", "")
                    if yielded is not None:
                        yielded[0] = True
                    yield {"type": "text_delta", "text": d.get("text", "")}
                elif dtype == "thinking_delta":
                    b = blocks.setdefault(idx, {"type": "thinking", "thinking": "", "signature": ""})
                    b["thinking"] += d.get("thinking", "")
                    if yielded is not None:
                        yielded[0] = True
                    yield {"type": "reasoning_delta", "text": d.get("thinking", "")}
                elif dtype == "signature_delta":
                    b = blocks.setdefault(idx, {"type": "thinking", "thinking": "", "signature": ""})
                    b["signature"] += d.get("signature", "")
                elif dtype == "input_json_delta":
                    b = blocks.setdefault(idx, {"type": "tool_use", "id": "", "name": "", "args": ""})
                    b["args"] = b.get("args", "") + d.get("partial_json", "")
                    if yielded is not None:
                        yielded[0] = True
                    yield {"type": "tool_call_delta", "index": idx,
                           "args_delta": d.get("partial_json", "")}
            elif t == "message_delta":
                stop = (ev.get("delta") or {}).get("stop_reason") or stop
                u = ev.get("usage") or {}
                usage_out = int(u.get("output_tokens") or usage_out)
            elif t == "error":
                err = ev.get("error") or {}
                raise LLMError(f"stream error: {err.get('type')}: {err.get('message')}")
        usage = {"input": usage_in, "output": usage_out}
        if cache_read:
            usage["cache_read"] = cache_read
        if cache_create:
            usage["cache_creation"] = cache_create
        yield from self._finish_events(blocks, stop, usage)

    def _events_from_json(self, data: dict) -> Iterator[dict]:
        """非流式回退：与流式同一套事件语义（UI 仍能看到逐步输出）。"""
        blocks: dict = {}
        for i, b in enumerate(data.get("content") or []):
            bt = b.get("type")
            if bt == "text":
                blocks[i] = {"type": "text", "text": b.get("text", "")}
                if b.get("text"):
                    yield {"type": "text_delta", "text": b["text"]}
            elif bt == "thinking":
                blocks[i] = {"type": "thinking", "thinking": b.get("thinking", ""),
                             "signature": b.get("signature", "")}
                if b.get("thinking"):
                    yield {"type": "reasoning_delta", "text": b["thinking"]}
            elif bt == "tool_use":
                try:
                    args = json.dumps(b.get("input") or {}, ensure_ascii=False)
                except (TypeError, ValueError):
                    args = "{}"
                blocks[i] = {"type": "tool_use", "id": b.get("id", ""),
                             "name": b.get("name", ""), "args": args}
                yield {"type": "tool_call", "index": i,
                       "id": b.get("id", ""), "name": b.get("name", "")}
                yield {"type": "tool_call_delta", "index": i, "args_delta": args}
        u = data.get("usage") or {}
        cache_read = int(u.get("cache_read_input_tokens") or 0)
        cache_create = int(u.get("cache_creation_input_tokens") or 0)
        usage = {"input": (int(u.get("input_tokens") or 0) + cache_read + cache_create),
                 "output": int(u.get("output_tokens") or 0)}
        if cache_read:
            usage["cache_read"] = cache_read
        if cache_create:
            usage["cache_creation"] = cache_create
        yield from self._finish_events(blocks, data.get("stop_reason"), usage)

    @staticmethod
    def _finish_events(blocks: dict, stop, usage) -> Iterator[dict]:
        text_parts: List[str] = []
        tool_calls: List[dict] = []
        thinking_blocks: List[dict] = []
        for idx, b in sorted(blocks.items()):
            if b["type"] == "text":
                text_parts.append(b["text"])
            elif b["type"] == "tool_use":
                tool_calls.append({"id": b["id"], "name": b["name"],
                                   "args": b.get("args") or "{}"})
            elif b["type"] == "thinking" and b.get("thinking"):
                thinking_blocks.append({"thinking": b["thinking"],
                                        "signature": b.get("signature", "")})
        yield {"type": "finish", "stop_reason": stop, "usage": usage,
               "message": assistant_message("".join(text_parts) or None, tool_calls,
                                            thinking_blocks)}


class FailoverProvider(Provider):
    """模型故障转移：主模型失败时自动切换备用模型继续同一回合。

    触发条件：连接失败 / 限速重试耗尽 / 5xx / 模型不存在 / 端点不支持工具
    等 LLMError，且**尚未产出任何流事件**（部分输出绝不跨模型重放）。
    会话消息是中性格式，跨厂商切换无损。on_event 回调向用户展示切换过程。
    """

    def __init__(self, primary: Provider, fallbacks: List[Provider], on_event=None):
        # providers 必须先于 super().__init__ 赋值：model 是 property，
        # 其 setter 会写 providers[0].model
        self.providers = [primary] + list(fallbacks)
        super().__init__(primary.model, primary.api_key, primary.base_url,
                         primary.max_tokens, primary.extra_body)
        self.on_event = on_event
        self.active = 0   # 当前生效的 provider 下标

    # -- delegation --
    @property
    def primary(self) -> Provider:
        return self.providers[0]

    @property
    def model(self) -> str:
        return self.providers[self.active].model

    @model.setter
    def model(self, value):
        self.providers[0].model = value   # /model 切换改主模型并回到主模型
        self.active = 0

    @property
    def name(self) -> str:
        n = len(self.providers) - 1
        return f"{self.primary.name}+{n}备用" if n else self.primary.name

    @property
    def reasoning_effort(self):
        return getattr(self.primary, "reasoning_effort", "")

    @reasoning_effort.setter
    def reasoning_effort(self, value):
        for p in self.providers:
            if hasattr(p, "reasoning_effort"):
                p.reasoning_effort = value

    def _notify(self, msg: str):
        if self.on_event:
            try:
                self.on_event(msg)
            except Exception:
                pass

    def stream(self, messages, tools, system, thinking: int = 0):
        last_err: Optional[LLMError] = None
        for i, provider in enumerate(self.providers):
            yielded = [False]
            try:
                for ev in provider.stream(messages, tools, system, thinking):
                    yielded[0] = True
                    yield ev
                return
            except LLMError as e:
                if yielded[0]:
                    raise   # 部分输出已流出——绝不跨模型重放
                last_err = e
                if i + 1 < len(self.providers):
                    nxt = self.providers[i + 1]
                    self._notify(f"模型 {provider.model} 调用失败（{str(e)[:120]}）"
                                 f"—— 自动切换备用模型 {nxt.model}")
                    self.active = i + 1
        raise last_err   # 所有模型都失败：抛最后一个错误


def _fallback_cfg(cfg, fb: dict):
    """备用模型条目 → 独立 Config。model 必填，provider/base_url/api_key
    缺省继承主配置。"""
    import dataclasses
    return dataclasses.replace(
        cfg,
        provider=str(fb.get("provider") or cfg.provider),
        model=str(fb.get("model") or ""),
        base_url=str(fb.get("base_url") or cfg.base_url or ""),
        api_key=str(fb.get("api_key") or cfg.api_key or ""),
        fallbacks=[],
    )


def make_provider(cfg, on_failover=None) -> Provider:
    fake = os.environ.get("MINICODE_FAKE_LLM", "").strip()
    if fake:
        from .fake import FakeProvider
        return FakeProvider.demo() if fake.lower() == "demo" else FakeProvider.from_file(fake)
    primary = _build_single_provider(cfg)
    # 直接构造 Config 可能绕过 load_config 的归一化——这里再做一次
    from .config import _normalize_fallbacks
    fallback_cfgs = [fb for fb in _normalize_fallbacks(getattr(cfg, "fallbacks", None))
                     if fb.get("model")]
    if not fallback_cfgs:
        return primary
    fallbacks = [_build_single_provider(_fallback_cfg(cfg, fb))
                 for fb in fallback_cfgs]
    return FailoverProvider(primary, fallbacks, on_event=on_failover)


def _build_single_provider(cfg) -> Provider:
    if cfg.provider == "anthropic":
        return AnthropicProvider(cfg.model, cfg.api_key, cfg.base_url, cfg.max_tokens,
                                 extra_body=cfg.extra_body)
    return OpenAIProvider(cfg.model, cfg.api_key, cfg.base_url, cfg.max_tokens,
                          cfg.include_usage, extra_body=cfg.extra_body,
                          reasoning_effort=getattr(cfg, "reasoning_effort", ""),
                          stream_mode=os.environ.get("MINICODE_STREAM_MODE", "auto"))


# ---------- diagnostics: model listing & live probe ----------

def _unwrap(provider: Provider) -> Provider:
    """FailoverProvider 委托到主 provider（模型列表/体检只关心主端点）。"""
    return getattr(provider, "primary", provider)


def list_models(provider: Provider) -> List[str]:
    provider = _unwrap(provider)
    if not isinstance(provider, OpenAIProvider):
        raise LLMError("/models 仅支持 OpenAI 兼容端")
    req = urllib.request.Request(
        provider.base_url + "/models",
        headers={"Authorization": f"Bearer {provider.api_key}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raise _raise_http_error(e) from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise LLMError(f"获取模型列表失败：{e}") from None
    return [m.get("id", "") for m in data.get("data", []) if isinstance(m, dict)
            and m.get("id")]


def probe_provider(provider: Provider) -> List[Tuple[str, bool, str, float]]:
    """Live compatibility probe for OpenAI-compatible endpoints.
    Returns rows: (step, ok, detail, elapsed_seconds)."""
    provider = _unwrap(provider)
    if not isinstance(provider, OpenAIProvider):
        return [("probe", False, "仅支持 OpenAI 兼容端（中转站/自建）", 0.0)]
    rows: List[Tuple[str, bool, str, float]] = []

    def step(name, fn):
        t0 = time.time()
        try:
            detail = fn()
            rows.append((name, True, detail, time.time() - t0))
        except Exception as e:
            rows.append((name, False, str(e)[:160], time.time() - t0))

    def check_models():
        ids = list_models(provider)
        return f"{len(ids)} 个模型可用"
    step("模型列表 /models", check_models)

    def check_plain():
        data = _post_json(provider._url(), provider._headers(), {
            "model": provider.model, "stream": False, "max_tokens": 200,
            "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
        }, timeout=120)
        text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        if not text.strip():
            raise LLMError("空回复")
        return f"回复: {text.strip()[:40]!r}"
    step("非流式补全", check_plain)

    def check_stream():
        body = provider._build_body(
            [{"role": "user", "content": "Reply with exactly: OK"}], None, None)
        body["stream"] = True
        resp = _post_stream(provider._url(), provider._headers(), body, timeout=120)
        chars = 0
        try:
            for ev in provider._iter_stream(resp):
                if ev["type"] == "text_delta":
                    chars += len(ev["text"])
        finally:
            resp.close()
        if chars <= 0:
            raise LLMError("流式无内容")
        return f"流式收到 {chars} 字符"
    step("流式补全", check_stream)

    def check_tools():
        body = provider._build_body(
            [{"role": "user",
              "content": "You must call the echo_test tool with text='ping'. "
                         "Do not reply with plain text."}],
            [{"name": "echo_test", "description": "Echo text back",
              "input_schema": {"type": "object",
                               "properties": {"text": {"type": "string"}},
                               "required": ["text"]}}], None)
        body["stream"] = False
        data = _post_json(provider._url(), provider._headers(), body, timeout=120)
        tcs = ((data.get("choices") or [{}])[0].get("message") or {}).get("tool_calls") or []
        if not tcs:
            raise LLMError("模型没有调用工具")
        names = [t.get("function", {}).get("name") for t in tcs]
        return f"调用 {names}"
    step("工具调用", check_tools)
    return rows
