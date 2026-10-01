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


def _require_read(ctx: ToolContext, p: Path, action: str):
    """Hard read-before-edit rule (same as Claude Code): modifying a file that
    was never read in this session is refused — no blind edits from memory."""
    recorded = getattr(ctx.session, "file_mtimes", {}).get(str(p))
    if recorded is None:
        raise ToolError(
            f"Read-before-edit: {action} requires reading the file first. "
            f"Call read_file on '{p.name}' to confirm its current content, then retry.")
    try:
        if abs(os.path.getmtime(p) - recorded) > 0.5:
            raise ToolError(
                "File has been modified since it was last read — re-read it "
                "with read_file, then apply your edit to the current content.")
    except OSError:
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
                   "To change part of an existing file, prefer edit_file. "
                   "Overwriting an existing file requires reading it first "
                   "(read-before-edit is enforced).")
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
        if existed:
            _require_read(ctx, p, "overwrite")   # 覆盖已有文件前必须先读
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
    description = ("String replacement in a file. old_string should match uniquely "
                   "(include enough surrounding context); set replace_all=true to replace "
                   "every occurrence. If the exact text doesn't match, whitespace-flexible "
                   "and fuzzy matching are attempted automatically. Optional line= (1-based, "
                   "from read_file output) disambiguates locations. The file must have been "
                   "read with read_file first (read-before-edit is enforced).")
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path."},
            "old_string": {"type": "string", "description": "Text to replace."},
            "new_string": {"type": "string", "description": "Replacement text ('' deletes)."},
            "replace_all": {"type": "boolean", "description": "Replace all occurrences (default false)."},
            "line": {"type": "integer", "description": "1-based line number where old_string starts (helps disambiguate)."},
        },
        "required": ["path", "old_string", "new_string"],
    }

    def describe_call(self, args: dict) -> str:
        ra = " (replace_all)" if args.get("replace_all") else ""
        ln = f" @L{args['line']}" if args.get("line") else ""
        return f"{args.get('path')}{ra}{ln}"

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
        _require_read(ctx, p, "edit_file")   # 硬性 read-before-edit
        text, newline = _read_text_normalized(p)
        replace_all = bool(args.get("replace_all"))
        line_hint = None
        if args.get("line") not in (None, ""):
            try:
                line_hint = int(args.get("line"))
            except (TypeError, ValueError):
                line_hint = None

        count = text.count(old)
        if count == 1 or (count > 1 and replace_all):
            updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
            n, how = (count, "exact") if replace_all else (1, "exact")
        elif count > 1:
            # 多处命中：line 提示可以消歧（取离提示行最近的命中）
            if line_hint is None:
                raise ToolError(f"old_string matches {count} locations; add more "
                                f"surrounding context, pass replace_all=true, "
                                f"or add line= (1-based from read_file)")
            positions = []
            start = text.find(old)
            while start != -1:
                positions.append((start, text.count("\n", 0, start)))
                start = text.find(old, start + 1)
            start = min(positions, key=lambda t: abs(t[1] - (line_hint - 1)))[0]
            updated = text[:start] + new + text[start + len(old):]
            n, how = 1, "exact (disambiguated by line)"
        else:
            res = _flexible_replace(text, old, new, replace_all, line_hint)
            if res[0] == "ok":
                updated, n, how = res[1], res[2], res[3]
            elif res[0] == "ambiguous":
                lines_show = ", ".join(str(i + 1) for i in res[1][:5])
                raise ToolError(f"old_string matched {len(res[1])} regions loosely "
                                f"(lines {lines_show}); add more surrounding context "
                                f"or pass line= to pick one")
            else:
                file_lines = text.split("\n")
                nearest = _nearest_summary(file_lines, _split_lines_nl(old))
                raise ToolError("old_string not found (whitespace-flexible and fuzzy "
                                f"matching also tried). Closest region(s): {nearest}. "
                                "Re-read the file and copy the exact text including "
                                "whitespace/indentation, or pass line=.")
        try:
            _write_text_nl(p, updated, newline)
        except OSError as e:
            raise ToolError(f"cannot write {p}: {e}")
        _note_read(ctx, p)
        note = "" if how == "exact" else f" [{how}]"
        return f"edited {_rel(ctx, p)}: replaced {n} occurrence(s) of {len(old)} chars{note}"


