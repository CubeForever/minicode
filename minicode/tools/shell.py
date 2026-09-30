"""Shell tools: foreground bash with persistent cwd AND persistent environment
(exports/venv activation survive across calls via env snapshots), plus
background shells (run_in_background / bash_output / bash_kill), on
bash / powershell / cmd."""
from __future__ import annotations

import atexit
import codecs
import locale
import os
import re
import shutil
import signal
import subprocess
import threading
from pathlib import Path
from typing import Dict, Optional

from .base import Tool, ToolContext, ToolError, truncate_middle

MARKER = "__MCC_PWD__"
ENV_MARKER = "__MCC_ENV__"

# 每条命令都会被 bash 自身改写的变量——不纳入持久化差分
_ENV_NOISE = {"PWD", "OLDPWD", "SHLVL", "_", "PS1"}
_ENV_KEY_OK = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# cmd 无引号 set 值中需要 ^ 转义的字符（& 前不留空格，空格本身也要转义）
_CMD_UNSAFE = set(" &|<>()^=\"\t")


def find_bash_exe() -> Optional[str]:
    """Locate a real Git/MSYS bash. On Windows the PATH may resolve ``bash``
    to the System32 WSL launcher stub, which is useless without WSL — skip it
    and fall back to known Git for Windows install locations."""
    w = shutil.which("bash")
    if w and "system32" not in w.lower():
        return w
    for guess in (r"C:\Program Files\Git\bin\bash.exe",
                  r"C:\Program Files\Git\usr\bin\bash.exe",
                  r"C:\Program Files (x86)\Git\bin\bash.exe"):
        if os.path.isfile(guess):
            return guess
    return None


