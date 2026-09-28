"""apply_patch tool (inspired by Codex): apply a multi-file, multi-hunk patch
in a single tool call. Format (simplified Codex V4A):

    *** Begin Patch
    *** Add File: path/new.py
    +full content
    *** Update File: path/old.py
   @@ optional hunk header (ignored)
     context line (unchanged, prefix a single space)
    -removed line
    +added line
    *** Delete File: path/gone.py
    *** End Patch

The whole patch is parsed first; any error aborts before touching files.
"""
from __future__ import annotations

import difflib

from .base import Tool, ToolContext, ToolError
from .fs import (_note_read, _require_read, _resolve, _read_text_normalized,
                 _write_text_nl)


def _parse_patch(patch: str) -> list:
    lines = patch.strip("\n").splitlines()
    if not lines or lines[0].strip() != "*** Begin Patch":
        raise ToolError("patch must start with '*** Begin Patch'")

    def flush(op):
        if op is not None and op["action"] == "update" and (op["old"] or op["new"]):
            op["hunks"].append((op["old"], op["new"]))
            op["old"], op["new"] = [], []

    ops = []
    cur = None
    for raw in lines[1:]:
        stripped = raw.strip()
        if stripped == "*** End Patch":
            break
        if stripped.startswith("*** Add File:"):
            flush(cur)
            cur = {"action": "add", "path": stripped[len("*** Add File:"):].strip(),
                   "lines": []}
            ops.append(cur)
        elif stripped.startswith("*** Delete File:"):
            flush(cur)
            cur = {"action": "delete", "path": stripped[len("*** Delete File:"):].strip()}
            ops.append(cur)
        elif stripped.startswith("*** Update File:"):
            flush(cur)
            cur = {"action": "update", "path": stripped[len("*** Update File:"):].strip(),
                   "hunks": [], "old": [], "new": []}
            ops.append(cur)
        elif stripped.startswith("*** Move to:"):
            raise ToolError("Move to is not supported; use Delete + Add")
        elif stripped == "*** Begin Patch":
            continue
        elif cur is None:
            raise ToolError(f"unexpected patch line: {raw!r}")
        elif cur["action"] == "add":
            if not raw.startswith("+"):
                raise ToolError(f"Add File lines must start with '+': {raw!r}")
            cur["lines"].append(raw[1:])
        elif cur["action"] == "update":
            if stripped.startswith("@@"):
                flush(cur)
                continue
            if raw.startswith("+"):
                cur["new"].append(raw[1:])
            elif raw.startswith("-"):
                cur["old"].append(raw[1:])
            elif raw.startswith(" ") or raw == "":
                cur["old"].append(raw[1:] if raw else "")
                cur["new"].append(raw[1:] if raw else "")
            else:
                raise ToolError(
                    f"Update File lines must start with ' ', '+' or '-': {raw!r}")
        else:
            raise ToolError(f"unexpected line after {cur['action']}: {raw!r}")
    flush(cur)
    if not ops:
        raise ToolError("empty patch")
    return ops


def _apply_update(text: str, hunks: list, path: str) -> str:
    for old_lines, new_lines in hunks:
        old_block = "\n".join(old_lines)
        new_block = "\n".join(new_lines)
        if old_block == "":
            raise ToolError(
                f"{path}: an update hunk needs context or '-' lines "
                "(use Add File for brand-new content)")
        count = text.count(old_block)
        if count == 0:
            hint = "\n".join(old_lines[:3])
            raise ToolError(f"{path}: patch context not found:\n{hint}\n"
                            "Re-read the file and copy exact lines.")
        if count > 1:
            raise ToolError(f"{path}: patch context matches {count} locations — "
                            "add more surrounding context lines")
        text = text.replace(old_block, new_block, 1)
    return text


