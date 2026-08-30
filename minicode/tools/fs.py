"""File tools: read_file, write_file, edit_file, glob, grep, list_dir."""
from __future__ import annotations

import base64
import difflib
import fnmatch
import os
import re
import shutil
from pathlib import Path
from typing import List, Union

from .base import Tool, ToolContext, ToolError

IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".gif": "image/gif", ".webp": "image/webp"}

IGNORED_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".eggs",
    "dist", "build", ".idea", ".vscode", ".next", ".turbo", "coverage",
}


def _resolve(ctx: ToolContext, path_str: str) -> Path:
    p = Path(path_str).expanduser()
    if not p.is_absolute():
        p = ctx.cwd / p
    return p


def _rel(ctx: ToolContext, p: Path) -> str:
    try:
        return str(p.relative_to(ctx.cwd)).replace("\\", "/")
    except ValueError:
        return str(p)


def _read_text(p: Path) -> str:
    data = p.read_bytes()
    if b"\x00" in data[:8192]:
        raise ToolError(f"binary file: {p.name} (cannot display)")
    return data.decode("utf-8", errors="replace")


def _read_text_normalized(p: Path) -> tuple:
    """Read text and normalize CRLF -> LF for matching. Returns
    (text, newline_style) so writes can restore the original style.
    Windows CRLF files would otherwise never match edit anchors."""
    text = _read_text(p)
    crlf = text.count("\r\n")
    cr = text.count("\r") - crlf
    if crlf >= cr and crlf > 0:
        return text.replace("\r\n", "\n"), "\r\n"
    if cr > crlf and cr > 0:  # ancient CR-only files
        return text.replace("\r", "\n"), "\r\n"
    return text, "\n"


def _write_text_nl(p: Path, text: str, newline: str = "\n"):
    if newline != "\n":
        text = text.replace("\n", newline)
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def _write_text(p: Path, text: str):
    """Write with LF endings (JSON/machine files)."""
    _write_text_nl(p, text, "\n")


def _dominant_newline(text: str) -> str:
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    return "\r\n" if crlf > lf else "\n"


def _note_read(ctx: ToolContext, p: Path):
    """Record mtime so edit_file can reject stale edits (file changed since read)."""
    try:
        ctx.session.file_mtimes[str(p)] = os.path.getmtime(p)
    except (OSError, AttributeError):
        pass


class ReadFileTool(Tool):
    name = "read_file"
    kind = "read"
    description = ("Read a text file. Returns numbered lines (cat -n style). "
                   "Large files are paginated; use offset/limit to read more.")
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path (relative to cwd or absolute)."},
            "offset": {"type": "integer", "description": "1-based start line (default 1)."},
            "limit": {"type": "integer", "description": "Max lines to return (default 2000, max 2000)."},
        },
        "required": ["path"],
    }

    def describe_call(self, args: dict) -> str:
        return str(args.get("path") or "")

    def run(self, args: dict, ctx: ToolContext) -> Union[str, dict]:
        p = _resolve(ctx, str(args.get("path") or ""))
        if not p.exists():
            raise ToolError(f"file not found: {_rel(ctx, p)}")
        if p.is_dir():
            raise ToolError(f"{_rel(ctx, p)} is a directory (use list_dir)")
        media = IMAGE_TYPES.get(p.suffix.lower())
        if media:  # image → return as content block for vision models
            data = p.read_bytes()
            b64 = base64.b64encode(data).decode("ascii")
            _note_read(ctx, p)
            return {"_blocks": [
                {"type": "image", "media_type": media, "data": b64},
                {"type": "text", "text": f"Image file {_rel(ctx, p)} "
                                         f"({len(data)} bytes, {media})."},
            ]}
        lines = _read_text(p).splitlines()
        _note_read(ctx, p)
        total = len(lines)
        if total == 0:
            return "(empty file)"
        try:
            offset = max(1, int(args.get("offset") or 1))
        except (TypeError, ValueError):
            offset = 1
        try:
            limit = min(2000, max(1, int(args.get("limit") or 2000)))
        except (TypeError, ValueError):
            limit = 2000
        sel = lines[offset - 1: offset - 1 + limit]
        if not sel:
            return f"(offset {offset} is beyond end of file; total {total} lines)"
        out = []
        for i, line in enumerate(sel, start=offset):
            if len(line) > 2000:
                line = line[:2000] + " …[line truncated]"
            out.append(f"{i:>6}\t{line}")
        note = ""
        if offset > 1 or offset - 1 + len(sel) < total:
            note = (f"\n\n[showing lines {offset}-{offset + len(sel) - 1} of {total}; "
                    f"adjust offset/limit to see more]")
        return "\n".join(out) + note


