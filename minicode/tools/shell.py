"""Shell tools: foreground bash with persistent cwd, plus background shells
(run_in_background / bash_output / bash_kill), on bash / powershell / cmd."""
from __future__ import annotations

import atexit
import codecs
import locale
import os
import shutil
import signal
import subprocess
import threading
from pathlib import Path
from typing import Dict, Optional

from .base import Tool, ToolContext, ToolError, truncate_middle

MARKER = "__MCC_PWD__"


def detect_shell(configured=None) -> str:
    """Return one of: bash | powershell | cmd."""
    if configured:
        s = str(configured).lower()
        if s not in ("bash", "powershell", "cmd"):
            raise ToolError(f"unsupported shell {configured!r} (use bash / powershell / cmd)")
        return s
    if os.name == "nt":
        if shutil.which("bash"):
            return "bash"
        if shutil.which("powershell") or shutil.which("pwsh"):
            return "powershell"
        return "cmd"
    return "bash"


def kill_process_tree(proc: subprocess.Popen):
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _decode(b: bytes) -> str:
    if not b:
        return ""
    if b.startswith(codecs.BOM_UTF16_LE) or b.startswith(codecs.BOM_UTF16_BE):
        return b.decode("utf-16", errors="replace")
    if b.startswith(codecs.BOM_UTF8):
        return b.decode("utf-8-sig", errors="replace")
    if b"\x00" in b[:512]:  # UTF-16-ish output (PowerShell redirection)
        return b.decode("utf-16-le", errors="replace")
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        pass
    # GBK handles Chinese Windows console output when UTF-8 mode hides cp936
    for enc in ("gbk", locale.getpreferredencoding(False) or "utf-8"):
        try:
            return b.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return b.decode("utf-8", errors="replace")


class ShellState:
    """Persistent cwd + shell choice + background shells across tool calls."""

    def __init__(self, cwd: Path, shell: str):
        self.cwd = Path(cwd)
        self.shell = shell
        self.background: Dict[str, "BackgroundShell"] = {}
        self._bg_counter = 0

    def next_id(self) -> str:
        self._bg_counter += 1
        return f"bash_{self._bg_counter}"


class BackgroundShell:
    """A running process with a reader thread accumulating its output."""

    MAX_BUFFER = 200_000  # chars; long-running procs must not eat RAM

    def __init__(self, bid: str, proc: subprocess.Popen):
        self.id = bid
        self.proc = proc
        self.buffer: list = []
        self._size = 0
        self.lock = threading.Lock()
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()
        # never leave orphaned processes behind when minicode exits
        atexit.register(self._kill_if_alive)

    def _read(self):
        try:
            for raw in iter(self.proc.stdout.readline, b""):
                line = _decode(raw)
                if MARKER in line:  # internal cwd probe — not real output
                    continue
                self._append(line)
        except Exception:
            pass

    def _append(self, line: str):
        with self.lock:
            self.buffer.append(line)
            self._size += len(line)
            while self._size > self.MAX_BUFFER and self.buffer:
                self._size -= len(self.buffer[0])
                self.buffer.pop(0)

    def _kill_if_alive(self):
        try:
            if self.proc.poll() is None:
                kill_process_tree(self.proc)
        except Exception:
            pass

    def poll(self) -> Optional[int]:
        return self.proc.poll()

    def drain(self) -> str:
        with self.lock:
            out = "".join(self.buffer)
            self.buffer.clear()
            return out

    def kill(self):
        kill_process_tree(self.proc)

    def output(self) -> str:
        with self.lock:
            return "".join(self.buffer)


