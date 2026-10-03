"""Project Brain — the agent's self-maintained, cross-session memory.

Stored at ``<project>/.minicode/BRAIN.md``. The model records durable
knowledge (facts / gotchas / decisions / failed approaches) via the
``brain_write`` tool; every later session inherits it through the system
prompt. Users can edit the file freely — it is re-parsed each load.

v0.22 起 brain 注入按 section 配额均衡截取（裸截断会整段丢掉尾部），
被省略的条目可通过 ``brain_search`` 工具按关键词检索（渐进披露：
system 段保持稳定以维持 prompt 缓存，全量记忆按需获取）。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Tuple

from .base import Tool, ToolContext, ToolError

BRAIN_HEADER = ("# Project Brain\n\n"
                "Auto-maintained by minicode. Durable knowledge that every future "
                "session inherits. Edit freely.\n")
SECTION_BY_KIND = {
    "fact": "Facts",            # how things work / commands / conventions
    "gotcha": "Gotchas",        # pitfalls discovered the hard way
    "decision": "Decisions",    # choices made and why
    "failed": "Failed approaches",  # what was tried and did not work
}
SECTION_ORDER = list(SECTION_BY_KIND.values())
MAX_PER_SECTION = 50


def brain_path(cwd) -> Path:
    return Path(cwd) / ".minicode" / "BRAIN.md"


def load_brain_text(cwd) -> str:
    p = brain_path(cwd)
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _parse(text: str) -> Dict[str, List[str]]:
    sections: Dict[str, List[str]] = {}
    cur = None
    for ln in text.splitlines():
        if ln.startswith("## "):
            cur = ln[3:].strip()
            sections.setdefault(cur, [])
        elif ln.startswith("- ") and cur:
            sections[cur].append(ln[2:].strip())
    return sections


def render_brain(cwd, cap: int = 4000) -> str:
    """按 section 配额均衡渲染（v0.22）。

    旧版 ``text[:cap]`` 裸截断：Facts 一节超长时 Gotchas/Decisions 整段
    消失。现在每个 section 平分配额，超出部分计数并在结尾提示用
    brain_search 检索——system 注入保持有界（prompt 缓存友好），
    召回率交给检索工具。
    """
    text = load_brain_text(cwd).strip()
    if not text:
        return ""
    if len(text) <= cap:
        return text
    sections = _parse(text)
    active = [name for name in SECTION_ORDER if sections.get(name)]
    if not active:
        return text[:cap]
    per = max(300, cap // len(active))
    out: List[str] = []
    omitted = 0
    for name in active:
        items = sections[name]
        out.append(f"## {name}")
        used = 0
        kept = 0
        for it in items:
            line = f"- {it}"
            if kept and used + len(line) > per:
                omitted += len(items) - kept
                break
            out.append(line)
            used += len(line)
            kept += 1
    if omitted:
        out.append(f"(brain 另有 {omitted} 条未展示——"
                   "用 brain_search 工具按关键词检索全部经验)")
    result = "\n".join(out)
    return result[:cap] if len(result) > cap else result


def _terms(query: str) -> set:
    """零依赖检索分词：英文按 [a-z0-9_]，中文按 bigram（单字兜底）。"""
    q = (query or "").lower()
    en = set(re.findall(r"[a-z0-9_]{2,}", q))
    cjk = re.findall(r"[\u4e00-\u9fff]", q)
    if len(cjk) > 1:
        en.update(cjk[i] + cjk[i + 1] for i in range(len(cjk) - 1))
    elif cjk:
        en.update(cjk)
    return en


def search_brain_text(cwd, query: str, limit: int = 8) -> str:
    """按关键词检索全部 brain 条目，返回打分最高的若干条。"""
    text = load_brain_text(cwd)
    if not text.strip():
        return "brain 为空（用 brain_write 沉淀第一条经验）。"
    terms = _terms(query)
    if not terms:
        return "查询为空——请给出关键词。"
    scored = []
    for section, items in _parse(text).items():
        for it in items:
            hay = (it + " " + section).lower()
            score = sum(1 for t in terms if t in hay)
            if score > 0:
                scored.append((score, section, it))
    if not scored:
        return f"brain 中没有匹配 {query!r} 的条目。"
    scored.sort(key=lambda t: (-t[0], t[2]))
    # 来源标注：检索路径与 system 注入路径同等是"过去 agent 写入的文本"，
    # 注入面提示不能只存在于 prompts.py 一处（v0.22.1 审查采纳）。
    lines = ["[brain_search] 以下条目来自 BRAIN.md 的历史沉淀，可能过时或有误，"
             "采信前先验证。"]
    lines += [f"[{sec}] {it[:200]}" for _s, sec, it in scored[:limit]]
    if len(scored) > limit:
        lines.append(f"(另 {len(scored) - limit} 条匹配未展示)")
    return "\n".join(lines)


class BrainSearchTool(Tool):
    name = "brain_search"
    kind = "read"
    description = (
        "Search the project brain (.minicode/BRAIN.md) for past facts, gotchas, "
        "decisions and failed approaches by keyword. Use it when the injected "
        "brain excerpt seems incomplete, or before repeating work the project "
        "may have done before.")
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string",
                      "description": "Keywords, e.g. 'deploy timeout' or '部署 超时'."},
        },
        "required": ["query"],
    }

    def describe_call(self, args: dict) -> str:
        return str(args.get("query") or "")

    def run(self, args: dict, ctx: ToolContext) -> str:
        return search_brain_text(ctx.cwd, str(args.get("query") or ""))


def append_brain(cwd, kind: str, content: str) -> Tuple[bool, str]:
    """Append a bullet under the section for `kind`. Returns (added, message)."""
    if kind not in SECTION_BY_KIND:
        raise ToolError(f"kind must be one of: {', '.join(SECTION_BY_KIND)}")
    content = " ".join(str(content).split())
    if not content:
        raise ToolError("content is required")
    sections = _parse(load_brain_text(cwd))
    name = SECTION_BY_KIND[kind]
    items = sections.setdefault(name, [])
    if content in items:
        return False, "already recorded in the brain"
    if len(items) >= MAX_PER_SECTION:
        items.pop(0)  # keep the section bounded; oldest facts rotate out first
    items.append(content)

    out = [BRAIN_HEADER.rstrip(), ""]
    for sec in SECTION_ORDER:
        if sections.get(sec):
            out.append(f"## {sec}")
            out.extend(f"- {i}" for i in sections[sec])
            out.append("")
    # keep any custom sections the user added
    for sec, items_extra in sections.items():
        if sec in SECTION_ORDER or not items_extra:
            continue
        out.append(f"## {sec}")
        out.extend(f"- {i}" for i in items_extra)
        out.append("")
    p = brain_path(cwd)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write("\n".join(out))
    return True, f"recorded under {name}"


class BrainWriteTool(Tool):
    name = "brain_write"
    # write 而非 meta：大脑内容会注入后续所有会话的系统提示，是持久化写入，
    # 不能静默自动批准（提示注入可能借它跨会话存活）。
    kind = "write"
    description = ("Record a durable fact about this project into the project brain "
                   "(.minicode/BRAIN.md) so every future session inherits it. Kinds: "
                   "fact (how it works, commands, conventions), gotcha (pitfall), "
                   "decision (choice + why), failed (approach that did not work). "
                   "Do not record transient details.")
    input_schema = {
        "type": "object",
        "properties": {
            "kind": {"type": "string",
                     "enum": list(SECTION_BY_KIND),
                     "description": "Category of the note."},
            "content": {"type": "string", "description": "One concise sentence."},
        },
        "required": ["kind", "content"],
    }

    def describe_call(self, args: dict) -> str:
        return f"[{args.get('kind')}] {' '.join(str(args.get('content') or '').split())[:80]}"

    def mutated_path(self, args: dict, ctx: ToolContext):
        return brain_path(ctx.cwd)

    def run(self, args: dict, ctx: ToolContext) -> str:
        added, msg = append_brain(ctx.cwd, str(args.get("kind") or ""),
                                  str(args.get("content") or ""))
        return msg if not added else f"{msg} — visible to future sessions"