class WriteFileTool(Tool):
    name = "write_file"
    kind = "write"
    description = ("Create a new file or completely overwrite an existing one. "
                   "To change part of an existing file, prefer edit_file.")
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path."},
            "content": {"type": "string", "description": "Full file content."},
        },
        "required": ["path", "content"],
    }

    def describe_call(self, args: dict) -> str:
        return str(args.get("path") or "")

    def mutated_path(self, args: dict, ctx: ToolContext):
        return _resolve(ctx, str(args.get("path") or ""))

    def preview(self, args: dict, ctx: ToolContext = None) -> str:
        p = _resolve(ctx, str(args.get("path") or ""))
        content = str(args.get("content") or "")
        if p.exists():
            try:
                old = _read_text(p)
            except ToolError:
                return f"Overwrite {_rel(ctx, p)} ({len(content)} chars)"
            diff = list(difflib.unified_diff(old.splitlines(), content.splitlines(),
                                             fromfile="old", tofile="new", lineterm=""))
            shown = diff[:80]
            more = f"\n… (+{len(diff) - 80} diff lines)" if len(diff) > 80 else ""
            return f"Overwrite {_rel(ctx, p)}:\n" + "\n".join(shown) + more
        lines = content.splitlines() or [""]
        head = "\n".join(lines[:30])
        more = f"\n… (+{len(lines) - 30} lines)" if len(lines) > 30 else ""
        return f"Create {_rel(ctx, p)} ({len(lines)} lines):\n{head}{more}"

    def run(self, args: dict, ctx: ToolContext) -> str:
        p = _resolve(ctx, str(args.get("path") or ""))
        content = str(args.get("content") or "")
        existed = p.exists()
        newline = _dominant_newline(_read_text(p)) if existed else "\n"
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            _write_text_nl(p, content, newline)
        except OSError as e:
            raise ToolError(f"cannot write {p}: {e}")
        _note_read(ctx, p)
        tag = "overwrote" if existed else "created"
        return f"{tag} {_rel(ctx, p)} ({len(content)} chars, {len(content.splitlines())} lines)"


class EditFileTool(Tool):
    name = "edit_file"
    kind = "write"
    description = ("Exact string replacement in a file. old_string must match uniquely "
                   "(include enough surrounding context); set replace_all=true to replace "
                   "every occurrence.")
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path."},
            "old_string": {"type": "string", "description": "Exact text to replace."},
            "new_string": {"type": "string", "description": "Replacement text ('' deletes)."},
            "replace_all": {"type": "boolean", "description": "Replace all occurrences (default false)."},
        },
        "required": ["path", "old_string", "new_string"],
    }

    def describe_call(self, args: dict) -> str:
        ra = " (replace_all)" if args.get("replace_all") else ""
        return f"{args.get('path')}{ra}"

    def mutated_path(self, args: dict, ctx: ToolContext):
        return _resolve(ctx, str(args.get("path") or ""))

    def preview(self, args: dict, ctx: ToolContext = None) -> str:
        p = _resolve(ctx, str(args.get("path") or ""))
        old = str(args.get("old_string") or "")
        new = str(args.get("new_string") or "")
        ol = old.splitlines() or ["(empty)"]
        nl = new.splitlines() or ["(empty)"]
        lines = (["- " + x for x in ol[:20]]
                  + ["+ " + x for x in nl[:20]])
        if len(ol) > 20:
            lines.append(f"- … ({len(ol) - 20} more)")
        if len(nl) > 20:
            lines.append(f"+ … ({len(nl) - 20} more)")
        return f"Edit {_rel(ctx, p)}:\n" + "\n".join(lines)

    def run(self, args: dict, ctx: ToolContext) -> str:
        p = _resolve(ctx, str(args.get("path") or ""))
        if not p.exists():
            raise ToolError(f"file not found: {_rel(ctx, p)}")
        old = str(args.get("old_string") or "")
        new = str(args.get("new_string") or "")
        if not old:
            raise ToolError("old_string is empty (use write_file to create files)")
        if old == new:
            raise ToolError("old_string and new_string are identical")
        text, newline = _read_text_normalized(p)
        recorded = getattr(ctx.session, "file_mtimes", {}).get(str(p))
        if recorded is not None:
            try:
                if abs(os.path.getmtime(p) - recorded) > 0.5:
                    raise ToolError(
                        "File has been modified since it was last read — re-read it "
                        "with read_file, then apply your edit to the current content.")
            except OSError:
                pass
        count = text.count(old)
        if count == 0:
            raise ToolError("old_string not found — re-read the file and copy the exact "
                            "text including whitespace/indentation")
        replace_all = bool(args.get("replace_all"))
        if count > 1 and not replace_all:
            raise ToolError(f"old_string matches {count} locations; add more surrounding "
                            f"context or pass replace_all=true")
        updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
        try:
            _write_text_nl(p, updated, newline)
        except OSError as e:
            raise ToolError(f"cannot write {p}: {e}")
        _note_read(ctx, p)
        n = count if replace_all else 1
        return f"edited {_rel(ctx, p)}: replaced {n} occurrence(s) of {len(old)} chars"


