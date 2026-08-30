"""Line input with Tab completion when readline is available.

POSIX ships readline in stdlib; on Windows install the optional
``pyreadline3`` package to get completion, otherwise falls back to
plain ``input()`` (which keeps CJK IME input fully intact).
"""
from __future__ import annotations

from pathlib import Path

try:
    import readline  # noqa
    HAS_READLINE = True
except ImportError:
    readline = None
    HAS_READLINE = False


class _Completer:
    def __init__(self, words):
        self.words = words

    def complete(self, text, state):
        options = [w for w in self.words if w.startswith(text)]
        return options[state] if state < len(options) else None


def file_completions(text: str, limit: int = 400):
    """Complete '@' mentions and path-ish words from the working directory."""
    prefix = text.lstrip("@")
    if not prefix:
        return []
    base = Path(".")
    pattern = prefix + "*"
    out = []
    try:
        for p in base.glob(pattern):
            suffix = "/" if p.is_dir() else ""
            out.append("@" + str(p).replace("\\", "/") + suffix)
            if len(out) >= limit:
                break
    except (OSError, ValueError):
        pass
    return out


def read_line(prompt: str, command_names=()) -> str:
    """input() with Tab completion when readline exists (slash commands + @files)."""
    if HAS_READLINE:
        def completer(text, state):
            if text.startswith("/"):
                options = [c for c in command_names if c.startswith(text)]
            elif text.startswith("@") or (" " not in text and len(text) > 1
                                          and ("/" in text or "." in text or text.islower()
                                               and not text.startswith("-"))):
                options = file_completions(text)
            else:
                options = []
            return options[state] if state < len(options) else None

        readline.set_completer(completer)
        readline.set_completer_delims(" \t\n")
        readline.set_history_length(500)
        readline.parse_and_bind("tab: complete")
        try:
            line = input(prompt)
        finally:
            readline.set_completer(None)
        if line.strip():
            try:
                readline.add_history(line)
            except Exception:
                pass
        return line
    return input(prompt)
