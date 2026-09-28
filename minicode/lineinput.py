"""Line input with Tab completion, persistent history and bracketed paste.

POSIX ships readline in stdlib (history persistence, ANSI-safe prompt,
bracketed paste when the underlying readline supports it). On Windows a
self-contained raw line editor (msvcrt) provides the same interaction:
history navigation, slash/@file completion, multiline paste (burst
detection), CJK-aware cursor rendering — zero dependencies.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from .ui import _disp_width as _disp_width  # re-export：winedit 经此引用，与 UI 共用实现

try:
    import readline  # noqa
    HAS_READLINE = True
except ImportError:
    readline = None
    HAS_READLINE = False

HISTORY_FILE = Path.home() / ".minicode" / "history"
_HISTORY_LIMIT = 500

# bracketed paste 是否可用：None=未知（首行试用），False=已检测到不兼容
_BRACKETED_PASTE_OK = None if HAS_READLINE else False

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _load_history_lines() -> list:
    try:
        lines = HISTORY_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
        return [ln for ln in lines if ln.strip()][-_HISTORY_LIMIT:]
    except OSError:
        return []


def _append_history_file(line: str):
    try:
        HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


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


def _wrap_prompt_ansi(prompt: str) -> str:
    """readline 把 ANSI 转义计入列宽导致光标错位；用 \\001/\\002 包裹转义序列。"""
    return _ANSI_RE.sub(lambda m: "\x01" + m.group(0) + "\x02", prompt)


def _completions_for(text: str, command_names) -> list:
    if text.startswith("/"):
        return [c for c in command_names if c.startswith(text)]
    if text.startswith("@") or (" " not in text and len(text) > 1
                                and ("/" in text or "." in text or text.islower()
                                     and not text.startswith("-"))):
        return file_completions(text)
    return []


def read_line(prompt: str, command_names=()) -> str:
    """Read one input line with completion, history and paste support."""
    if HAS_READLINE:
        return _readline_input(prompt, command_names)
    if os.name == "nt":
        from .winedit import windows_read_line
        return windows_read_line(prompt, command_names)
    return input(prompt)


# ---------------- readline 路径（POSIX / pyreadline3） ----------------

def _readline_input(prompt: str, command_names) -> str:
    global _BRACKETED_PASTE_OK
    if readline is not None:
        def completer(text, state):
            options = _completions_for(text, command_names)
            return options[state] if state < len(options) else None

        readline.set_completer(completer)
        readline.set_completer_delims(" \t\n")
        readline.set_history_length(_HISTORY_LIMIT)
        readline.parse_and_bind("tab: complete")
        try:
            readline.read_history_file(str(HISTORY_FILE))
        except (OSError, Exception):
            pass  # 首次运行 / 权限问题 —— 历史仅本次会话可用
        paste_enabled = False
        if _BRACKETED_PASTE_OK is not False and sys.stdout.isatty():
            sys.stdout.write("\x1b[?2004h")   # 开启 bracketed paste
            sys.stdout.flush()
            paste_enabled = True
        show = _wrap_prompt_ansi(prompt) if sys.stdout.isatty() else prompt
        try:
            line = input(show)
        finally:
            if paste_enabled:
                sys.stdout.write("\x1b[?2004l")   # 关闭 bracketed paste
                sys.stdout.flush()
            readline.set_completer(None)
        if paste_enabled and "\x1b[200~" in line:
            # 旧版 readline / libedit 不识别标记 → 标记泄漏为明文，永久关闭
            _BRACKETED_PASTE_OK = False
            line = line.replace("\x1b[200~", "").replace("\x1b[201~", "")
        if line.strip():
            try:
                readline.add_history(line)
                readline.write_history_file(str(HISTORY_FILE))
            except Exception:
                pass
        return line
    return input(prompt)
