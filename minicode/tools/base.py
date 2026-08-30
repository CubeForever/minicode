"""Tool base classes, context and registry."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional


class ToolError(Exception):
    """Raised by a tool to report a recoverable failure to the model."""


@dataclass
class ToolContext:
    cwd: Path                       # project working directory
    config: object
    session: object
    ui: object
    agent_factory: Optional[Callable[[str], str]] = None  # for dispatch_agent


class Tool:
    name = "tool"
    description = ""
    kind = "read"                   # read | write | bash | meta — drives confirmation
    input_schema = {"type": "object", "properties": {}, "required": []}

    def schema(self) -> dict:
        return {"name": self.name, "description": self.description,
                "input_schema": self.input_schema}

    def describe_call(self, args: dict) -> str:
        return ""

    def preview(self, args: dict, ctx: ToolContext = None) -> str:
        """Human-readable preview shown before confirmation."""
        return self.describe_call(args)

    def run(self, args: dict, ctx: ToolContext) -> str:
        raise NotImplementedError


class ToolRegistry:
    def __init__(self, tools: List[Tool]):
        self.tools: Dict[str, Tool] = {}
        for t in tools:
            if t.name in self.tools:
                raise ValueError(f"duplicate tool name: {t.name}")
            self.tools[t.name] = t

    def get(self, name: str) -> Optional[Tool]:
        return self.tools.get(name)

    def schemas(self) -> List[dict]:
        return [t.schema() for t in self.tools.values()]

    def names(self) -> List[str]:
        return list(self.tools)


def truncate_middle(text: str, limit: int = 30000) -> str:
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    tail = limit // 3
    return (text[:head]
            + f"\n\n… [{len(text) - limit} chars truncated] …\n\n"
            + text[-tail:])
