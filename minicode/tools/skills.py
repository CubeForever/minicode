"""Skills system (borrowed from ZCode): reusable workflow instructions.

Skills live in (highest priority first):
    <project>/.minicode/skills/<name>/SKILL.md
    ~/.minicode/skills/<name>/SKILL.md
    minicode/builtin_skills/<name>/SKILL.md      (bundled)

The model loads them on demand via the ``skill`` tool; users can list them
with ``/skills``. Frontmatter: name, description.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .base import Tool, ToolContext, ToolError

BUILTIN_DIR = Path(__file__).parent.parent / "builtin_skills"
SKILL_FILE = "SKILL.md"
MAX_SKILL_CHARS = 12_000


def _skill_dirs(cwd) -> List[Path]:
    return [BUILTIN_DIR,
            Path.home() / ".minicode" / "skills",
            Path(cwd) / ".minicode" / "skills"]


def _parse(path: Path) -> Tuple[str, str, str]:
    """Returns (name, description, body)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return path.parent.name, "", ""
    name = path.parent.name
    desc, body = "", text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            for ln in parts[1].splitlines():
                if ln.lower().startswith("name:"):
                    name = ln.split(":", 1)[1].strip()
                elif ln.lower().startswith("description:"):
                    desc = ln.split(":", 1)[1].strip()
            body = parts[2]
    if not desc:
        for ln in body.splitlines():
            s = ln.strip()
            if s and not s.startswith("#"):
                desc = s[:80]
                break
    return name, desc, body.strip()


def skills_catalog(cwd) -> Dict[str, Tuple[str, str]]:
    """name -> (description, source_label), project overrides bundled."""
    catalog: Dict[str, Tuple[str, str]] = {}
    for label, d in zip(["内置", "用户", "项目"], _skill_dirs(cwd)):
        if not d.is_dir():
            continue
        for f in sorted(d.glob(f"*/{SKILL_FILE}")):
            name, desc, _ = _parse(f)
            if name:
                catalog[name] = (desc, label)
    return catalog


def skills_section_text(cwd) -> str:
    catalog = skills_catalog(cwd)
    if not catalog:
        return ""
    lines = ["Available workflow skills — when a task matches one, load it with "
             "the skill tool and follow it:"]
    for name, (desc, _src) in catalog.items():
        lines.append(f"- {name}: {desc}".rstrip())
    return "\n# Skills\n" + "\n".join(lines) + "\n"


def load_skill(cwd, name: str) -> Optional[str]:
    # project overrides user overrides builtin
    for d in reversed(_skill_dirs(cwd)):
        f = d / str(name) / SKILL_FILE
        if f.exists():
            _n, _d, body = _parse(f)
            return body[:MAX_SKILL_CHARS]
    return None


class SkillTool(Tool):
    name = "skill"
    kind = "read"
    description = ("Load a workflow skill's instructions into context (e.g. "
                   "systematic-debugging, test-driven-development, "
                   "verification-before-completion). Use when the current task "
                   "matches an available skill, before starting that work.")
    input_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Skill name (see /skills)."},
        },
        "required": ["name"],
    }

    def describe_call(self, args: dict) -> str:
        return str(args.get("name") or "")

    def run(self, args: dict, ctx: ToolContext) -> str:
        name = str(args.get("name") or "").strip()
        if not name:
            raise ToolError("name is required")
        catalog = skills_catalog(ctx.cwd)
        if name not in catalog:
            known = ", ".join(sorted(catalog)) or "(none)"
            raise ToolError(f"unknown skill {name!r}. Available: {known}")
        body = load_skill(ctx.cwd, name)
        if not body:
            raise ToolError(f"skill {name!r} is empty")
        return f"[skill: {name}]\n{body}\n[End of skill — follow it for this task.]"