class _BaseShellTool(Tool):
    def __init__(self, state: ShellState):
        self.state = state

    def _argv(self, command: str) -> list:
        sh = self.state.shell
        if sh == "bash":
            inner = '$(cygpath -m "$(pwd)" 2>/dev/null || pwd)'
            script = (f"{command}\n"
                      "__mcc_rc=$?\n"
                      f"printf '\\n{MARKER}%s' \"{inner}\"\n"
                      "exit $__mcc_rc")
            return [shutil.which("bash") or "bash", "-c", script]
        if sh == "powershell":
            exe = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
            ps = (f"{command}\n"
                  f"Write-Output (\"{MARKER}\" + (Get-Location).Path)")
            return [exe, "-NoProfile", "-NonInteractive", "-Command", ps]
        one_liner = " & ".join(ln for ln in command.splitlines() if ln.strip())
        return ["cmd", "/V:ON", "/C", f"{one_liner} & echo {MARKER}!CD!"]

    def _popen(self, argv: list) -> subprocess.Popen:
        env = dict(os.environ)
        env.setdefault("PAGER", "cat")
        env.setdefault("GIT_PAGER", "cat")
        kwargs = dict(cwd=str(self.state.cwd), env=env, stdin=subprocess.DEVNULL,
                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if os.name == "posix":
            kwargs["start_new_session"] = True
        try:
            return subprocess.Popen(argv, **kwargs)
        except OSError as e:
            raise ToolError(f"failed to start shell: {e}")

    def _extract_cwd(self, out: str) -> str:
        idx = out.rfind(MARKER)
        if idx == -1:
            return out
        rest = out[idx + len(MARKER):]
        nl = rest.find("\n")
        pwd = (rest[:nl] if nl != -1 else rest).strip()
        after = rest[nl + 1:] if nl != -1 else ""
        before = out[:idx]
        if before.endswith("\n"):
            before = before[:-1]
        if pwd:
            candidate = Path(pwd)
            if candidate.is_dir():
                self.state.cwd = candidate
        return before + ("\n" + after if after else "")


class BashTool(_BaseShellTool):
    name = "bash"
    kind = "bash"
    description = ("Run a shell command and get its combined output. "
                   "Working directory persists between calls (a `cd` in one call affects later ones). "
                   "Interactive commands and commands that read stdin are not supported. "
                   "For long-running processes (dev servers, watchers) pass run_in_background=true.")
    input_schema = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The shell command to run."},
            "description": {"type": "string",
                            "description": "One-line purpose of this command (for the user)."},
            "timeout": {"type": "integer", "description": "Timeout in seconds (default 120, max 600)."},
            "run_in_background": {"type": "boolean",
                                  "description": "Start without waiting; poll with bash_output, "
                                                 "stop with bash_kill."},
        },
        "required": ["command"],
    }

    def describe_call(self, args: dict) -> str:
        desc = args.get("description")
        one = " ".join(str(args.get("command") or "").split())[:90]
        prefix = "bg " if args.get("run_in_background") else ""
        return f"{prefix}{desc}: {one}" if desc else f"{prefix}{one}"

    def preview(self, args: dict, ctx: ToolContext = None) -> str:
        prefix = "(background) " if args.get("run_in_background") else ""
        return "$ " + prefix + str(args.get("command") or "")

    def run(self, args: dict, ctx: ToolContext) -> str:
        command = str(args.get("command") or "").strip()
        if not command:
            raise ToolError("command is required")
        if not self.state.cwd.exists():
            self.state.cwd = ctx.cwd
        if args.get("run_in_background"):
            return self._start_background(command, ctx)
        try:
            timeout = min(600, max(1, int(args.get("timeout") or ctx.config.timeout)))
        except (TypeError, ValueError):
            timeout = ctx.config.timeout
        proc = self._popen(self._argv(command))
        timed_out = False
        try:
            out_bytes, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            kill_process_tree(proc)
            out_bytes, _ = proc.communicate()
        out = self._extract_cwd(_decode(out_bytes or b""))
        if timed_out:
            body = truncate_middle(out.strip(), 8000) or "(no output)"
            return f"Error: command timed out after {timeout}s.\nPartial output:\n{body}"
        out = truncate_middle(out.rstrip("\n"))
        if proc.returncode == 0:
            return out or "(no output)"
        return f"Exit code: {proc.returncode}\n{out or '(no output)'}"

    def _start_background(self, command: str, ctx: ToolContext) -> str:
        bid = self.state.next_id()
        proc = self._popen(self._argv(command))
        self.state.background[bid] = BackgroundShell(bid, proc)
        return (f"Started in background as {bid}. "
                f"Poll its output with bash_output; stop it with bash_kill. "
                f"Do not block waiting for it.")


class BashOutputTool(_BaseShellTool):
    name = "bash_output"
    kind = "read"
    description = ("Read new output from a background shell started with "
                   "bash run_in_background=true. Omits output already returned.")
    input_schema = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "Background shell id (default: latest)."},
            "block": {"type": "boolean",
                      "description": "Wait for new output or exit before returning (default false)."},
            "timeout": {"type": "integer",
                        "description": "Max seconds to wait when block=true (default 30, max 120)."},
        },
    }

    def run(self, args: dict, ctx: ToolContext) -> str:
        sh = self._pick(args.get("id"))
        if args.get("block"):
            try:
                timeout = min(120, max(1, int(args.get("timeout") or 30)))
            except (TypeError, ValueError):
                timeout = 30
            import time
            deadline = time.time() + timeout
            while time.time() < deadline:
                if sh.poll() is not None:
                    break
                with sh.lock:
                    has_new = bool(sh.buffer)
                if has_new:
                    break
                time.sleep(0.2)
        out = sh.drain().strip()
        rc = sh.poll()
        status = "running" if rc is None else f"exited with code {rc}"
        body = truncate_middle(out, 10000) if out else "(no new output)"
        return f"{sh.id} [{status}]\n{body}"

    def _pick(self, bid) -> BackgroundShell:
        if not self.state.background:
            raise ToolError("no background shells (start one with bash run_in_background=true)")
        if bid:
            key = str(bid)
            if key.isdigit():
                key = f"bash_{key}"
            sh = self.state.background.get(key)
            if sh is None:
                raise ToolError(f"unknown background shell {bid!r}; "
                                f"known: {', '.join(self.state.background)}")
            return sh
        return list(self.state.background.values())[-1]


class BashKillTool(_BaseShellTool):
    name = "bash_kill"
    kind = "read"  # only kills shells this session started
    description = "Stop a background shell started earlier."
    input_schema = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "Background shell id (default: latest)."},
        },
    }

    def run(self, args: dict, ctx: ToolContext) -> str:
        if not self.state.background:
            raise ToolError("no background shells")
        bid = str(args.get("id") or list(self.state.background)[-1])
        if bid.isdigit():
            bid = f"bash_{bid}"
        sh = self.state.background.get(bid)
        if sh is None:
            raise ToolError(f"unknown background shell {bid!r}")
        if sh.poll() is None:
            sh.kill()
            sh.proc.wait(timeout=10)
            return f"bash_{bid} killed."
        return f"bash_{bid} already exited with code {sh.poll()}."