def _expand_braces(pattern: str) -> List[str]:
    """Expand one-level brace alternation: '**/*.{py,md}' -> ['**/*.py', '**/*.md'].
    Path.glob does not support braces, but models write them naturally."""
    m = re.search(r"\{([^{}]+)\}", pattern)
    if not m:
        return [pattern]
    out = []
    for alt in m.group(1).split(","):
        out.extend(_expand_braces(pattern[:m.start()] + alt.strip() + pattern[m.end():]))
    return out


class GlobTool(Tool):
    name = "glob"
    kind = "read"
    description = ("Find files by wildcard pattern (e.g. \"src/**/*.py\", braces like "
                   "\"**/*.{py,md}\" are supported). Results sorted by modification time, "
                   "newest first.")
    input_schema = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Glob pattern, supports **."},
            "path": {"type": "string", "description": "Directory to search in (default '.')."},
        },
        "required": ["pattern"],
    }

    def describe_call(self, args: dict) -> str:
        return f"{args.get('pattern')} in {args.get('path') or '.'}"

    def run(self, args: dict, ctx: ToolContext) -> str:
        pattern = str(args.get("pattern") or "")
        if not pattern:
            raise ToolError("pattern is required")
        root = _resolve(ctx, str(args.get("path") or "."))
        if not root.exists():
            raise ToolError(f"path not found: {_rel(ctx, root)}")
        if not root.is_dir():
            raise ToolError("path must be a directory")
        matches = []
        seen = set()
        for pat in _expand_braces(pattern)[:20]:
            for p in root.glob(pat):
                if str(p) in seen:
                    continue
                seen.add(str(p))
                try:
                    rel = p.relative_to(ctx.cwd)
                except ValueError:
                    rel = p
                if set(rel.parts[:-1]) & IGNORED_DIRS:
                    continue
                if p.is_file():
                    matches.append((_mtime(p), p))
        matches.sort(key=lambda t: -t[0])
        if not matches:
            return f"no files match {pattern!r} under {_rel(ctx, root)}"
        shown = [_rel(ctx, p) for _, p in matches[:100]]
        note = f"\n({len(matches)} files total" + (", showing 100" if len(matches) > 100 else "") + ")"
        return "\n".join(shown) + note


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


