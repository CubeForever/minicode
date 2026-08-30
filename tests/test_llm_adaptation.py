import io
import json
import urllib.error
import urllib.request

import pytest

from minicode import llm
from minicode.llm import (LLMError, OpenAIProvider, ToolUnsupportedError)


class FakeResp:
    def __init__(self, chunks):
        self.chunks = chunks

    def __iter__(self):
        return iter(self.chunks)

    def close(self):
        pass


class HTTPError429(urllib.error.HTTPError):
    def __init__(self, detail=b'{"error":{"message":"rpm exhausted"}}'):
        super().__init__("http://x", 429, "too many requests", None,
                         io.BytesIO(detail))


def test_tool_calls_without_index_two_calls():
    chunks = [
        b'data: {"choices":[{"delta":{"tool_calls":[{"id":"c1","function":'
        b'{"name":"bash","arguments":"{\\"command\\":\\"ls\\"}"}}]}}]}\n\n',
        b'data: {"choices":[{"delta":{"tool_calls":[{"id":"c2","function":'
        b'{"name":"read_file","arguments":"{\\"path\\":\\"x\\"}"}}]}}]}\n\n',
        b'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n\n',
        b'data: [DONE]\n\n',
    ]
    p = OpenAIProvider("m", "k")
    events = list(p._iter_stream(FakeResp(chunks)))
    msg = events[-1]["message"]
    assert [t["name"] for t in msg["tool_calls"]] == ["bash", "read_file"]
    assert msg["tool_calls"][0]["args"] == '{"command":"ls"}'
    assert events[-1]["stop_reason"] == "tool_calls"


def test_dict_arguments_shim_stream():
    chunks = [
        b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1",'
        b'"function":{"name":"write_file","arguments":{"path":"a.txt"}}}]}}]}\n\n',
        b'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n\n',
        b'data: [DONE]\n\n',
    ]
    p = OpenAIProvider("m", "k")
    msg = list(p._iter_stream(FakeResp(chunks)))[-1]["message"]
    assert msg["tool_calls"][0]["args"] == json.dumps({"path": "a.txt"},
                                                      ensure_ascii=False)


