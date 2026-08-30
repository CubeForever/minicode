"""Sensitive-path guard: forced confirmation for critical assets.

Unlike permission rules, the guard fires even in yolo mode (forced confirm;
auto-declined in non-interactive sessions). Reading sensitive files stays
allowed — only writes/deletes are gated.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

SENSITIVE_FILE_PATTERNS = [
    r"\.env(\..+)?$", r"\.pem$", r"\.key$", r"\.p12$", r"\.pfx$",
    r"id_rsa.*", r"id_ed25519.*", r"id_ecdsa.*",
    r"credentials\.json$", r"\.npmrc$", r"\.netrc$", r"\.git-credentials$",
    r"secrets?\.(json|ya?ml|txt|toml)$",
]
SENSITIVE_DIRS = (".git", ".ssh", ".gnupg", ".minicode/secrets")

BASH_SENSITIVE = re.compile(
    r"(\.git\b|\.env\b|id_rsa|\.ssh\b|\.gnupg\b|\.pem\b|\.key\b|credentials)", re.I)
BASH_DESTRUCTIVE = re.compile(
    r"(\brm\b|\bdel\b|\brd\b|\bmv\b|\btruncate\b|Remove-Item|>>?|"
    r"git\s+(push|reset|clean|checkout|restore|rm)|\bformat\b|\bmkfs\b)", re.I)


def file_guard_reason(path: Path, cwd: Path) -> Optional[str]:
    """Return a human-readable reason when `path` is a protected asset."""
    try:
        rel = Path(path).resolve().relative_to(Path(cwd).resolve())
        parts = rel.parts
    except (ValueError, OSError):
        return "工作目录之外的路径"
    if not parts:
        return None
    posix = rel.as_posix()
    for d in SENSITIVE_DIRS:
        if posix == d or posix.startswith(d + "/"):
            return f"受保护目录 ./{'/'.join(posix.split('/')[:2])}"
    name = parts[-1].lower()
    for pat in SENSITIVE_FILE_PATTERNS:
        if re.search(pat, name):
            return f"敏感文件 ./{posix}"
    return None


# benign redirections that must NOT count as file writes: 2>&1, 1>&2, 2>/dev/null …
_BENIGN_REDIRECTS = re.compile(r"\d*>>?\s*/dev/null|\d*\s*>&\s*\d+")


def bash_guard_reason(command: str) -> Optional[str]:
    """Gate destructive shell commands that touch sensitive paths.
    Reading stays allowed (cat .env), and benign fd redirections
    (2>&1, 2>/dev/null) are not treated as writes."""
    if not BASH_SENSITIVE.search(command or ""):
        return None
    benign = _BENIGN_REDIRECTS.sub("", command)
    if not BASH_DESTRUCTIVE.search(benign):
        return None
    token = BASH_SENSITIVE.search(command).group(0)
    return f"命令同时涉及敏感路径（{token}）和写/删操作"
