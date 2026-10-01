"""Post-edit lint fast loop (Aider-style): run the project's linter on files
just changed by write tools and feed failures back to the model so it fixes
them in place, instead of discovering them at test time.

Command resolution order:
    1. config `lint_command` (may contain a {files} placeholder)
    2. auto-detection: ruff (ruff.toml / [tool.ruff] in pyproject.toml) or
       eslint (package.json + eslint config) — only when the binary exists

This module never blocks a tool result: lint output is advisory feedback.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import List

LINT_TIMEOUT = 60
MAX_FEEDBACK_CHARS = 2000


def detect_lint_command(cwd: Path) -> str:
    """Best-effort zero-config detection. Returns a command template with
    {files} placeholder, or "" when nothing usable is found."""
    cwd = Path(cwd)
    if _has(cwd, ("ruff.toml", ".ruff.toml")) or _pyproject_has(cwd, "[tool.ruff]"):
        if shutil.which("ruff"):
            return "ruff check {files}"
    if (cwd / "package.json").is_file() and _has(
            cwd, (".eslintrc", ".eslintrc.js", ".eslintrc.json", ".eslintrc.yml",
                  ".eslintrc.yaml", "eslint.config.js", "eslint.config.mjs",
                  "eslint.config.ts")):
        if shutil.which("npx"):
            return "npx --no-install eslint {files}"
    return ""


def render_command(template: str, files: List[str], cwd: Path) -> str:
    """Fill the {files} placeholder with quoted paths relative to cwd
    (absolute when outside). No placeholder → template as-is (project-wide)."""
    if "{files}" not in template:
        return template
    if files:
        quoted = " ".join(_quote(str(_rel_or_abs(Path(f), cwd)).replace("\\", "/"))
                          for f in files)
    else:
        quoted = "."   # e.g. /lint with no session edits → lint everything
    return template.replace("{files}", quoted)


def run_lint(cwd: Path, command: str, timeout: int = LINT_TIMEOUT) -> str:
    """Run the lint command. Returns failure output ('' = passed)."""
    try:
        proc = subprocess.run(command, shell=True, cwd=str(cwd),
                              capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return f"(lint 超过 {timeout}s 未完成，已跳过)"
    except OSError as e:
        return f"(lint 无法执行：{e})"
    out = _decode(proc.stdout) + _decode(proc.stderr)
    if proc.returncode == 0:
        return ""
    if not out.strip():
        return f"(lint 退出码 {proc.returncode}，无输出)"
    return out.strip()[:MAX_FEEDBACK_CHARS]


def _decode(data: bytes) -> str:
    return (data or b"").decode("utf-8", errors="replace")


def _rel_or_abs(p: Path, cwd: Path) -> Path:
    try:
        return p.resolve().relative_to(Path(cwd).resolve())
    except (ValueError, OSError):
        return p


def _quote(s: str) -> str:
    return f'"{s}"' if (" " in s or "(" in s or ")" in s) else s


def _has(cwd: Path, names: tuple) -> bool:
    return any((cwd / n).exists() for n in names)


def _pyproject_has(cwd: Path, section: str) -> bool:
    p = cwd / "pyproject.toml"
    if not p.is_file():
        return False
    try:
        return section in p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