def detect_shell(configured=None) -> str:
    """Return one of: bash | powershell | cmd."""
    if configured:
        s = str(configured).lower()
        if s not in ("bash", "powershell", "cmd"):
            raise ToolError(f"unsupported shell {configured!r} (use bash / powershell / cmd)")
        return s
    if os.name == "nt":
        if find_bash_exe():
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
    """Persistent cwd + shell choice + persistent environment + background
    shells across tool calls.

    环境持久化（bash / powershell / cmd）：ShellState 创建时抓取一次环境
    基线；此后每条命令执行完附加环境转储，与基线做差分得到 overrides/
    unsets；下一条命令前按各自 shell 语法重放——`export`、`source activate`、
    `unset`（bash）、`$env:X`（PowerShell）、`set X`（cmd）从此跨调用存活。
    """

    def __init__(self, cwd: Path, shell: str):
        self.cwd = Path(cwd)
        self.shell = shell
        self.background: Dict[str, "BackgroundShell"] = {}
        self._bg_counter = 0
        self._env_baseline: Optional[dict] = None
        self._env_overrides: Dict[str, str] = {}
        self._env_unsets: set = set()
        if shell in ("bash", "powershell", "cmd"):
            self._capture_env_baseline()

    def next_id(self) -> str:
        self._bg_counter += 1
        return f"bash_{self._bg_counter}"

    # ---------- persistent environment ----------
    def _capture_env_baseline(self):
        """按 shell 抓取环境基线。任一环节失败 → 持久化静默降级。"""
        try:
            if self.shell == "bash":
                import base64
                bash = find_bash_exe() or "bash"
                # 与命令尾的转储同一条 base64 管道：env -0 的 NUL 字节若直接
                # 进入解码链会被 _decode 误判成 UTF-16
                proc = subprocess.run([bash, "-c", "env -0 | base64 -w0"],
                                      cwd=str(self.cwd), capture_output=True,
                                      timeout=10, env=dict(os.environ))
                raw = base64.b64decode(_decode(proc.stdout or b"").strip())
                self._env_baseline = self._parse_env(raw.decode("utf-8",
                                                                errors="replace"))
            elif self.shell == "powershell":
                exe = shutil.which("pwsh") or shutil.which("powershell")
                if not exe:
                    self._env_baseline = None
                    return
                dump = self._ps_env_dump()
                proc = subprocess.run(
                    [exe, "-NoProfile", "-NonInteractive", "-Command", dump],
                    cwd=str(self.cwd), capture_output=True, timeout=20,
                    env=dict(os.environ))
                self._env_baseline = self._parse_env(_decode(proc.stdout or b""))
            elif self.shell == "cmd":
                proc = subprocess.run(["cmd", "/C", "set"], cwd=str(self.cwd),
                                      capture_output=True, timeout=15,
                                      env=dict(os.environ))
                self._env_baseline = self._parse_env(_decode(proc.stdout or b""))
        except Exception:
            self._env_baseline = None

    @staticmethod
    def _ps_env_dump() -> str:
        return ('Get-ChildItem Env: | ForEach-Object '
                '{ "$($_.Name)=$($_.Value)" }')

    @staticmethod
    def _parse_env(dump: str) -> dict:
        """bash 路径：NUL 分隔的 KEY=VALUE（env -0 解码后）。"""
        env = {}
        for chunk in dump.split("\x00"):
            if "=" not in chunk:
                continue
            k, _, v = chunk.partition("=")
            if k and k not in _ENV_NOISE:
                env[k] = v
        return env

    @staticmethod
    def _parse_env_lines(dump: str) -> dict:
        """PowerShell / cmd 路径：逐行 KEY=VALUE。cmd 的驱动器伪变量
        （`=C:` 开头）与空键跳过；含换行无法跨行表示的值丢弃。"""
        env = {}
        for line in dump.splitlines():
            line = line.strip("\r\n")
            if "=" not in line or line.startswith("="):
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            if k and k not in _ENV_NOISE and "\n" not in v and "\r" not in v:
                env[k] = v
        return env

    def _parse_dump(self, dump: str) -> dict:
        if self.shell == "bash":
            return self._parse_env(dump)
        return self._parse_env_lines(dump)

    def update_env(self, dump: str):
        """命令执行后的环境转储 → 差分更新 overrides/unsets。转储为空或
        解析不出任何变量时视为不可信，直接跳过——绝不把「无转储」当成
        「全部变量被 unset」。"""
        if self._env_baseline is None:
            return
        dump = (dump or "").strip()
        if not dump:
            return
        current: dict = {}
        if self.shell == "bash":
            import base64
            try:
                raw = base64.b64decode(dump, validate=True).decode("utf-8",
                                                                   errors="replace")
            except Exception:
                return
            if "=" not in raw:
                return
            current = self._parse_env(raw)
        else:
            current = self._parse_env_lines(dump)
        if not current:
            return
        overrides, unsets = {}, set()
        for k in set(self._env_baseline) | set(current):
            base_v = self._env_baseline.get(k)
            cur_v = current.get(k)
            if cur_v is None and base_v is not None:
                unsets.add(k)
            elif cur_v is not None and cur_v != base_v:
                if "\n" in cur_v or "\r" in cur_v:
                    continue   # 行式转储无法安全回传多行值（bash 走 NUL 不受影响）
                overrides[k] = cur_v
        self._env_overrides, self._env_unsets = overrides, unsets

    def env_export_prefix(self) -> str:
        """下一条命令前的重放前缀（按 shell 语法）。值经编码传输，杜绝
        引号/特殊字符炸掉语法；非法变量名跳过。"""
        if self._env_baseline is None:
            return ""
        parts: list = []
        if self.shell == "bash":
            import base64
            for k in sorted(self._env_unsets):
                if _ENV_KEY_OK.match(k):
                    parts.append(f"unset {k};")
            for k, v in sorted(self._env_overrides.items()):
                if not _ENV_KEY_OK.match(k):
                    continue
                enc = base64.b64encode(v.encode("utf-8", "surrogateescape")).decode("ascii")
                parts.append(f"export {k}=$(printf %s '{enc}' | base64 -d);")
            return " ".join(parts)
        if self.shell == "powershell":
            for k in sorted(self._env_unsets):
                if _ENV_KEY_OK.match(k):
                    parts.append(f"Remove-Item Env:{k} -ErrorAction SilentlyContinue;")
            for k, v in sorted(self._env_overrides.items()):
                if not _ENV_KEY_OK.match(k) or "\n" in v or "\r" in v:
                    continue
                safe = v.replace("'", "''")
                parts.append(f"$env:{k}='{safe}';")
            return "".join(parts)
        if self.shell == "cmd":
            # cmd 的引号会破坏 /V:ON 延迟展开（引号超过两个时 cmd 的
            # 引号剥离规则把整行搞坏），因此 set 一律不加引号：
            # 值逐字符 ^ 转义（含空格），跳过含 %/" 的值，& 之前不留空格
            # （否则尾随空格会并进值里）。
            for k in sorted(self._env_unsets):
                if _ENV_KEY_OK.match(k):
                    parts.append(f"set {k}=&")
            for k, v in sorted(self._env_overrides.items()):
                if not _ENV_KEY_OK.match(k) or "\n" in v or "\r" in v:
                    continue
                if "%" in v or '"' in v:
                    continue   # % 无法在命令行内安全转义
                esc = "".join("^" + ch if ch in _CMD_UNSAFE else ch for ch in v)
                parts.append(f"set {k}={esc}&")
            return "".join(parts)
        return ""


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
                if ENV_MARKER in line:
                    break   # 尾部 env 转储是内部探针，不进入后台输出
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
        prefix = self.state.env_export_prefix()
        if sh == "bash":
            inner = '$(cygpath -m "$(pwd)" 2>/dev/null || pwd)'
            script = (f"{prefix}{command}\n"
                      "__mcc_rc=$?\n"
                      f"printf '\\n{MARKER}%s' \"{inner}\"\n"
                      f"printf '\\n{ENV_MARKER}'\n"
                      "env -0 2>/dev/null | base64 -w0 2>/dev/null\n"
                      "exit $__mcc_rc")
            return [find_bash_exe() or "bash", "-c", script]
        if sh == "powershell":
            exe = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
            ps = (f"{prefix}{command}\n"
                  f"Write-Output (\"{MARKER}\" + (Get-Location).Path)\n"
                  f"Write-Output \"{ENV_MARKER}\"\n"
                  f"{self.state._ps_env_dump()}")
            return [exe, "-NoProfile", "-NonInteractive", "-Command", ps]
        one_liner = " & ".join(ln for ln in command.splitlines() if ln.strip())
        return ["cmd", "/V:ON", "/C",
                f"{prefix}{one_liner} & echo {MARKER}!CD! & echo {ENV_MARKER} & set"]

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

    def _split_env_dump(self, out: str) -> str:
        """剥掉命令尾部的 env 转储并差分更新 ShellState（bash 路径专用）。"""
        idx = out.rfind("\n" + ENV_MARKER)
        if idx == -1:
            return out
        self.state.update_env(out[idx + len(ENV_MARKER) + 1:])
        return out[:idx]


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
        out = self._extract_cwd(self._split_env_dump(_decode(out_bytes or b"")))
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
