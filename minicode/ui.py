"""Terminal UI: ANSI colors, streaming output, spinner, confirmations."""
from __future__ import annotations

import itertools
import os
import sys
import threading
import time

if os.name == "nt":
    os.system("")  # enable ANSI escape processing on legacy Windows consoles

USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")

MODE_LABELS = {
    "default": "确认模式（写文件/命令需确认）",
    "accept-edits": "自动接受文件编辑",
    "plan": "计划模式（只读调研 → 出计划）",
    "full-access": "完全访问（自动执行 · 高危需你确认）",
    "yolo": "完全访问（自动执行 · 高危需你确认）",
}


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        v = n / 1_000_000
        return f"{v:.1f}M".replace(".0M", "M")
    if n >= 10_000:
        return f"{n / 1000:.0f}k"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def c(text: str, code: str) -> str:
    if not USE_COLOR:
        return text
    return f"\x1b[{code}m{text}\x1b[0m"


def bold(t): return c(t, "1")
def dim(t): return c(t, "2")
def red(t): return c(t, "31")
def green(t): return c(t, "32")
def yellow(t): return c(t, "33")
def cyan(t): return c(t, "36")
def gray(t): return c(t, "90")


# 主线程 input()（提示符/confirm/choose）期间置位：键盘监听线程暂停读键，
# 把按键完整让给行编辑器。回合 watcher 与行编辑器通过它互斥访问 stdin。
INPUT_ACTIVE = threading.Event()
_WRITE_LOCK = threading.RLock()


class stdin_claim:
    """标记主线程即将独占 stdin。用法：with stdin_claim(): input(...)"""

    def __enter__(self):
        INPUT_ACTIVE.set()
        return self

    def __exit__(self, *exc):
        INPUT_ACTIVE.clear()
        return False


class Spinner:
    """Animated spinner on the current line; safe to start/stop repeatedly."""

    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, label: str):
        self.label = label
        self._stop_event = None
        self._thread = None

    def start(self):
        if self._thread is not None:
            return
        if not (sys.stdout.isatty() and USE_COLOR):
            return
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def stop(self):
        if self._thread is None:
            return
        self._stop_event.set()
        self._thread.join(timeout=0.5)
        self._thread = None
        sys.stdout.write("\r\x1b[2K")
        sys.stdout.flush()

    def _spin(self):
        for frame in itertools.cycle(self.FRAMES):
            if self._stop_event.is_set():
                break
            sys.stdout.write("\r" + gray(f"{frame} {self.label}…"))
            sys.stdout.flush()
            time.sleep(0.08)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()
        return False


