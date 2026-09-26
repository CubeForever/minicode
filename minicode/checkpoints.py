"""File checkpoints: snapshot before every mutating file tool, restore with /undo and /rewind."""
from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import List, Optional


class CheckpointManager:
    """Keeps flat-file backups under root; one entry per file mutation."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.entries: List[dict] = []

    def snapshot(self, path: Path, tool: str) -> None:
        path = Path(path)
        self.root.mkdir(parents=True, exist_ok=True)
        backup = None
        if path.exists():
            backup = self.root / f"{len(self.entries):04d}_{path.name}"
            try:
                shutil.copy2(path, backup)
            except OSError:
                backup = None
        self.entries.append({
            "time": time.strftime("%H:%M:%S"),
            "tool": tool,
            "path": str(path),
            "backup": str(backup) if backup else None,
        })

    def undo(self) -> Optional[dict]:
        if not self.entries:
            return None
        e = self.entries.pop()
        p = Path(e["path"])
        try:
            if e["backup"]:
                p.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(e["backup"], p)
            else:  # file did not exist before the tool ran
                p.unlink(missing_ok=True)
        except OSError:
            pass
        return e

    def list(self, n: int = 10) -> List[dict]:
        return self.entries[-n:]

    def rewind_to(self, shown_index: int, shown: List[dict]) -> List[dict]:
        """Undo everything after the chosen entry (shown is list(n()) as displayed)."""
        target = shown[shown_index]
        undone = []
        while self.entries and self.entries[-1] is not target:
            undone.append(self.undo())
        undone.append(self.undo())
        return undone

    def session_diff(self) -> List[tuple]:
        """(path, unified diff) for every file touched this session, vs its
        state before the first change. Files back to original are skipped."""
        import difflib
        first_by_path = {}
        for e in self.entries:
            first_by_path.setdefault(e["path"], e)
        outs = []
        for path, first in first_by_path.items():
            p = Path(path)
            try:
                old = Path(first["backup"]).read_text(encoding="utf-8",
                                                      errors="replace") \
                    if first["backup"] else None
            except OSError:
                old = None
            try:
                cur = p.read_text(encoding="utf-8", errors="replace") \
                    if p.exists() else None
            except OSError:
                cur = None
            if old == cur:
                continue
            label = path if len(path) < 50 else "…" + path[-49:]
            diff = difflib.unified_diff(
                (old or "").splitlines(), (cur or "").splitlines(),
                fromfile=f"{label} (before)", tofile=f"{label} (now)", lineterm="")
            outs.append((path, "\n".join(diff)))
        return outs


def prune_checkpoint_roots(base: Path, keep: int = 20) -> None:
    """~/.minicode/checkpoints/ 下每个会话一个目录，长期使用会无限累积；
    只保留最新的 keep 个（按修改时间）。"""
    try:
        roots = [d for d in base.iterdir() if d.is_dir()]
    except OSError:
        return
    roots.sort(key=lambda d: d.stat().st_mtime, reverse=True)
    for d in roots[keep:]:
        shutil.rmtree(d, ignore_errors=True)
