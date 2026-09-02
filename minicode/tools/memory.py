"""Project Brain — the agent's self-maintained, cross-session memory.

Stored at ``<project>/.minicode/BRAIN.md``. The model records durable
knowledge (facts / gotchas / decisions / failed approaches) via the
``brain_write`` tool; every later session inherits it through the system
prompt. Users can edit the file freely — it is re-parsed each load.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Tuple

from .base import Tool, ToolContext, ToolError

BRAIN_HEADER = ("# Project Brain\n\n"
                "Auto-maintained by minicode. Durable knowledge that every future "
                "session inherits. Edit freely.\n")
SECTION_BY_KIND = {
    "fact": "Facts",            # how things work / commands / conventions
    "gotcha": "Gotchas",        # pitfalls discovered the hard way
    "decision": "Decisions",    # choices made and why
    "failed": "Failed approaches",  # what was tried and did not work
}
SECTION_ORDER = list(SECTION_BY_KIND.values())
MAX_PER_SECTION = 50


def brain_path(cwd) -> Path:
    return Path(cwd) / ".minicode" / "BRAIN.md"


def load_brain_text(cwd) -> str:
    p = brain_path(cwd)
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _parse(text: str) -> Dict[str, List[str]]:
    sections: Dict[str, List[str]] = {}
    cur = None
    for ln in text.splitlines():
        if ln.startswith("## "):
            cur = ln[3:].strip()
            sections.setdefault(cur, [])
        elif ln.startswith("- ") and cur:
            sections[cur].append(ln[2:].strip())
    return sections


def render_brain(cwd, cap: int = 4000) -> str:
    text = load_brain_text(cwd).strip()
    return text[:cap] if text else ""


def append_brain(cwd, kind: str, content: str) -> Tuple[bool, str]:
    """Append a bullet under the section for `kind`. Returns (added, message)."""
    if kind not in SECTION_BY_KIND:
        raise ToolError(f"kind must be one of: {', '.join(SECTION_BY_KIND)}")
    content = " ".join(str(content).split())
    if not content:
        raise ToolError("content is required")
    sections = _parse(load_brain_text(cwd))
    name = SECTION_BY_KIND[kind]
    items = sections.setdefault(name, [])
    if content in items:
        return False, "already recorded in the brain"
    if len(items) >= MAX_PER_SECTION:
        items.pop(0)  # keep the section bounded; oldest facts rotate out first
    items.append(content)

    out = [BRAIN_HEADER.rstrip(), ""]
    for sec in SECTION_ORDER:
        if sections.get(sec):
            out.append(f"## {sec}")
            out.extend(f"- {i}" for i in sections[sec])
            out.append("")
    # keep any custom sections the user added
    for sec, items_extra in sections.items():
        if sec in SECTION_ORDER or not items_extra:
            continue
        out.append(f"## {sec}")
        out.extend(f"- {i}" for i in items_extra)
        out.append("")
    p = brain_path(cwd)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write("\n".join(out))
    return True, f"recorded under {name}"


class BrainWriteTool(Tool):
    name = "brain_write"
    kind = "meta"
    description = ("Record a durable fact about this project into the project brain "
                   "(.minicode/BRAIN.md) so every future session inherits it. Kinds: "
                   "fact (how it works, commands, conventions), gotcha (pitfall), "
                   "decision (choice + why), failed (approach that did not work). "
                   "Do not record transient details.")
    input_schema = {
        "type": "object",
        "properties": {
            "kind": {"type": "string",
                     "enum": list(SECTION_BY_KIND),
                     "description": "Category of the note."},
            "content": {"type": "string", "description": "One concise sentence."},
        },
        "required": ["kind", "content"],
    }

    def describe_call(self, args: dict) -> str:
        return f"[{args.get('kind')}] {' '.join(str(args.get('content') or '').split())[:80]}"

    def run(self, args: dict, ctx: ToolContext) -> str:
        added, msg = append_brain(ctx.cwd, str(args.get("kind") or ""),
                                  str(args.get("content") or ""))
        return msg if not added else f"{msg} — visible to future sessions"
