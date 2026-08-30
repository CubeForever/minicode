"""Local Python plugin tools: drop a .py file into .minicode/tools/ (or
~/.minicode/tools/) and it becomes a first-class tool.

Plugin contract (module level):
    TOOL = {
        "name": "my_tool",              # optional, defaults to filename
        "description": "...",           # shown to the model
        "input_schema": {...},          # JSON schema, optional
        "kind": "meta",                 # read|write|bash|meta (default meta)
    }
    def run(args: dict, ctx) -> str: ...
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import List, Tuple

from .tools.base import Tool


class PluginTool(Tool):
    def __init__(self, module, path: Path):
        meta = getattr(module, "TOOL", {}) or {}
        self.name = str(meta.get("name") or Path(path).stem)
        self.description = str(meta.get("description")
                               or f"Local plugin tool from {path.name}.")
        self.input_schema = meta.get("input_schema") or {"type": "object",
                                                         "properties": {}}
        self.kind = meta.get("kind", "meta")
        if self.kind not in ("read", "write", "bash", "meta"):
            self.kind = "meta"
        self._module = module
        self._src = str(path)

    def describe_call(self, args: dict) -> str:
        return json.dumps(args, ensure_ascii=False)[:90]

    def run(self, args: dict, ctx) -> str:
        return str(self._module.run(args, ctx) or "(no output)")


def load_plugin_tools(cwd) -> Tuple[List[Tool], List[Tuple[str, str]]]:
    """Returns (tools, errors) where errors = [(filename, message)]."""
    tools: List[Tool] = []
    errors: List[Tuple[str, str]] = []
    seen = set()
    dirs = [Path(cwd) / ".minicode" / "tools", Path.home() / ".minicode" / "tools"]
    for d in dirs:
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.py")):
            if f.stem.startswith("_"):
                continue
            try:
                spec = importlib.util.spec_from_file_location(
                    f"minicode_plugin_{f.stem}_{abs(hash(str(f)))}", f)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                tool = PluginTool(mod, f)
                if tool.name in seen or any(t.name == tool.name for t in tools):
                    errors.append((f.name, f"工具名冲突：{tool.name}"))
                    continue
                seen.add(tool.name)
                tools.append(tool)
            except Exception as e:  # a broken plugin must not break startup
                errors.append((f.name, f"{type(e).__name__}: {e}"))
    return tools, errors