def _split_lines_nl(s: str) -> List[str]:
    """Split for line-based matching. A single trailing newline doesn't create
    an empty final line (models add/drop it freely)."""
    lines = s.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


_FLEX_PASSES = (
    ("trailing-space", lambda a, b: a.rstrip() == b.rstrip()),
    ("ignoring-whitespace", lambda a, b: "".join(a.split()) == "".join(b.split())),
)


def _pick_hits(hits: List[int], line_hint, replace_all: bool):
    """Disambiguate matching window starts. None = ambiguous (no hint)."""
    if replace_all:
        return hits
    if len(hits) == 1:
        return hits
    if line_hint is not None and hits:
        return [min(hits, key=lambda i: abs(i - (line_hint - 1)))]
    return None


def _fuzzy_candidates(file_lines: List[str], old_lines: List[str],
                      limit: int = 20000) -> List[tuple]:
    """All windows scored by similarity (best first). Uses the classic
    real_quick_ratio → quick_ratio → ratio cascade so big files stay fast;
    hard-capped at `limit` lines."""
    if not old_lines or len(file_lines) > limit:
        return []
    old_text = "\n".join(old_lines)
    n = len(old_lines)
    sm = difflib.SequenceMatcher(None, autojunk=False)
    sm.set_seq2(old_text)
    scored = []
    for i in range(len(file_lines) - n + 1):
        window = "\n".join(file_lines[i:i + n])
        sm.set_seq1(window)
        if sm.real_quick_ratio() < 0.45 or sm.quick_ratio() < 0.6:
            continue
        r = sm.ratio()
        if r >= 0.3:
            scored.append((r, i))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return scored


def _nearest_summary(file_lines: List[str], old_lines: List[str],
                     k: int = 3) -> str:
    cand = _fuzzy_candidates(file_lines, old_lines)
    if not cand:
        return "none"
    return ", ".join(f"line {i + 1} ({int(r * 100)}%)" for r, i in cand[:k])


def _flexible_replace(text: str, old: str, new: str, replace_all: bool,
                      line_hint):
    """Aider-style fallback matching when the exact string is absent.

    Passes: trailing-space-insensitive → all-whitespace-insensitive →
    difflib fuzzy window (≥0.90 similarity, unambiguous). Returns
    ("ok", updated_text, n, how) | ("ambiguous", hit_line_indices) | ("missing", None).
    """
    file_lines = text.split("\n")
    old_lines = _split_lines_nl(old)
    new_lines = _split_lines_nl(new)
    n = len(old_lines)
    if n == 0 or n > len(file_lines):
        return ("missing", None)
    ambiguous: List[int] = []
    for name, eq in _FLEX_PASSES:
        hits = [i for i in range(len(file_lines) - n + 1)
                if all(eq(file_lines[i + j], old_lines[j]) for j in range(n))]
        picked = _pick_hits(hits, line_hint, replace_all)
        if picked:
            out = list(file_lines)
            for i in sorted(picked, reverse=True):
                out[i:i + n] = new_lines
            return ("ok", "\n".join(out), len(picked), name)
        if len(hits) > 1:
            ambiguous = hits
    # fuzzy pass — only when confident and clearly best (or line hint agrees)
    cand = _fuzzy_candidates(file_lines, old_lines)
    if cand:
        best_r, best_i = cand[0]
        chosen = None
        if line_hint is not None and 0 <= line_hint - 1 <= len(file_lines) - n:
            anchored = min(cand, key=lambda t: abs(t[1] - (line_hint - 1)))
            if anchored[0] >= 0.75:
                chosen = anchored
        if chosen is None and best_r >= 0.90 and \
                (len(cand) == 1 or best_r - cand[1][0] >= 0.02):
            chosen = (best_r, best_i)
        if chosen is not None:
            r, i = chosen
            out = list(file_lines)
            out[i:i + n] = new_lines
            return ("ok", "\n".join(out), 1, f"fuzzy match {int(r * 100)}%, line {i + 1}")
    if ambiguous:
        return ("ambiguous", ambiguous)
    return ("missing", None)


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