class ApplyPatchTool(Tool):
    name = "apply_patch"
    kind = "write"
    description = ("Apply a multi-file, multi-hunk patch in one call (preferred for "
                   "refactors touching several files or several spots in one file). "
                   "Format: '*** Begin Patch' ... sections '*** Add File: <path>' "
                   "('+' lines), '*** Update File: <path>' with hunks of ' ' context / "
                   "'-' removed / '+' added lines, '*** Delete File: <path>', "
                   "'*** End Patch'. Update hunks must match exactly and uniquely; "
                   "Update/Delete sections require reading the target with read_file "
                   "first (read-before-edit is enforced).")
    input_schema = {
        "type": "object",
        "properties": {
            "patch": {"type": "string", "description": "The full patch text."},
        },
        "required": ["patch"],
    }

    def describe_call(self, args: dict) -> str:
        try:
            ops = _parse_patch(str(args.get("patch") or ""))
            return ", ".join(f"{o['action']}:{o['path']}" for o in ops)[:100]
        except ToolError:
            return "(patch preview failed)"

    def preview(self, args: dict, ctx: ToolContext = None) -> str:
        patch = str(args.get("patch") or "")
        plines = patch.splitlines()
        shown = plines[:60]
        more = f"\n… (+{len(plines) - 60} lines)" if len(plines) > 60 else ""
        return "apply_patch:\n" + "\n".join(shown) + more

    def mutated_paths(self, args: dict, ctx: ToolContext):
        try:
            ops = _parse_patch(str(args.get("patch") or ""))
        except ToolError:
            return []
        return [_resolve(ctx, o["path"]) for o in ops]

    def run(self, args: dict, ctx: ToolContext) -> str:
        ops = _parse_patch(str(args.get("patch") or ""))
        resolved = [(_resolve(ctx, o["path"]), o) for o in ops]

        # validate first: no partial application on failure
        seen_paths = set()
        for p, o in resolved:
            if p in seen_paths:
                raise ToolError(
                    f"{o['path']}: duplicate file section in one patch — combine all "
                    "changes under a single Update File section with multiple hunks")
            seen_paths.add(p)
            if o["action"] == "add" and p.exists():
                raise ToolError(f"{o['path']}: already exists (use Update File)")
            if o["action"] in ("update", "delete") and not p.exists():
                raise ToolError(f"{o['path']}: not found")
            if o["action"] == "update":   # 硬性 read-before-edit（与 edit_file 一致）
                _require_read(ctx, p, "apply_patch update")
            elif o["action"] == "delete":
                _require_read(ctx, p, "apply_patch delete")

        # pre-read every updated file so all hunks validate against originals
        # (CRLF normalized for matching; original style restored on write)
        originals = {}
        newlines = {}
        for p, o in resolved:
            if o["action"] == "update":
                originals[p], newlines[p] = _read_text_normalized(p)
                _apply_update(originals[p], o["hunks"], o["path"])

        summary = []
        for p, o in resolved:
            if o["action"] == "add":
                p.parent.mkdir(parents=True, exist_ok=True)
                _write_text_nl(p, "\n".join(o["lines"]) + ("\n" if o["lines"] else ""))
                _note_read(ctx, p)
                summary.append(f"added {o['path']} ({len(o['lines'])} lines)")
            elif o["action"] == "delete":
                p.unlink()
                try:
                    ctx.session.file_mtimes.pop(str(p), None)
                except (AttributeError, KeyError):
                    pass
                summary.append(f"deleted {o['path']}")
            else:
                updated = _apply_update(originals[p], o["hunks"], o["path"])
                _write_text_nl(p, updated, newlines.get(p, "\n"))
                _note_read(ctx, p)
                added = sum(len(n) - len(ol) for ol, n in o["hunks"])
                diff = list(difflib.unified_diff(originals[p].splitlines(),
                                                 updated.splitlines(), lineterm=""))
                summary.append(f"updated {o['path']} ({len(o['hunks'])} hunk(s), "
                               f"delta {added:+d} lines, diff {max(0, len(diff) - 2)} lines)")
        return "\n".join(summary)
