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


# ---------------------------------------------------------------------------
# Tool-aware result summarization — smarter than truncate_middle for tools
# whose output has structure (line numbers, file groups, error lines).
# ---------------------------------------------------------------------------

_ERROR_HINTS = ("error", "traceback", "exception", "fatal", "failed",
                "panic", "undefined", "not found", "no such", "拒绝", "错误",
                "失败", "不存在", "权限")


def _summarize_read_file(result: str, limit: int) -> str:
    """read_file output carries line numbers; fold the middle by line count."""
    lines = result.splitlines()
    if len(lines) <= 80:
        return truncate_middle(result, limit)
    head_n = 50
    tail_n = 30
    omitted = len(lines) - head_n - tail_n
    head = "\n".join(lines[:head_n])
    tail = "\n".join(lines[-tail_n:])
    return (f"{head}\n\n… [{omitted} lines folded] …\n\n{tail}")


def _summarize_grep(result: str, limit: int) -> str:
    """grep output is grouped by file; keep first matches per file + totals."""
    lines = result.splitlines()
    if len(lines) <= 100:
        return truncate_middle(result, limit)
    # rg format: "path:line:content" or "path\nline-content"
    from collections import OrderedDict
    per_file: "OrderedDict[str, list]" = OrderedDict()
    for ln in lines:
        if ":" in ln:
            fname = ln.split(":", 1)[0]
        else:
            fname = "(other)"
        per_file.setdefault(fname, []).append(ln)
    out = []
    total_matches = sum(len(v) for v in per_file.values())
    for fname, matches in per_file.items():
        out.append(f"## {fname} ({len(matches)} matches)")
        out.extend(matches[:5])
        if len(matches) > 5:
            out.append(f"  … +{len(matches) - 5} more in this file")
    summary = "\n".join(out)
    if len(summary) > limit:
        summary = truncate_middle(summary, limit)
    return summary + f"\n\n[grep summary: {len(per_file)} files, {total_matches} matches total]"


def _summarize_bash(result: str, limit: int) -> str:
    """bash output: keep head/tail, and always keep lines that look like errors."""
    lines = result.splitlines()
    if len(lines) <= 100:
        return truncate_middle(result, limit)
    head_n = 30
    tail_n = 30
    error_lines = [ln for ln in lines if any(h in ln.lower() for h in _ERROR_HINTS)]
    kept = set(range(head_n)) | set(range(len(lines) - tail_n, len(lines)))
    # include error lines near the kept region
    for i, ln in enumerate(lines):
        if any(h in ln.lower() for h in _ERROR_HINTS):
            kept |= set(range(max(0, i - 1), min(len(lines), i + 2)))
    out = []
    prev = -1
    for i in sorted(kept):
        if i != prev + 1 and prev >= 0:
            out.append(f"… [{i - prev - 1} lines skipped] …")
        out.append(lines[i])
        prev = i
    summary = "\n".join(out)
    if error_lines:
        summary += f"\n\n[bash summary: {len(lines)} total lines, {len(error_lines)} error-like lines kept]"
    return summary[:limit] if len(summary) > limit else summary


def _summarize_listing(result: str, limit: int) -> str:
    """list_dir / glob output: just count entries and show a sample."""
    lines = [ln for ln in result.splitlines() if ln.strip()]
    if len(lines) <= 60:
        return truncate_middle(result, limit)
    sample = "\n".join(lines[:40])
    return (f"{sample}\n\n… [{len(lines) - 40} more entries] …\n"
            f"[listing summary: {len(lines)} entries total]")


# tool name -> summarizer; anything not listed falls back to truncate_middle
_SUMMARY_STRATEGIES = {
    "read_file": _summarize_read_file,
    "grep": _summarize_grep,
    "bash": _summarize_bash,
    "list_dir": _summarize_listing,
    "glob": _summarize_listing,
    "web_fetch": truncate_middle,
    "web_search": truncate_middle,
}


def summarize_result(tool_name: str, result: str, limit: int = 30000) -> str:
    """Summarize an oversized tool result using a tool-aware strategy.

    Tools with structured output (read_file/grep/bash) get intelligent folding;
    everything else uses the generic truncate_middle.
    """
    if not isinstance(result, str) or len(result) <= limit:
        return result
    strategy = _SUMMARY_STRATEGIES.get(tool_name, truncate_middle)
    try:
        return strategy(result, limit)
    except Exception:
        return truncate_middle(result, limit)