def test_events_from_json_dict_args_and_usage():
    data = {"choices": [{"message": {"content": None, "tool_calls": [
        {"id": "a", "function": {"name": "write_file",
                                 "arguments": {"path": "x"}}}]},
        "finish_reason": "tool_calls"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1}}
    events = list(OpenAIProvider("m", "k")._events_from_json(data))
    msg = events[-1]["message"]
    assert msg["tool_calls"][0]["args"] == '{"path": "x"}'
    assert events[-1]["usage"] == {"input": 3, "output": 1}


def test_compat_retry_drops_max_tokens(monkeypatch):
    calls = []

    def fake_post_stream(url, headers, body, timeout=600):
        calls.append(body)
        if len(calls) == 1:
            raise LLMError("HTTP 400: max_tokens is not supported by this model")
        return FakeResp([
            b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n',
            b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
            b'data: [DONE]\n\n',
        ])

    monkeypatch.setattr(llm, "_post_stream", fake_post_stream)
    p = OpenAIProvider("m", "k", max_tokens=123)
    events = list(p.stream([{"role": "user", "content": "x"}], None, None))
    assert calls[0].get("max_tokens") == 123
    assert "max_tokens" not in calls[1]
    assert events[-1]["message"]["content"] == "hi"


def test_compat_retry_max_completion_tokens(monkeypatch):
    calls = []

    def fake_post_stream(url, headers, body, timeout=600):
        calls.append(body)
        if len(calls) == 1:
            raise LLMError("HTTP 400: use max_completion_tokens instead")
        return FakeResp([
            b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n',
            b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
        ])

    monkeypatch.setattr(llm, "_post_stream", fake_post_stream)
    p = OpenAIProvider("m", "k", max_tokens=500)
    list(p.stream([{"role": "user", "content": "x"}], None, None))
    assert calls[1].get("max_completion_tokens") == 500
    assert "max_tokens" not in calls[1]


def test_tool_unsupported_error(monkeypatch):
    def fake_post_stream(url, headers, body, timeout=600):
        raise LLMError("HTTP 400: tools is not supported by this model")

    monkeypatch.setattr(llm, "_post_stream", fake_post_stream)
    p = OpenAIProvider("m", "k")
    with pytest.raises(ToolUnsupportedError):
        list(p.stream([{"role": "user", "content": "x"}],
                      [{"name": "t", "description": "",
                        "input_schema": {"type": "object", "properties": {}}}], None))


def test_nonstream_fallback_on_stream_error(monkeypatch):
    def fake_post_stream(url, headers, body, timeout=600):
        assert body.get("stream") is True
        raise LLMError("HTTP 400: stream mode is not supported")

    json_calls = []

    def fake_post_json(url, headers, body, timeout=600):
        json_calls.append(body)
        return {"choices": [{"message": {"content": "hello ns"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2}}

    monkeypatch.setattr(llm, "_post_stream", fake_post_stream)
    monkeypatch.setattr(llm, "_post_json", fake_post_json)
    events = list(OpenAIProvider("m", "k").stream(
        [{"role": "user", "content": "x"}], None, None))
    assert json_calls and json_calls[0].get("stream") is False
    assert events[-1]["message"]["content"] == "hello ns"
    assert events[-1]["usage"] == {"input": 5, "output": 2}


def test_empty_stream_falls_back(monkeypatch):
    monkeypatch.setattr(llm, "_post_stream",
                        lambda url, headers, body, timeout=600: FakeResp([]))
    monkeypatch.setattr(llm, "_post_json",
                        lambda url, headers, body, timeout=600:
                        {"choices": [{"message": {"content": "recovered"},
                                      "finish_reason": "stop"}]})
    events = list(OpenAIProvider("m", "k").stream(
        [{"role": "user", "content": "x"}], None, None))
    assert events[-1]["message"]["content"] == "recovered"


def test_429_backoff_growing_delays(monkeypatch):
    sleeps = []
    monkeypatch.setattr(llm.time, "sleep", lambda s: sleeps.append(s))
    attempts = {"n": 0}

    def fake_urlopen(req, timeout=None):
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise HTTPError429()
        return FakeResp([b"data: [DONE]\n\n"])

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    req = urllib.request.Request("http://x", data=b"{}", method="POST")
    resp = llm._open_with_retry(req, timeout=10, retries=4)
    assert list(resp) == [b"data: [DONE]\n\n"]
    assert sleeps[:2] == [2, 5]
    assert attempts["n"] == 3


def test_429_gives_up_after_retries(monkeypatch):
    sleeps = []
    monkeypatch.setattr(llm.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(llm.urllib.request, "urlopen",
                        lambda req, timeout=None: (_ for _ in ()).throw(HTTPError429()))
    req = urllib.request.Request("http://x", data=b"{}", method="POST")
    with pytest.raises(LLMError, match="429"):
        llm._open_with_retry(req, timeout=10, retries=2)
    assert len(sleeps) == 2


def test_no_retry_on_400(monkeypatch):
    monkeypatch.setattr(llm.time, "sleep", lambda s: sleeps.append(s) if False else None)
    sleeps = []
    monkeypatch.setattr(llm.time, "sleep", lambda s: sleeps.append(s))

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError("http://x", 400, "bad", None,
                                     io.BytesIO(b'{"error":{"message":"bad request"}}'))

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    req = urllib.request.Request("http://x", data=b"{}", method="POST")
    with pytest.raises(LLMError, match="400"):
        llm._open_with_retry(req, timeout=10, retries=4)
    assert sleeps == []  # non-retryable: fail fast


def test_probe_provider_all_steps(monkeypatch):
    p = OpenAIProvider("m", "k")
    monkeypatch.setattr(llm, "list_models", lambda prov: ["m", "x"])

    def fake_post_json(url, headers, body, timeout=600):
        if "tools" in body:
            return {"choices": [{"message": {"content": None, "tool_calls": [
                {"id": "a", "function": {"name": "echo_test",
                                         "arguments": "{\"text\": \"ping\"}"}}]},
                "finish_reason": "tool_calls"}]}
        return {"choices": [{"message": {"content": "OK"},
                             "finish_reason": "stop"}]}

    monkeypatch.setattr(llm, "_post_json", fake_post_json)
    monkeypatch.setattr(llm, "_post_stream", lambda url, h, b, timeout=600: FakeResp([
        b'data: {"choices":[{"delta":{"content":"OK"}}]}\n\n',
        b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
    ]))
    rows = llm.probe_provider(p)
    assert [r[0] for r in rows] == ["模型列表 /models", "非流式补全", "流式补全", "工具调用"]
    assert all(r[1] for r in rows), rows


def test_probe_reports_tool_failure(monkeypatch):
    p = OpenAIProvider("m", "k")
    monkeypatch.setattr(llm, "list_models", lambda prov: ["m"])

    def fake_post_json(url, headers, body, timeout=600):
        if "tools" in body:
            raise LLMError("HTTP 400: tools unsupported")
        return {"choices": [{"message": {"content": "OK"},
                             "finish_reason": "stop"}]}

    monkeypatch.setattr(llm, "_post_json", fake_post_json)
    monkeypatch.setattr(llm, "_post_stream", lambda url, h, b, timeout=600: FakeResp([
        b'data: {"choices":[{"delta":{"content":"OK"}}]}\n\n',
        b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
    ]))
    rows = llm.probe_provider(p)
    tools_row = rows[-1]
    assert tools_row[0] == "工具调用" and tools_row[1] is False


# ---------- thinking-mode reasoning pass-back (DeepSeek/GLM) ----------

def test_reasoning_accumulated_and_passed_back():
    chunks = [
        b'data: {"choices":[{"delta":{"reasoning_content":"think "}}]}\n\n',
        b'data: {"choices":[{"delta":{"reasoning_content":"hard"}}]}\n\n',
        b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","function":'
        b'{"name":"edit_file","arguments":"{}"}}]}}]}\n\n',
        b'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n\n',
        b'data: [DONE]\n\n',
    ]
    p = OpenAIProvider("m", "k")
    events = list(p._iter_stream(FakeResp(chunks)))
    msg = events[-1]["message"]
    assert msg["reasoning"] == "think hard"
    assert events[-2]["type"] == "tool_call" or any(
        e["type"] == "tool_call" for e in events)
    # replay must include reasoning_content on the assistant message
    wire = llm._to_openai_messages([
        {"role": "user", "content": "go"}, msg,
        {"role": "tool", "tool_call_id": "c1", "name": "edit_file",
         "content": "ok", "is_error": False},
    ], "sys")
    assert wire[2]["reasoning_content"] == "think hard"
    assert wire[2]["tool_calls"][0]["id"] == "c1"


def test_tools_unsupported_shim_does_not_misfire():
    # deepseek thinking-mode pass-back error contains "tool_calls" — must NOT
    # be treated as "tools unsupported"
    assert not llm._looks_like_tools_unsupported(
        "HTTP 400: If thinking mode and tool_calls, `reasoning_content` must be "
        "passed back to the API.")
    assert llm._looks_like_tools_unsupported("HTTP 400: tools is not supported")
    assert llm._looks_like_tools_unsupported("HTTP 400: Function calling is disabled")
    assert llm._looks_like_tools_unsupported("HTTP 400: invalid tools parameter")
    assert not llm._looks_like_tools_unsupported("HTTP 429: rpm exhausted")


def test_reasoning_error_not_misread_as_tool_unsupported(monkeypatch):
    def fake_post_stream(url, headers, body, timeout=600):
        raise LLMError("HTTP 400: If thinking mode and tool_calls, "
                       "`reasoning_content` must be passed back to the API.")

    monkeypatch.setattr(llm, "_post_stream", fake_post_stream)
    p = OpenAIProvider("m", "k")
    with pytest.raises(LLMError) as exc:
        list(p.stream([{"role": "user", "content": "x"}],
                      [{"name": "t", "description": "",
                        "input_schema": {"type": "object", "properties": {}}}], None))
    assert not isinstance(exc.value, ToolUnsupportedError)


def test_nonstream_message_keeps_reasoning():
    data = {"choices": [{"message": {
        "content": "answer", "reasoning_content": "thought process",
        "tool_calls": []}, "finish_reason": "stop"}]}
    events = list(OpenAIProvider("m", "k")._events_from_json(data))
    assert events[-1]["message"]["reasoning"] == "thought process"


def test_nonstream_reads_reasoning_key_variant():
    # some relays (sensenova) use "reasoning" instead of "reasoning_content"
    data = {"choices": [{"message": {
        "content": "答", "reasoning": "推理过程", "tool_calls": []},
        "finish_reason": "stop"}]}
    events = list(OpenAIProvider("m", "k")._events_from_json(data))
    assert any(e["type"] == "reasoning_delta" and e["text"] == "推理过程"
               for e in events)
    assert events[-1]["message"]["reasoning"] == "推理过程"


def test_reasoning_effort_shim(monkeypatch):
    calls = []

    def fake_post_stream(url, headers, body, timeout=600):
        calls.append(body)
        if len(calls) == 1:
            raise LLMError("HTTP 400: reasoning_effort is not supported by this model")
        return FakeResp([
            b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n',
            b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
        ])

    monkeypatch.setattr(llm, "_post_stream", fake_post_stream)
    p = OpenAIProvider("m", "k", reasoning_effort="high")
    list(p.stream([{"role": "user", "content": "x"}], None, None))
    assert calls[0].get("reasoning_effort") == "high"
    assert "reasoning_effort" not in calls[1]


# ---------- image relay for OpenAI-compatible vision models ----------

def test_tool_image_relayed_as_user_message():
    content = [{"type": "image", "media_type": "image/png", "data": "QUJD"},
               {"type": "text", "text": "screenshot of the app"}]
    msgs = [{"role": "user", "content": "look"},
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "t1", "name": "read_file", "args": "{}"}]},
            {"role": "tool", "tool_call_id": "t1", "name": "read_file",
             "content": content, "is_error": False}]
    wire = llm._to_openai_messages(msgs, None)
    tool_msg, user_msg = wire[-2], wire[-1]
    assert tool_msg["role"] == "tool"
    assert all(p["type"] == "text" for p in tool_msg["content"])  # no image in tool msg
    assert user_msg["role"] == "user"
    img_part = user_msg["content"][1]
    assert img_part["type"] == "image_url"
    assert img_part["image_url"]["url"].startswith("data:image/png;base64,QUJD")


def test_multiple_tool_images_flushed_once():
    img = {"type": "image", "media_type": "image/png", "data": "QUJD"}
    msgs = [{"role": "assistant", "content": None,
             "tool_calls": [{"id": "t1", "name": "r", "args": "{}"},
                            {"id": "t2", "name": "r", "args": "{}"}]},
            {"role": "tool", "tool_call_id": "t1", "name": "r",
             "content": [img, {"type": "text", "text": "one"}], "is_error": False},
            {"role": "tool", "tool_call_id": "t2", "name": "r",
             "content": [img, {"type": "text", "text": "two"}], "is_error": False},
            {"role": "user", "content": "next"}]
    wire = llm._to_openai_messages(msgs, None)
    roles = [m["role"] for m in wire]
    # tool, tool, user(images), user(next) — images grouped into ONE message
    assert roles == ["assistant", "tool", "tool", "user", "user"]
    img_parts = [p for p in wire[3]["content"] if p["type"] == "image_url"]
    assert len(img_parts) == 2
