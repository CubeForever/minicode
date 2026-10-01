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

COMPACT_PROMPT = """Summarize the DIALOGUE FLOW of the following coding-agent session. A separate structured section (files, todos, commands, tool-call digest) is preserved mechanically, so do NOT list files or commands — focus on:
1. The user's goals, constraints and decisions made along the way.
2. What was attempted and what the outcomes were (successes and failures).
3. Anything still unresolved or half-done.

Keep every concrete detail about intent and reasoning (not file lists). Do not add commentary.
{instructions}
Transcript:
---
{transcript}
---"""

# 压缩时保留原文的最近回合数（一个回合 = 一条用户消息及其后全部
# assistant/tool 消息）。更早的回合才进入 LLM 摘要。
COMPACT_KEEP_TURNS = 4

PREAMBLE_MARKER = "compact_preamble"


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
        self.chars_per_token: float = 3.0   # 真实 usage 校准后的 chars/token 比率
        self._turn_growth = 0.0             # 每回合输入增长的 EMA
        self._last_turn_input = 0           # 上一回合结束时的输入规模
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

    def content_chars(self) -> int:
        chars = 0
        for m in self.messages:
            chars += self._content_chars(m.get("content"))
            for tc in m.get("tool_calls") or []:
                chars += len(tc.get("args") or "") + len(tc.get("name") or "")
        return chars

    def approx_tokens(self) -> int:
        # 用真实 usage 持续校准的 chars/token 比率估算；中文会话比率
        # 接近 1.5–2，纯代码接近 3.5–4，固定值会差出数倍。
        return int(self.content_chars() / self.chars_per_token)

    def calibrate_usage(self, chars: int, usage: Optional[dict]):
        """一次模型调用后用真实 input tokens 校准估算比率（EMA）。

        chars 必须是这次调用发出前的消息字符量。input≤0 或 chars≤0
        时跳过；比率夹在 [1.2, 8] 防止异常响应带偏。"""
        if not usage or chars <= 0:
            return
        real = usage.get("input") or 0
        if real <= 0:
            return
        ratio = min(8.0, max(1.2, chars / real))
        self.chars_per_token = round(self.chars_per_token * 0.6 + ratio * 0.4, 3)

    def end_turn(self):
        """回合结束：记录输入规模，累计每回合增长 EMA（供剩余回合估算）。"""
        inp = (self.last_usage or {}).get("input") or 0
        if inp <= 0:
            return
        if self._last_turn_input > 0 and inp > self._last_turn_input:
            growth = inp - self._last_turn_input
            self._turn_growth = growth if not self._turn_growth \
                else self._turn_growth * 0.5 + growth * 0.5
        self._last_turn_input = inp

    def turns_remaining(self, context_limit: int) -> Optional[float]:
        """按每回合平均增长估算还能继续多少回合（None = 数据不足）。"""
        if not context_limit or self._turn_growth <= 0:
            return None
        inp = (self.last_usage or {}).get("input") or 0
        headroom = context_limit * 0.9 - inp   # 留 10% 给回复与压缩缓冲
        if headroom <= 0:
            return 0.0
        return max(0.0, headroom / self._turn_growth)

    def note_usage(self, usage: Optional[dict]):
        if not usage:
            return
        self.last_usage = usage
        self.total_usage["input"] += usage.get("input") or 0
        self.total_usage["output"] += usage.get("output") or 0
        if usage.get("cache_read"):
            self.total_usage["cache_read"] = (self.total_usage.get("cache_read", 0)
                                              + usage["cache_read"])
        if usage.get("cache_creation"):
            self.total_usage["cache_creation"] = (self.total_usage.get("cache_creation", 0)
                                                  + usage["cache_creation"])

    def context_tokens(self) -> int:
        return (self.last_usage or {}).get("input") or self.approx_tokens()

    def cache_stats(self) -> dict:
        """缓存命中统计（OpenAI 与 Anthropic 归一化后语义一致：
        input 为总输入，cache_read 是其中命中缓存的部分）。"""
        t = self.total_usage
        cached = t.get("cache_read") or 0
        total = t.get("input") or 0
        return {"cache_read": cached,
                "cache_creation": t.get("cache_creation") or 0,
                "hit_rate": (cached / total * 100) if total else 0.0}

    # ---------- microcompaction ----------

    # Tools whose results matter more for future edits keep more context.
    _HIGH_VALUE_TOOLS = {"write_file", "edit_file", "apply_patch", "notebook_edit"}
    _MEDIUM_VALUE_TOOLS = {"bash", "read_file", "web_fetch"}

    def _elide_budget(self, tool_name: str, is_error: bool) -> int:
        """How many chars to keep when eliding an old tool result."""
        if is_error:
            return 400  # errors always keep more (stack traces, diagnostics)
        if tool_name in self._HIGH_VALUE_TOOLS:
            return 800  # write/edit results tell the model what it changed
        if tool_name in self._MEDIUM_VALUE_TOOLS:
            return 300
        return 180  # read-only listings, search results, etc.

    def elide_old_tool_results(self, keep_recent: int = 8, min_chars: int = 500) -> int:
        """Replace big old tool outputs with short markers (structure preserved),
        keeping the most recent ones verbatim. Retention budget is tool-aware:
        write/edit results keep more than read-only listings. Returns number elided."""
        n = len(self.messages)
        changed = 0
        for m in self.messages[:max(0, n - keep_recent)]:
            if m.get("role") != "tool":
                continue
            content = m.get("content")
            if isinstance(content, str) and len(content) > min_chars:
                budget = self._elide_budget(m.get("name", ""), bool(m.get("is_error")))
                head = content[:budget].replace("\n", " ⏎ ")
                m["content"] = f"[elided tool result: {len(content)} chars] {head}…"
                changed += 1
        return changed

    # ---------- compaction ----------

    @staticmethod
    def _split_turns(messages: List[dict]) -> List[List[dict]]:
        """按用户消息切分回合：一个回合 = 一条 user 消息及其后全部
        assistant/tool 消息。只在 user 边界切分，tool_use/tool_result
        配对永远不会被拆散。"""
        turns: List[List[dict]] = []
        for m in messages:
            if m.get("role") == "user" or not turns:
                turns.append([m])
            else:
                turns[-1].append(m)
        return turns

    @staticmethod
    def _turn_digest(msgs: List[dict], cap: int = 60) -> str:
        """被压缩回合的机械骨架：每次工具调用一行（工具名 + 参数摘要 +
        结果首行）。让模型在 LLM 摘要之外仍能回溯"早期做过什么"。"""
        lines: List[str] = []
        pending: dict = {}  # tool_call_id -> (name, args 摘要)
        for m in msgs:
            for tc in m.get("tool_calls") or []:
                try:
                    args = json.loads(tc.get("args") or "{}")
                except json.JSONDecodeError:
                    args = None
                if isinstance(args, dict):
                    brief = ", ".join(f"{k}={str(v)[:60]}"
                                      for k, v in list(args.items())[:2])
                else:
                    brief = str(args or tc.get("args") or "")[:60]
                pending[tc.get("id") or ""] = (tc.get("name") or "?", brief)
            if m.get("role") == "tool":
                name, brief = pending.get(m.get("tool_call_id") or "",
                                          (m.get("name") or "?", ""))
                content = m.get("content")
                first = content if isinstance(content, str) else ""
                first = " ".join(first.split())[:100]
                line = f"- {name}({brief})"
                if first:
                    line += f" -> {first}"
                lines.append(line)
        if len(lines) > cap:
            lines = lines[:cap] + [f"- …（其余 {len(lines) - cap} 条工具调用略）"]
        return "\n".join(lines)

    def compact(self, provider, instructions: Optional[str] = None,
                carry_over: str = "", keep_recent_turns: Optional[int] = None) -> dict:
        """分层压缩（对标 Claude Code compaction）：

        1. 最近 keep_recent_turns 个回合**逐字保留**（thinking、工具配对
           结构原样，模型可无损引用最近上下文）；
        2. 更早回合交给 LLM 出对话摘要；
        3. 同时机械提取早期工具调用骨架（_turn_digest），与结构化
           carry-over 一起放进开头的 preamble 用户消息。

        与旧版"整段历史替换为单条消息"相比，保留了 tool_use/tool_result
        配对与 thinking——推理模型（GLM/DeepSeek/Claude thinking）在压缩
        后不会因结构断裂而报错或质量骤降。
        """
        keep = COMPACT_KEEP_TURNS if keep_recent_turns is None \
            else max(1, keep_recent_turns)
        msgs = self.messages
        prior = ""
        if msgs and msgs[0].get(PREAMBLE_MARKER):   # 已压缩过：旧 preamble 并入转录
            prior = msgs[0].get("content") or ""
            msgs = msgs[1:]
        turns = self._split_turns(msgs)
        older, recent = turns[:-keep], turns[-keep:]
        older_msgs = ([{"role": "user", "content": prior}] if prior else []) \
            + [m for t in older for m in t]

        summary = ""
        if older_msgs:
            prompt = COMPACT_PROMPT.format(
                instructions=f"\nExtra user instructions: {instructions}\n"
                if instructions else "",
                transcript=self._transcript(older_msgs))
            summary = provider.stream_text([user_message(prompt)],
                                           COMPACT_SYSTEM).strip()
        digest = self._turn_digest(older_msgs) if older_msgs else ""

        parts = []
        if carry_over.strip():
            parts.append(carry_over.strip())
        if digest:
            parts.append("## 早期操作摘要\n" + digest)
        if summary:
            parts.append("## 对话摘要\n" + summary)
        before = len(self.messages)
        new_messages: List[dict] = []
        if parts:
            preamble = user_message(
                "以下是此前会话的结构化状态与摘要。请从最近的对话无缝继续。\n\n"
                + "\n\n".join(parts))
            preamble[PREAMBLE_MARKER] = True
            new_messages.append(preamble)
        new_messages.extend(m for t in recent for m in t)
        self.messages = new_messages
        self.last_usage = None
        return {"before": before, "after": len(self.messages),
                "summary_chars": len(summary), "digest_chars": len(digest),
                "kept_turns": len(recent), "dropped_turns": len(older)}

    def _transcript(self, msgs: Optional[List[dict]] = None,
                    cap_per_item: int = 900, cap_total: int = 60000) -> str:
        lines: List[str] = []
        for m in (msgs if msgs is not None else self.messages):
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
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write("\n".join(out))
        return path

    # ---------- persistence ----------
    def to_dict(self) -> dict:
        return {"version": 1, "created": self.created, "messages": self.messages,
                "todos": self.todos, "total_usage": self.total_usage,
                "chars_per_token": self.chars_per_token}

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
        try:
            s.chars_per_token = min(8.0, max(1.2, float(data.get("chars_per_token"))))
        except (TypeError, ValueError):
            pass
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
