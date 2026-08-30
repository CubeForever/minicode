"""Scripted provider for offline demos and testing (MINICODE_FAKE_LLM=demo|file.json)."""
from __future__ import annotations

import json
from typing import Iterator, List

from .llm import Provider, assistant_message


def _chunks(text: str, n: int = 24) -> Iterator[str]:
    for i in range(0, len(text), n):
        yield text[i:i + n]


class FakeProvider(Provider):
    """Plays back a scripted list of steps. Each step:
    {"text": "...", "tool_calls": [{"id","name","args"}]}"""

    name = "fake"

    def __init__(self, script: List[dict], model: str = "fake"):
        super().__init__(model, api_key="fake")
        self.script = script or []
        self.i = 0

    @classmethod
    def from_file(cls, path: str) -> "FakeProvider":
        with open(path, "r", encoding="utf-8") as f:
            return cls(json.load(f))

    @classmethod
    def demo(cls) -> "FakeProvider":
        return cls([
            {"text": "好的，我先看一下当前目录，然后创建 hello.txt。",
             "tool_calls": [
                 {"id": "call_1", "name": "list_dir", "args": "{}"},
                 {"id": "call_2", "name": "write_file",
                  "args": json.dumps({"path": "hello.txt",
                                      "content": "Hello from minicode!\n"})},
             ]},
            {"text": "已创建 hello.txt，里面写了一句问候语。"},
        ])

    def stream(self, messages, tools, system, thinking: int = 0) -> Iterator[dict]:
        if self.i >= len(self.script):
            step = {"text": "(fake provider: script exhausted)"}
        else:
            step = self.script[self.i]
            self.i += 1
        text = step.get("text") or ""
        tcs = step.get("tool_calls") or []
        for ch in _chunks(text):
            yield {"type": "text_delta", "text": ch}
        for k, tc in enumerate(tcs):
            yield {"type": "tool_call", "index": k,
                   "id": tc.get("id", f"call_{k}"), "name": tc["name"]}
            args = tc.get("args") or "{}"
            yield {"type": "tool_call_delta", "index": k, "args_delta": args}
        yield {"type": "finish", "stop_reason": "tool_calls" if tcs else "end_turn",
               "usage": {"input": 100 + 50 * self.i, "output": 40},
               "message": assistant_message(text or None, tcs)}
