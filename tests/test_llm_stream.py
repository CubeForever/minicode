import json

from minicode.llm import (AnthropicProvider, OpenAIProvider,
                          _to_anthropic_messages, _to_openai_messages,
                          assistant_message, tool_message, user_message)


class FakeResp:
    def __init__(self, chunks):
        self.chunks = chunks

    def __iter__(self):
        return iter(self.chunks)

    def close(self):
        pass


def _ev(d):
    return ("data: " + json.dumps(d) + "\n\n").encode()


def test_openai_stream_text_and_tool():
    chunks = [
        b'data: {"choices":[{"delta":{"role":"assistant","content":"He"}}]}\n\n',
        b'data: {"choices":[{"delta":{"content":"llo"}}]}\n\n',
        b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1",'
        b'"function":{"name":"edit_file","arguments":"{\\"old\\":"}}]}}]}\n\n',
        b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
        b'"function":{"arguments":"\\"x\\"}"}}]}}]}\n\n',
        b'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n\n',
        b'data: {"choices":[],"usage":{"prompt_tokens":12,"completion_tokens":7}}\n\n',
        b'data: [DONE]\n\n',
    ]
    p = OpenAIProvider("m", "k")
    events = list(p._iter_stream(FakeResp(chunks)))
    texts = [e["text"] for e in events if e["type"] == "text_delta"]
    assert texts == ["He", "llo"]
    finish = events[-1]
    assert finish["type"] == "finish"
    assert finish["usage"] == {"input": 12, "output": 7}
    msg = finish["message"]
    assert msg["content"] == "Hello"
    assert msg["tool_calls"][0]["id"] == "c1"
    assert msg["tool_calls"][0]["name"] == "edit_file"
    assert msg["tool_calls"][0]["args"] == '{"old":"x"}'


def test_openai_tool_call_without_index():
    chunks = [
        b'data: {"choices":[{"delta":{"tool_calls":[{"id":"c9",'
        b'"function":{"name":"bash","arguments":"{\\"command\\":\\"ls\\"}"}}]},'
        b'"finish_reason":"tool_calls"}]}\n\n',
        b'data: [DONE]\n\n',
    ]
    p = OpenAIProvider("m", "k")
    msg = list(p._iter_stream(FakeResp(chunks)))[-1]["message"]
    assert msg["tool_calls"][0]["name"] == "bash"
    assert msg["tool_calls"][0]["args"] == '{"command":"ls"}'


def test_anthropic_stream_text_and_tool():
    chunks = [
        _ev({"type": "message_start", "message": {"usage": {"input_tokens": 20}}}),
        _ev({"type": "content_block_start", "index": 0, "content_block": {"type": "text"}}),
        _ev({"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": "Run "}}),
        _ev({"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": "it"}}),
        _ev({"type": "content_block_stop", "index": 0}),
        _ev({"type": "content_block_start", "index": 1,
             "content_block": {"type": "tool_use", "id": "tu1", "name": "bash"}}),
        _ev({"type": "content_block_delta", "index": 1,
             "delta": {"type": "input_json_delta", "partial_json": "{\"command\":"}}),
        _ev({"type": "content_block_delta", "index": 1,
             "delta": {"type": "input_json_delta", "partial_json": "\"ls\"}"}}),
        _ev({"type": "content_block_stop", "index": 1}),
        _ev({"type": "message_delta", "delta": {"stop_reason": "tool_use"},
             "usage": {"output_tokens": 9}}),
        _ev({"type": "message_stop"}),
    ]
    p = AnthropicProvider("m", "k")
    events = list(p._iter_stream(FakeResp(chunks)))
    assert [e["text"] for e in events if e["type"] == "text_delta"] == ["Run ", "it"]
    finish = events[-1]
    msg = finish["message"]
    assert msg["content"] == "Run it"
    assert msg["tool_calls"][0] == {"id": "tu1", "name": "bash", "args": '{"command":"ls"}'}
    assert finish["usage"] == {"input": 20, "output": 9}


def test_openai_conversion():
    msgs = [
        user_message("hi"),
        assistant_message(None, [{"id": "c1", "name": "bash", "args": '{"command":"ls"}'}]),
        tool_message("c1", "bash", "file.txt", False),
        user_message("thanks"),
    ]
    wire = _to_openai_messages(msgs, "sys")
    assert wire[0] == {"role": "system", "content": "sys"}
    assert wire[2]["tool_calls"][0]["function"]["arguments"] == '{"command":"ls"}'
    assert wire[3] == {"role": "tool", "tool_call_id": "c1", "content": "file.txt"}


def test_anthropic_conversion_groups_tool_results():
    msgs = [
        user_message("hi"),
        assistant_message("doing", [{"id": "a", "name": "bash", "args": "{}"},
                                    {"id": "b", "name": "bash", "args": "{}"}]),
        tool_message("a", "bash", "boom", True),
        tool_message("b", "bash", "ok2", False),
    ]
    wire = _to_anthropic_messages(msgs)
    assert [m["role"] for m in wire] == ["user", "assistant", "user"]
    tool_blocks = wire[2]["content"]
    assert len(tool_blocks) == 2
    assert tool_blocks[0]["is_error"] is True
    assert tool_blocks[1]["tool_use_id"] == "b"
    assert wire[1]["content"][0] == {"type": "text", "text": "doing"}
    assert wire[1]["content"][1]["type"] == "tool_use"