class GrepTool(Tool):
    name = "grep"
    kind = "read"
    description = ("Search file contents with a regular expression. "
                   "Skips binary files and common junk directories (.git, node_modules, ...).")
    input_schema = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regular expression."},
            "path": {"type": "string", "description": "File or directory (default '.')."},
            "include": {"type": "string", "description": "Only search files matching this glob, e.g. \"*.py\"."},
            "case_insensitive": {"type": "boolean", "description": "Ignore case (default false)."},
        },
        "required": ["pattern"],
    }

    def describe_call(self, args: dict) -> str:
        inc = f" include={args['include']}" if args.get("include") else ""
        return f"/{args.get('pattern')}/{inc}"

    def run(self, args: dict, ctx: ToolContext) -> str:
        pattern = str(args.get("pattern") or "")
        if not pattern:
            raise ToolError("pattern is required")
        try:
            rx = re.compile(pattern, re.IGNORECASE if args.get("case_insensitive") else 0)
        except re.error as e:
            raise ToolError(f"invalid regex: {e}")
        root = _resolve(ctx, str(args.get("path") or "."))
        include = args.get("include")
        # fast path: ripgrep when available
        rg = shutil.which("rg")
        if rg is not None and root.exists():
            out = self._run_ripgrep(rg, pattern, root, include,
                                    bool(args.get("case_insensitive")))
            if out is not None:
                return out
        if root.is_file():
            files = [root]
        elif root.is_dir():
            files = self._walk(root)
        else:
            raise ToolError(f"path not found: {_rel(ctx, root)}")
        results: List[str] = []
        matched_files = set()
        for f in files:
            if include and not fnmatch.fnmatch(f.name, str(include)):
                continue
            try:
                data = f.read_bytes()
            except OSError:
                continue
            if b"\x00" in data[:1024]:
                continue
            text = data.decode("utf-8", errors="replace")
            for ln, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    matched_files.add(_rel(ctx, f))
                    results.append(f"{_rel(ctx, f)}:{ln}: {line.strip()[:400]}")
                    if len(results) >= 200:
                        return ("\n".join(results)
                                + "\n(… results truncated at 200 matches)")
        if not results:
            return f"no matches for {pattern!r}" + (f" in {include}" if include else "")
        return (f"{len(results)} match(es) in {len(matched_files)} file(s):\n"
                + "\n".join(results))

    @staticmethod
    def _run_ripgrep(rg: str, pattern: str, root: Path, include, ci: bool):
        """Run ripgrep (from inside root so paths come out relative); returns
        formatted results or None to fall back to Python."""
        import subprocess
        argv = [rg, "--no-heading", "--line-number", "--max-columns", "400",
                "--max-count", "50"]
        for d in sorted(IGNORED_DIRS):
            argv += ["--glob", f"!{d}/**"]
        if ci:
            argv.append("-i")
        if include:
            argv += ["--glob", str(include)]
        argv += ["-e", pattern, "."]
        try:
            proc = subprocess.run(argv, capture_output=True, timeout=60, cwd=str(root))
        except (OSError, subprocess.TimeoutExpired):
            return None
        if proc.returncode not in (0, 1):  # 1 = no matches, 2 = error
            return None
        out = proc.stdout.decode("utf-8", errors="replace").replace("\\", "/")
        out = re.sub(r"(?m)^\./", "", out)  # rg prints ./app.py when searching "."
        if proc.returncode == 1 or not out.strip():
            return f"no matches for {pattern!r}" + (f" in {include}" if include else "")
        lines = out.rstrip("\n").splitlines()
        if len(lines) >= 200:
            lines = lines[:200] + ["(… results truncated at 200 matches)"]
        return f"{len(lines) if len(lines) < 200 else '200+'} match(es) (ripgrep):\n" \
               + "\n".join(lines)

    @staticmethod
    def _walk(root: Path) -> List[Path]:
        out = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS]
            for name in filenames:
                out.append(Path(dirpath) / name)
        return out


class ListDirTool(Tool):
    name = "list_dir"
    kind = "read"
    description = "List a directory tree (depth-limited). Junk directories are skipped."
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Directory (default '.')."},
            "depth": {"type": "integer", "description": "Depth levels 1-5 (default 2)."},
        },
    }

    def describe_call(self, args: dict) -> str:
        return f"{args.get('path') or '.'} depth={args.get('depth') or 2}"

    def run(self, args: dict, ctx: ToolContext) -> str:
        root = _resolve(ctx, str(args.get("path") or "."))
        if not root.is_dir():
            raise ToolError(f"not a directory: {_rel(ctx, root)}")
        try:
            depth = min(5, max(1, int(args.get("depth") or 2)))
        except (TypeError, ValueError):
            depth = 2
        lines: List[str] = []
        budget = [400]
        self._walk(root, "", depth, lines, budget)
        return f"{_rel(ctx, root) or '.'}\n" + "\n".join(lines)

    def _walk(self, d: Path, prefix: str, depth: int, lines: List[str], budget: list):
        try:
            entries = sorted(d.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        except OSError:
            return
        for e in entries:
            if budget[0] <= 0:
                lines.append(prefix + "…")
                return
            lines.append(prefix + e.name + ("/" if e.is_dir() else ""))
            budget[0] -= 1
            if e.is_dir() and depth > 1 and e.name not in IGNORED_DIRS:
                self._walk(e, prefix + "  ", depth - 1, lines, budget)