class UI:
    """Interactive terminal UI. Subagents use SubUI."""

    def __init__(self):
        self._need_newline = False

    # -- low level --
    def _write(self, s: str):
        with _WRITE_LOCK:
            sys.stdout.write(s)
            sys.stdout.flush()

    def newline(self):
        if self._need_newline:
            self._write("\n")
            self._need_newline = False

    # -- streaming --
    def stream_text(self, text: str):
        self._write(text)
        self._need_newline = not text.endswith("\n")

    def stream_reasoning(self, text: str):
        self._write(dim(text))
        self._need_newline = not text.endswith("\n")

    # -- lines --
    def tool_line(self, name: str, summary: str = "", prefix: str = "  ● "):
        self.newline()
        line = cyan(f"{prefix}{name}")
        if summary:
            line += " " + gray(" ".join(str(summary).split())[:110])
        self._write(line + "\n")
        self._need_newline = False

    def tool_result_note(self, text: str):
        lines = [ln for ln in str(text).splitlines() if ln.strip()]
        if not lines:
            return
        extra = f" (+{len(lines) - 1} lines)" if len(lines) > 1 else ""
        self._write(gray(f"  ⎿ {lines[0][:130]}{extra}") + "\n")
        self._need_newline = False

    def info(self, msg: str):
        self.newline()
        self._write(cyan("ℹ " + msg) + "\n")
        self._need_newline = False

    def warn(self, msg: str):
        self.newline()
        self._write(yellow("⚠ " + msg) + "\n")
        self._need_newline = False

    def error(self, msg: str):
        self.newline()
        self._write(red("✗ " + msg) + "\n")
        self._need_newline = False

    def plain(self, msg: str = ""):
        self._write(msg + "\n")
        self._need_newline = False

    def banner(self, version: str, cfg, cwd):
        mode = MODE_LABELS.get(cfg.mode, cfg.mode)
        self.plain()
        self._write(cyan("  ╭─ ") + bold("minicode") + gray(f" v{version}") +
                    cyan(" ────────────────────────────") + "\n")
        self._write(cyan("  │") + gray("  终端编码智能体 · 零依赖 · 多模型适配") + "\n")
        self._write(cyan("  │") + f"  {bold(cfg.model)}" + gray(f"  ·  {cfg.provider}"))
        self._write("\n" + cyan("  │") + f"  {mode}")
        self._write("\n" + cyan("  │") + gray(f"  {cwd}"))
        self._write("\n" + cyan("  ╰─ ") + gray("/help 命令 · Esc 中断回合 · Ctrl+D 退出") + "\n\n")

    def rule(self, label: str = ""):
        """Turn separator: a dim horizontal rule with optional turn number."""
        self.newline()
        if label:
            self._write(dim(f"  ── {label} " + "─" * max(4, 46 - len(label) * 2)) + "\n")
        else:
            self._write(dim("  " + "─" * 50) + "\n")
        self._need_newline = False

    def context_bar(self, used: int, limit: int, model: str = ""):
        """Minimal opencode-style context meter right above the prompt."""
        if not limit:
            return
        used = max(0, used)
        pct = min(1.0, used / limit)
        remaining = (1.0 - pct) * 100
        width = 14
        filled = int(pct * width)
        color_fn = green if pct < 0.5 else (yellow if pct < 0.8 else red)
        bar = color_fn("━" * filled) + dim("─" * (width - filled))
        line = (dim("  ctx ") + bar + dim("  剩余 ") + f"{remaining:.0f}%" +
                gray(f" · {_fmt_tokens(used)}/{_fmt_tokens(limit)}") +
                (gray(f" · {model}") if model else ""))
        self._write(line + "\n")
        self._need_newline = False

    def token_note(self, in_tok: int, out_tok: int, pct=None):
        extra = f" · ctx {pct:.0f}%" if pct is not None else ""
        self._write(gray(f"  ▲ in {in_tok:,} · out {out_tok:,}{extra}") + "\n")
        self._need_newline = False

    def todo_render(self, todos):
        marks = {"completed": (green, "✓"), "in_progress": (yellow, "→"), "pending": (gray, "○")}
        prio = {"high": (red, "!!"), "low": (gray, "·")}
        self.newline()
        self._write(bold("  To-dos") + "\n")
        for t in todos:
            color_fn, mark = marks.get(t.get("status", "pending"), (gray, "○"))
            text = t.get("content", "")
            if t.get("status") == "completed":
                text = gray(text)
            p = t.get("priority", "medium")
            badge = ""
            if p in prio and t.get("status") != "completed":
                pcolor, pmark = prio[p]
                badge = " " + pcolor(pmark)
            self._write(f"  {color_fn(mark)} {text}{badge}\n")
        self._need_newline = False

    # -- prompts --
    def prompt(self) -> str:
        self.newline()
        try:
            with stdin_claim():
                return input(cyan("❯ "))
        except (EOFError, KeyboardInterrupt):
            self._write("\n")
            raise

    def confirm(self, title: str, preview: str = None) -> str:
        """Ask y/a/n inside a bordered dialog. Returns 'y', 'a' or 'n'."""
        self.newline()
        self._write(yellow("  ╭─ ⚠ ") + yellow(f"{title} ") + yellow("─" * max(2, 30 - len(title) * 2)) + "\n")
        if preview:
            plines = preview.splitlines()
            shown = plines[:40]
            for line in shown:
                # colorize diff-style previews
                if line.lstrip().startswith("+"):
                    self._write(green("  │  " + line) + "\n")
                elif line.lstrip().startswith("-"):
                    self._write(red("  │  " + line) + "\n")
                else:
                    self._write(dim("  │  " + line) + "\n")
            if len(plines) > 40:
                self._write(dim(f"  │  … (+{len(plines) - 40} lines)") + "\n")
        else:
            self._write(dim("  │") + "\n")
        self._write(yellow("  ╰─ ") + f"[{bold('y')}] 是  "
                    f"[{bold('a')}] 本次会话总是  [{bold('n')}] 否" + yellow(" ❯ "))
        try:
            with stdin_claim():
                ans = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            self._write("\n")
            return "n"
        if ans.startswith("a"):
            return "a"
        if ans.startswith("y"):
            return "y"
        return "n"

    def choose(self, question: str, options, multi: bool = False,
               allow_other: bool = True) -> list:
        """Numbered multiple-choice prompt (AskUserQuestion tool).
        Free-text input is returned as a custom answer."""
        self.newline()
        self._write(yellow(f"  ? {question}") + "\n")
        for i, opt in enumerate(options, 1):
            if not isinstance(opt, dict):
                opt = {"label": str(opt)}
            line = f"    [{i}] {opt.get('label', '')}"
            if opt.get("description"):
                line += gray(f" — {opt['description']}")
            self._write(line + "\n")
            if opt.get("preview"):
                for pline in str(opt["preview"]).splitlines()[:3]:
                    self._write(dim(f"        {pline}") + "\n")
        if allow_other:
            self._write(gray("    [o] 其他（自由输入）") + "\n")
        hint = "编号，逗号分隔" if multi else "编号"
        try:
            with stdin_claim():
                raw = input(yellow(f"    选择{hint}（回车跳过）❯ ")).strip()
        except (EOFError, KeyboardInterrupt):
            self._write("\n")
            return []
        if raw.lower() == "o" and allow_other:
            try:
                with stdin_claim():
                    custom = input(cyan("    请输入你的答案 ❯ ")).strip()
            except (EOFError, KeyboardInterrupt):
                self._write("\n")
                return []
            return [custom] if custom else []
        picked = set()
        for part in raw.replace("，", ",").split(","):
            part = part.strip()
            if part.isdigit() and 1 <= int(part) <= len(options):
                picked.add(int(part) - 1)
        if not picked and raw and allow_other:
            return [raw]  # typed a custom answer directly
        if multi:
            return [options[i].get("label", str(options[i])) for i in sorted(picked)]
        return [options[picked.pop()].get("label", "")] if picked else []


class StreamRenderer:
    """Line-buffered live rendering: dims fenced code blocks while streaming."""

    def __init__(self, emit):
        self.emit = emit
        self.in_fence = False
        self._buf = ""

    def feed(self, chunk: str):
        self._buf += chunk
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._line(line + "\n")

    def flush(self):
        if self._buf:
            self._line(self._buf)
            self._buf = ""

    def _line(self, line: str):
        if line.strip().startswith("```"):
            self.in_fence = not self.in_fence
            self.emit(dim(line))
        elif self.in_fence:
            self.emit(c(line, "2;36"))
        else:
            self.emit(line)


class SubUI(UI):
    """Quiet UI for subagents: no streaming echo, indented tool lines."""

    def __init__(self, parent: UI):
        super().__init__()
        self.parent = parent

    def stream_text(self, text: str):
        pass

    def stream_reasoning(self, text: str):
        pass

    def tool_line(self, name: str, summary: str = "", prefix: str = "  ┆ "):
        self.parent.tool_line(name, summary, prefix=gray("      ┆ "))

    def tool_result_note(self, text: str):
        pass

    def info(self, msg: str):
        pass

    def token_note(self, *a, **k):
        pass
