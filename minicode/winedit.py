"""Windows 原生行编辑器（msvcrt，零依赖）——pyreadline3 缺席时的完整替代。

能力：光标移动（←→/Home/End）、历史导航（↑↓，落盘 ~/.minicode/history）、
斜杠命令与 @文件 Tab 补全、多行粘贴（burst 检测，内部换行不提前提交）、
CJK 宽度光标渲染、Esc 清空、Ctrl+C/D/U/W。终端需启用 VT 处理
（ui.py 已在导入时开启）。
"""
from __future__ import annotations

import msvcrt
import os
import re
import shutil
import sys

from .lineinput import (_HISTORY_LIMIT, _append_history_file,
                        _completions_for, _disp_width, _load_history_lines)

_SPECIAL = {"H": "up", "P": "down", "K": "left", "M": "right",
            "G": "home", "O": "end", "S": "delete"}


class _Editor:
    def __init__(self, prompt: str, command_names):
        self.prompt = prompt
        self.command_names = list(command_names)
        self.buf = ""          # 可含 \n 的多行缓冲
        self.pos = 0           # 光标字符索引
        self.history = _load_history_lines()
        self.hist_idx = None
        self.hist_draft = ""
        self.rendered_rows = 0  # 上次渲染占用的屏幕行数（0 = 尚未渲染）

    # ---------- 渲染 ----------
    def _term_width(self) -> int:
        try:
            return shutil.get_terminal_size((100, 24)).columns
        except (OSError, ValueError):
            return 100

    def _display_rows(self) -> int:
        """当前渲染总行数（考虑终端自动换行）。"""
        width = max(20, self._term_width())
        total = 0
        for i, seg in enumerate(self.buf.split("\n")):
            cells = (_disp_width(self.prompt) if i == 0 else 2) + _disp_width(seg)
            total += max(1, -(-cells // width))
        return max(1, total)

    def _redraw(self):
        w = sys.stdout
        if self.rendered_rows > 1:
            w.write(f"\x1b[{self.rendered_rows - 1}A")
        w.write("\r\x1b[J")
        segs = self.buf.split("\n")
        shown = self.prompt + segs[0] if segs else self.prompt
        for seg in segs[1:]:
            shown += "\n  " + seg
        w.write(shown)
        self.rendered_rows = self._display_rows()
        # 光标定位：先算目标 (行, 列)
        target_row = target_col = 0
        for i, seg in enumerate(self.buf[:self.pos].split("\n")):
            target_row = i
            target_col = (_disp_width(self.prompt) if i == 0 else 2) + _disp_width(seg)
        rows_up = self.rendered_rows - 1 - target_row
        if rows_up > 0:
            w.write(f"\x1b[{rows_up}A")
        w.write("\r")
        if target_col > 0:
            w.write(f"\x1b[{target_col}C")
        w.flush()

    # ---------- 编辑 ----------
    def _insert(self, text: str):
        if not text:
            return
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        self.buf = self.buf[:self.pos] + text + self.buf[self.pos:]
        self.pos += len(text)
        self.hist_idx = None

    def _backspace(self):
        if self.pos > 0:
            self.buf = self.buf[:self.pos - 1] + self.buf[self.pos:]
            self.pos -= 1

    def _delete(self):
        if self.pos < len(self.buf):
            self.buf = self.buf[:self.pos] + self.buf[self.pos + 1:]

    def _del_word(self):
        head = self.buf[:self.pos]
        new_head = re.sub(r"\S*\s*$", "", head)
        self.buf = new_head + self.buf[self.pos:]
        self.pos = len(new_head)

    def _special(self, code: str):
        action = _SPECIAL.get(code.upper())
        if action == "left":
            self.pos = max(0, self.pos - 1)
        elif action == "right":
            self.pos = min(len(self.buf), self.pos + 1)
        elif action == "home":
            self.pos = 0
        elif action == "end":
            self.pos = len(self.buf)
        elif action == "delete":
            self._delete()
        elif action == "up":
            if "\n" in self.buf:
                self.pos = 0
            else:
                self._hist_nav(up=True)
        elif action == "down":
            if "\n" in self.buf:
                self.pos = len(self.buf)
            else:
                self._hist_nav(up=False)

    def _hist_nav(self, up: bool):
        if up:
            if self.hist_idx is None:
                self.hist_draft = self.buf
                self.hist_idx = len(self.history) - 1
            elif self.hist_idx > 0:
                self.hist_idx -= 1
            else:
                return
        else:
            if self.hist_idx is None:
                return
            if self.hist_idx < len(self.history) - 1:
                self.hist_idx += 1
            else:
                self.hist_idx = None
                self.buf = self.hist_draft
                self.pos = len(self.buf)
                return
        self.buf = self.history[self.hist_idx]
        self.pos = len(self.buf)

    def _tab_complete(self):
        before = self.buf[:self.pos]
        word = re.split(r"\s", before)[-1] if before else ""
        opts = _completions_for(word, self.command_names)
        if not opts:
            return
        if len(opts) > 1:
            common = os.path.commonprefix(opts)
            if len(common) > len(word):
                opts = [common]
        if len(opts) == 1:
            add = opts[0][len(word):]
            self.buf = self.buf[:self.pos] + add + self.buf[self.pos:]
            self.pos += len(add)
        else:
            sys.stdout.write("\n  " + "  ".join(opts[:24]) + "\n")
            self.rendered_rows = 0   # 候选已打印，编辑块从新行开始

    def _commit(self) -> str:
        line = self.buf
        sys.stdout.write("\n")
        sys.stdout.flush()
        self.rendered_rows = 0
        if line.strip() and (not self.history or self.history[-1] != line):
            self.history.append(line)
            self.history = self.history[-_HISTORY_LIMIT:]
            _append_history_file(line)
        return line

    # ---------- 主循环 ----------
    def run(self) -> str:
        sys.stdout.write(self.prompt)
        sys.stdout.flush()
        self.rendered_rows = 1
        while True:
            try:
                ch = msvcrt.getwch()
            except (OSError, ValueError):
                return ""
            if ch in ("\x00", "\xe0"):            # 特殊键前缀
                try:
                    self._special(msvcrt.getwch())
                except (OSError, ValueError):
                    pass
            elif ch in ("\r", "\n"):              # 回车：burst 内 = 粘贴换行
                if msvcrt.kbhit():
                    burst = [ch]
                    while msvcrt.kbhit():
                        burst.append(msvcrt.getwch())
                    self._insert("".join(
                        "\n" if c == "\r" else c for c in burst))
                else:
                    line = self._commit()
                    return line
            elif ch == "\x08":                    # Backspace
                self._backspace()
            elif ch == "\x7f":
                self._backspace()
            elif ch == "\x1b":                    # Esc 清空当前输入
                self.buf = ""
                self.pos = 0
            elif ch == "\x03":                    # Ctrl+C
                sys.stdout.write("\n")
                sys.stdout.flush()
                raise KeyboardInterrupt
            elif ch == "\x04":                    # Ctrl+D
                if not self.buf:
                    raise EOFError
            elif ch == "\x15":                    # Ctrl+U 清行
                self.buf = ""
                self.pos = 0
            elif ch == "\x17":                    # Ctrl+W 删词
                self._del_word()
            elif ch == "\t":
                self._tab_complete()
            elif ch >= " ":
                chars = [ch]
                while msvcrt.kbhit():             # 粘贴 burst：一次插入一次重绘
                    chars.append(msvcrt.getwch())
                    if len(chars) > 8192:
                        break
                self._insert("".join(chars))
            self._redraw()


def windows_read_line(prompt: str, command_names=()) -> str:
    """msvcrt 行编辑入口（非 tty 或异常时回退 input()）。"""
    try:
        return _Editor(prompt, command_names).run()
    except KeyboardInterrupt:
        raise
    except EOFError:
        raise
    except Exception:
        # 渲染层异常时保证可用性：退回原生 input()
        try:
            sys.stdout.write("\n")
        except OSError:
            pass
        return input(prompt)
