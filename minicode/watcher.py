"""Turn-time keyboard watcher: Esc interrupts the running turn, typed text
queues up and is injected after the current turn ends.

零依赖实现：POSIX 用 termios(select) 的 cbreak 模式读键，Windows 用 msvcrt
轮询。回合结束（with 退出）后恢复终端状态。主线程 confirm()/input() 期间
通过 ui.INPUT_ACTIVE 暂停读取，把按键完整让给行编辑器。

 与 Claude Code 对齐的交互：
    Esc          → 立即中断当前回合（部分回复保留）
    输入 + 回车  → 消息排队，回合结束后自动作为新提示词执行
    Ctrl+C       → 保持原 SIGINT 行为（POSIX cbreak 保留 ISIG）
"""
from __future__ import annotations

import os
import sys
import threading
import time

from .ui import INPUT_ACTIVE

_ESC_SEQ_GRACE = 0.02     # Esc 与后续按键（方向键序列）的区分窗口
_PASTE_GRACE = 0.004      # 回车后短暂仍有输入 → 视为粘贴内换行


class TurnWatcher:
    """在回合执行期间监听键盘。用法：

        with TurnWatcher(agent.interrupt_event, ui) as w:
            _run_turn(agent, text)
            for msg in w.drain(): ...
    """

    def __init__(self, interrupt_event: threading.Event, ui=None):
        self.interrupt_event = interrupt_event
        self.ui = ui
        self._queue = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._saved = None
        self._buf = ""

    # ---------- public ----------
    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()
        return False

    def start(self):
        self.interrupt_event.clear()
        try:
            if not sys.stdin.isatty():
                return
        except (OSError, ValueError, AttributeError):
            return
        if os.name == "nt" or self._can_termios():
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.5)
            self._thread = None
        self._restore()

    def drain(self):
        """取走回合期间排队的全部消息。"""
        with self._lock:
            out, self._queue = self._queue, []
        return out

    # ---------- shared loop ----------
    def _loop(self):
        if os.name == "nt":
            self._loop_windows()
        else:
            self._loop_posix()

    def _paused(self) -> bool:
        """主线程正在用 input()（confirm/choose/提示符）时暂停读键。"""
        return INPUT_ACTIVE.is_set()

    def _note(self, text: str):
        if self.ui is not None:
            try:
                self.ui.info(text)
            except Exception:
                pass

    def _interrupt(self):
        self.interrupt_event.set()

    # ---------- POSIX ----------
    @staticmethod
    def _can_termios() -> bool:
        try:
            import select  # noqa: F401
            import termios  # noqa: F401
            import tty  # noqa: F401
            return hasattr(sys.stdin, "fileno") and os.isatty(sys.stdin.fileno())
        except Exception:
            return False

    def _loop_posix(self):
        import select
        import termios
        import tty

        def enter_raw():
            try:
                self._saved = termios.tcgetattr(0)
                tty.setcbreak(0)   # 关行缓冲与回显；保留 ISIG（Ctrl+C 不变）
                return True
            except (termios.error, OSError, ValueError):
                return False

        raw = enter_raw()
        try:
            while not self._stop.is_set():
                if self._paused():
                    if raw:
                        self._restore()
                        raw = False
                    time.sleep(0.05)
                    continue
                if not raw:
                    raw = enter_raw()
                    if not raw:
                        return
                r, _w, _x = select.select([0], [], [], 0.05)
                if not r:
                    continue
                try:
                    data = os.read(0, 4096)
                except OSError:
                    return
                if not data:
                    return
                self._handle_bytes(data, lambda: bool(select.select([0], [], [], _PASTE_GRACE)[0]))
        finally:
            if raw:
                self._restore()

    def _handle_bytes(self, data: bytes, more_pending) -> None:
        """解析一段终端输入字节：可打印字符入缓冲；孤 Esc = 中断；
        粘连回车（后面还有字节）= 粘贴内换行；独立回车 = 提交排队。"""
        import sys as _sys
        enc = getattr(_sys.stdout, "encoding", None) or "utf-8"
        i = 0
        n = len(data)
        while i < n:
            b = data[i]
            if b == 0x1B:   # Esc：区分「孤按」与转义序列（方向键等）
                if i + 1 < n or more_pending():
                    i += 1     # 序列开头 → 丢弃整个序列的字节
                    while i < n and data[i] not in (0x40, 0x7E):
                        i += 1
                    i = min(i + 1, n)
                    continue
                self._interrupt()
                return
            if b in (0x0D, 0x0A):   # 回车
                if i + 1 < n or more_pending():
                    self._buffer_append("\n")
                else:
                    self._commit()
                i += 1
                continue
            if b in (0x7F, 0x08):   # 退格
                self._buffer_backspace()
                i += 1
                continue
            if b < 0x20:            # 其余控制键忽略
                i += 1
                continue
            # 多字节 UTF-8（CJK）：收集完整字符
            length = 1
            if b >= 0xF0:
                length = 4
            elif b >= 0xE0:
                length = 3
            elif b >= 0xC0:
                length = 2
            chunk = data[i:i + length]
            i += length
            try:
                self._buffer_append(chunk.decode(enc, errors="replace"))
            except (LookupError, UnicodeDecodeError):
                pass

    # ---------- Windows ----------
    def _loop_windows(self):
        import msvcrt

        while not self._stop.is_set():
            if self._paused():
                time.sleep(0.05)
                continue
            if not msvcrt.kbhit():
                time.sleep(0.03)
                continue
            try:
                ch = msvcrt.getwch()
            except (OSError, ValueError):
                return
            if ch in ("\x00", "\xe0"):       # 功能键/方向键前缀 → 丢弃键码
                try:
                    msvcrt.getwch()
                except (OSError, ValueError):
                    pass
                continue
            if ch == "\x1b":                 # Esc：无后续键才是中断
                if not msvcrt.kbhit():
                    self._interrupt()
                    return
                continue
            if ch in ("\r", "\n"):           # 粘连回车 = 粘贴内换行
                if msvcrt.kbhit():
                    self._buffer_append("\n")
                else:
                    self._commit()
                continue
            if ch in ("\x08", "\x7f"):
                self._buffer_backspace()
                continue
            if ch == "\x03":                 # Ctrl+C 兜底（正常走 SIGINT）
                self._interrupt()
                return
            if ch >= " " or ch == "\t":
                self._buffer_append(ch)

    # ---------- 缓冲 ----------
    def _buffer_append(self, ch: str):
        with self._lock:
            self._buf += ch

    def _buffer_backspace(self):
        with self._lock:
            self._buf = self._buf[:-1]

    def _commit(self):
        with self._lock:
            line, self._buf = self._buf, ""
        if not line.strip():
            return
        with self._lock:
            self._queue.append(line)
        self._note(f"⏎ 已排队（回合结束后执行）：{' '.join(line.split())[:80]}")

    # ---------- 终端状态 ----------
    def _restore(self):
        if self._saved is None:
            return
        try:
            import termios
            termios.tcsetattr(0, termios.TCSADRAIN, self._saved)
        except Exception:
            pass
        self._saved = None
