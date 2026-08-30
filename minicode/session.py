"""Conversation state: messages, todos, usage, persistence, compaction."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import List, Optional

from .llm import user_message

SESSIONS_DIR = Path.home() / ".minicode" / "sessions"
POINTER = Path.home() / ".minicode" / "LAST"

COMPACT_SYSTEM = ("You compress coding-agent conversations. Write dense, factual summaries. "
                  "Use the same language as the transcript.")

COMPACT_PROMPT = """Summarize the DIALOGUE FLOW of the following coding-agent session. A separate structured section (files, todos, commands) is preserved mechanically, so do NOT list files or commands — focus on:
1. The user's goals, constraints and decisions made along the way.
2. What was attempted and what the outcomes were (successes and failures).
3. Anything still unresolved or half-done.

Keep every concrete detail about intent and reasoning (not file lists). Do not add commentary.
{instructions}
Transcript:
---
{transcript}
---"""


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


class Session:
    def __init__(self, messages=None, todos=None):
        self.messages: List[dict] = list(messages or [])
        self.todos: List[dict] = list(todos or [])
        self.auto_approved: set = set()
        self.plan_allowed_rules: List[str] = []  # pre-approved by plan approval
        self.file_mtimes: dict = {}  # path -> mtime at last read (stale-edit detection)
        self.last_usage: Optional[dict] = None
        self.total_usage = {"input": 0, "output": 0}
        self.on_append = None  # callable(message) — used by stream-json output
        self.created = time.strftime("%Y-%m-%d %H:%M:%S")

    # ---------- history ----------
    def add(self, msg: dict):
        self.messages.append(msg)
        if self.on_append is not None:
            try:
                self.on_append(msg)
            except Exception:
                pass

    @staticmethod
    def _content_chars(content) -> int:
        if isinstance(content, str):
            return len(content)
        if isinstance(content, list):
            return sum(len(b.get("text", "")) for b in content
                       if isinstance(b, dict) and b.get("type") == "text")
        return 0

    def approx_tokens(self) -> int:
        chars = 0
        for m in self.messages:
            chars += self._content_chars(m.get("content"))
            for tc in m.get("tool_calls") or []:
                chars += len(tc.get("args") or "") + len(tc.get("name") or "")
        return chars // 3  # rough heuristic; real usage replaces it when available

    def note_usage(self, usage: Optional[dict]):
        if not usage:
            return
        self.last_usage = usage
        self.total_usage["input"] += usage.get("input") or 0
        self.total_usage["output"] += usage.get("output") or 0

    def context_tokens(self) -> int:
        return (self.last_usage or {}).get("input") or self.approx_tokens()

    # ---------- microcompaction ----------
    def elide_old_tool_results(self, keep_recent: int = 8, min_chars: int = 500) -> int:
        """Replace big old tool outputs with short markers (structure preserved),
        keeping the most recent ones verbatim. Returns number elided."""
        n = len(self.messages)
        changed = 0
        for m in self.messages[:max(0, n - keep_recent)]:
            if m.get("role") != "tool":
                continue
            content = m.get("content")
            if isinstance(content, str) and len(content) > min_chars:
                head = content[:180].replace("\n", " ⏎ ")
                m["content"] = f"[elided tool result: {len(content)} chars] {head}…"
                changed += 1
        return changed

    # ---------- compaction ----------
    def compact(self, provider, instructions: Optional[str] = None,
                carry_over: str = "") -> dict:
        transcript = self._transcript()
        prompt = COMPACT_PROMPT.format(
            instructions=f"\nExtra user instructions: {instructions}\n" if instructions else "",
            transcript=transcript)
        summary = provider.stream_text([user_message(prompt)], COMPACT_SYSTEM)
        structured = (carry_over.strip() + "\n\n") if carry_over.strip() else ""
        ctx = ("Below is structured carry-over state plus a dialogue summary. "
               "Continue seamlessly from where it left off.\n\n"
               + structured
               + "## Dialogue summary\n" + summary.strip())
        before = len(self.messages)
        self.messages = [user_message(ctx)]
        self.last_usage = None
        return {"before": before, "after": len(self.messages),
                "summary_chars": len(summary.strip())}

    def _transcript(self, cap_per_item: int = 900, cap_total: int = 60000) -> str:
        lines: List[str] = []
        for m in self.messages:
            role = m["role"]
            if role == "user":
                lines.append("User: " + (m.get("content") or "")[:cap_per_item])
            elif role == "assistant":
                if m.get("content"):
                    lines.append("Assistant: " + m["content"][:cap_per_item])
                for tc in m.get("tool_calls") or []:
                    lines.append(f"Assistant[{tc['name']}] args: "
                                 f"{(tc.get('args') or '')[:cap_per_item]}")
            elif role == "tool":
                content = m.get("content")
                if isinstance(content, list):
                    imgs = sum(1 for b in content if b.get("type") == "image")
                    body = "\n".join(b.get("text", "") for b in content
                                     if b.get("type") == "text")
                    if imgs:
                        body += f" [{imgs} image block(s) omitted]"
                else:
                    body = (content or "").replace("\n", " ⏎ ")
                lines.append(f"Tool[{m.get('name')}] -> {body[:cap_per_item]}")
        text = "\n".join(lines)
        if len(text) > cap_total:
            half = cap_total // 2
            text = text[:half] + "\n…[middle omitted]…\n" + text[-half:]
        return text

    def export_markdown(self, path: Path) -> Path:
        out = [f"# minicode 会话导出 — {self.created}", ""]
        for m in self.messages:
            role = m["role"]
            if role == "user":
                out += ["## 👤 User", "", m.get("content") or "", ""]
            elif role == "assistant":
                if m.get("content"):
                    out += ["## 🤖 Assistant", "", m["content"], ""]
                for tc in m.get("tool_calls") or []:
                    out += [f"**tool_call `{tc['name']}`**", "", "```json",
                            (tc.get("args") or "{}"), "```", ""]
            elif role == "tool":
                out += [f"**result `{m.get('name')}`**"
                        + (" *(error)*" if m.get("is_error") else ""), "",
                        "```", (m.get("content") or ""), "```", ""]
        path.write_text("\n".join(out), encoding="utf-8", newline="")
        return path

    # ---------- persistence ----------
    def to_dict(self) -> dict:
        return {"version": 1, "created": self.created, "messages": self.messages,
                "todos": self.todos, "total_usage": self.total_usage}

    def save(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False)

    @classmethod
    def load(cls, path: Path) -> "Session":
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        s = cls(data.get("messages") or [], data.get("todos") or [])
        s.total_usage = data.get("total_usage") or {"input": 0, "output": 0}
        s.created = data.get("created") or s.created
        return s

    def save_auto(self, first_user_text: str = "") -> Path:
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^\w\-]+", "-", (first_user_text or "session")[:40]).strip("-") or "session"
        path = SESSIONS_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}_{slug}.json"
        self.save(path)
        try:
            POINTER.write_text(str(path), encoding="utf-8")
        except OSError:
            pass
        return path

    @classmethod
    def load_last(cls) -> Optional["Session"]:
        try:
            p = Path(POINTER.read_text(encoding="utf-8").strip())
            # only trust pointers that actually point into the sessions dir
            if (p.parent.resolve() == SESSIONS_DIR.resolve() and p.suffix == ".json"
                    and p.exists()):
                return cls.load(p)
        except (OSError, ValueError):
            pass
        if SESSIONS_DIR.exists():
            files = sorted(SESSIONS_DIR.glob("*.json"), key=_mtime)
            if files:
                return cls.load(files[-1])
        return None

    @staticmethod
    def list_sessions(limit: int = 10) -> List[Path]:
        if not SESSIONS_DIR.exists():
            return []
        files = sorted(SESSIONS_DIR.glob("*.json"), key=_mtime, reverse=True)
        return files[:limit]
