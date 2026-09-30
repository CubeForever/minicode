"""Terminal UI: ANSI colors, streaming output, spinner, confirmations,
zero-dependency terminal markdown rendering."""
from __future__ import annotations

import itertools
import os
import re
import sys
import threading
import time
import unicodedata

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

    def token_note(self, in_tok: int, out_tok: int, pct=None, cache_read: int = 0):
        extra = f" · ctx {pct:.0f}%" if pct is not None else ""
        if cache_read and in_tok:
            extra += gray(f" · 缓存 {_fmt_tokens(cache_read)}"
                          f" ({min(100.0, cache_read / in_tok * 100):.0f}%)")
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


class RawStream:
    """Pass-through stream renderer for Web/bridge UIs: the browser renders
    markdown itself, so text must reach it untouched (终端 Markdown 转换
    只属于终端 —— 若把 • / 表格字形提前画进文本，浏览器端将无法再渲染)。"""

    def __init__(self, emit):
        self.emit = emit

    def feed(self, chunk: str):
        self.emit(chunk)

    def flush(self):
        pass


class StreamRenderer:
    """Line-buffered live rendering with zero-dependency terminal markdown.

    只对完整行渲染（流式按行缓冲，语义与内容逐字不丢）：围栏代码整体置灰，
    标题/列表/引用/分隔线结构化，行内 **粗体** / *斜体* / `代码` / 删除线 /
    [链接](url) 转 ANSI；GFM 表格缓冲到结束后按显示宽度对齐（检测引入一行
    延迟，肉眼不可感知）。无色模式（NO_COLOR/非 tty）下去除标记但保留文本。
    """

    _SEP_RE = re.compile(r"^[\s:\-|]*\|[\s:\-|]*$")

    def __init__(self, emit):
        self.emit = emit
        self.in_fence = False
        self._buf = ""
        self._pending = None   # 疑似表头的上一行（含 | 的完整行）
        self._table = None     # [表头, 分隔行, 数据行...] 原始行缓冲

    def feed(self, chunk: str):
        self._buf += chunk
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._line(line + "\n")

    def flush(self):
        if self._buf:
            self._line(self._buf)
            self._buf = ""
        self._flush_pending()
        self._flush_table()

    # -- line dispatch --
    def _line(self, line: str):
        stripped = line.strip()
        if stripped.startswith("```"):
            self._flush_pending()
            self._flush_table()
            self.in_fence = not self.in_fence
            self.emit(dim(line))
            return
        if self.in_fence:
            self.emit(c(line, "2;36"))
            return
        if self._table is not None:
            if stripped and "|" in stripped:
                self._table.append(stripped)
                return
            self._flush_table()   # 表格结束；当前行继续走普通渲染
        if self._pending is not None:
            if "|" in stripped and self._SEP_RE.match(stripped):
                self._table = [self._pending, stripped]
                self._pending = None
                return
            held, self._pending = self._pending, None
            self._emit_block(held + "\n")
        if "|" in stripped:
            self._pending = stripped   # 可能是表头——看下一行
            return
        self._emit_block(line)

    def _flush_pending(self):
        if self._pending is not None:
            held, self._pending = self._pending, None
            self._emit_block(held + "\n")

    def _flush_table(self):
        rows, self._table = self._table, None
        if not rows:
            return
        parsed = [_split_md_row(r) for r in rows]
        ncol = max(len(cells) for cells in parsed)
        for cells in parsed:
            cells += [""] * (ncol - len(cells))
        widths = [max(_disp_width(cells[i]) for cells in parsed)
                  for i in range(ncol)]
        total = sum(widths) + 3 * (ncol - 1)
        if total > 110:
            for r in rows:   # 过宽表格：原样输出——宁可朴素不可错位
                self.emit(dim(r) + "\n")
            return
        self.emit("  " + dim(" │ ").join(_pad_md(parsed[0][i], widths[i])
                                         for i in range(ncol)).rstrip() + "\n")
        self.emit(dim("  " + "┼".join("─" * w for w in widths)) + "\n")
        for cells in parsed[2:]:
            self.emit("  " + dim(" │ ").join(_pad_md(cells[i], widths[i])
                                             for i in range(ncol)).rstrip() + "\n")

    # -- block rendering --
    def _emit_block(self, line: str):
        body = line.rstrip("\n")
        nl = "\n" if line.endswith("\n") else ""
        if not body.strip():
            self.emit(body + nl)
            return
        m = re.match(r"^\s*(#{1,6})\s+(.*)$", body)
        if m:
            style = "1;36" if len(m.group(1)) <= 2 else "1"
            self.emit("  " + c(md_inline(m.group(2)), style) + nl)
            return
        if re.match(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$", body):
            self.emit(dim("  " + "─" * 30) + nl)
            return
        m = re.match(r"^(\s*)>\s?(.*)$", body)
        if m:
            self.emit(m.group(1) + dim("│ " + md_inline(m.group(2))) + nl)
            return
        m = re.match(r"^(\s*)[-*+]\s+(.*)$", body)
        if m:
            content = m.group(2)
            box = re.match(r"^\[( |x|X)\]\s*(.*)$", content)
            if box:
                content = (green("✓ ") if box.group(1) in ("x", "X")
                           else dim("○ ")) + box.group(2)
            self.emit(m.group(1) + dim("• ") + md_inline(content) + nl)
            return
        m = re.match(r"^(\s*)(\d+)([.)])\s+(.*)$", body)
        if m:
            self.emit(m.group(1) + c(m.group(2) + m.group(3) + " ", "2;36")
                      + md_inline(m.group(4)) + nl)
            return
        self.emit(md_inline(body) + nl)


def _disp_width(s: str) -> int:
    """终端显示宽度（CJK 全角计 2）。"""
    w = 0
    for ch in s:
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def md_inline(text: str) -> str:
    """行内 Markdown → ANSI（零依赖）。按 code→bold→italic→strike→link 顺序
    stash 渲染结果再统一还原，避免嵌套替换互相污染；无匹配快速返回。"""
    if "`" not in text and "*" not in text and "~" not in text and "[" not in text:
        return text
    stash: list = []

    def keep(rendered: str) -> str:
        stash.append(rendered)
        return f"\x00{len(stash) - 1}\x00"

    text = re.sub(r"`([^`]+)`", lambda m: keep(c(m.group(1), "36")), text)
    text = re.sub(r"\*\*([^*]+)\*\*", lambda m: keep(c(m.group(1), "1")), text)
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", lambda m: keep(c(m.group(1), "3")), text)
    text = re.sub(r"~~([^~]+)~~", lambda m: keep(c(m.group(1), "9")), text)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
                  lambda m: keep(c(m.group(1), "36") + dim(f" ({m.group(2)})")), text)
    if not stash:
        return text
    prev = None
    while prev != text and "\x00" in text:
        prev = text
        text = re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], text)
    return text


def _split_md_row(line: str) -> list:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [cell.strip() for cell in line.split("|")]


def _pad_md(text: str, width: int) -> str:
    rendered = md_inline(text)
    pad = width - _disp_width(_ANSI_RE.sub("", rendered))
    return rendered + " " * max(0, pad)


class SubUI(UI):
    """Quiet UI for subagents: no streaming echo, indented tool lines,
    non-interactive confirmations (subagents have no terminal — permission
    prompts auto-decline instead of stealing stdin from the main thread)."""

    def __init__(self, parent: UI):
        super().__init__()
        self.parent = parent

    def stream_text(self, text: str):
        pass

    def stream_reasoning(self, text: str):
        pass

    def confirm(self, title: str, preview=None) -> str:
        return "n"   # 子代理没有交互终端：一律拒绝，由模型改道

    def choose(self, question: str, options, multi: bool = False,
               allow_other: bool = True) -> list:
        return []

    def tool_line(self, name: str, summary: str = "", prefix: str = "  ┆ "):
        self.parent.tool_line(name, summary, prefix=gray("      ┆ "))

    def tool_result_note(self, text: str):
        pass

    def info(self, msg: str):
        pass

    def token_note(self, *a, **k):
        pass
