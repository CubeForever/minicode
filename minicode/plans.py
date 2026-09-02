"""Plan persistence: approved plans are archived under .minicode/plans/
and the latest active plan is injected into every session's system prompt."""
from __future__ import annotations

import time
from pathlib import Path
from typing import List, Optional, Tuple

STATUSES = ("in-progress", "done")


def plans_dir(cwd) -> Path:
    return Path(cwd) / ".minicode" / "plans"


def save_plan(cwd, text: str) -> Path:
    text = (text or "").strip() or "(empty plan)"
    slug = "".join(c if c.isalnum() else "-" for c in text.splitlines()[0][:32]).strip("-")
    slug = slug or "plan"
    p = plans_dir(cwd) / f"{time.strftime('%Y%m%d-%H%M%S')}_{slug}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    body = (f"---\nstatus: in-progress\ncreated: {time.strftime('%Y-%m-%d %H:%M')}\n---\n"
            + text + "\n")
    # Path.write_text(newline=) 需要 3.10+；用 open() 保持 3.9 兼容
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(body)
    return p


def _parse(path: Path) -> Tuple[dict, str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}, ""
    meta, body = {}, text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            for ln in parts[1].splitlines():
                if ":" in ln:
                    k, _, v = ln.partition(":")
                    meta[k.strip()] = v.strip()
            body = parts[2]
    return meta, body


def list_plans(cwd) -> List[Tuple[int, Path, str, str]]:
    """(index, path, status, first content line), newest first."""
    d = plans_dir(cwd)
    if not d.is_dir():
        return []
    rows = []
    for p in sorted(d.glob("*.md"), reverse=True):
        meta, body = _parse(p)
        title = next((ln.strip() for ln in body.splitlines() if ln.strip()), "(空)")
        rows.append((p, meta.get("status", "in-progress"), title[:70]))
    return [(i, p, s, t) for i, (p, s, t) in enumerate(rows, 1)]


def latest_active_plan(cwd) -> Optional[str]:
    d = plans_dir(cwd)
    if not d.is_dir():
        return None
    for p in sorted(d.glob("*.md"), reverse=True):
        meta, body = _parse(p)
        if meta.get("status", "in-progress") == "in-progress":
            return body.strip()
    return None


def mark_plan(cwd, index: int, status: str) -> Optional[Path]:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    for i, p, _s, _t in list_plans(cwd):
        if i == index:
            meta, body = _parse(p)
            meta["status"] = status
            fm = "\n".join(f"{k}: {v}" for k, v in meta.items())
            with open(p, "w", encoding="utf-8", newline="") as f:
                f.write(f"---\n{fm}\n---\n{body}")
            return p
    return None
