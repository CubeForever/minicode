"""Skills system (borrowed from ZCode): reusable workflow instructions.

Skills live in (highest priority first):
    <project>/.minicode/skills/<name>/SKILL.md
    ~/.minicode/skills/<name>/SKILL.md
    minicode/builtin_skills/<name>/SKILL.md      (bundled)

The model loads them on demand via the ``skill`` tool; users can list them
with ``/skills``. Frontmatter: name, description.

经验引擎（v0.22）：
    - auto-<name>/ 单层前缀目录 = 智能体自生成技能（maybe_generate_skill），
      复用既有单层 glob 发现，不需要新发现层
    - .hits.json 记录每个技能的加载次数与最近使用（load_skill 时落笔）
    - 自生成技能 30 天零命中即从目录清单剔除（archive_stale_autoskills
      移入 archive/，单层 glob 天然不可见，可随时手工捞回）
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .base import Tool, ToolContext, ToolError
from ..llm import user_message

BUILTIN_DIR = Path(__file__).parent.parent / "builtin_skills"
SKILL_FILE = "SKILL.md"
MAX_SKILL_CHARS = 12_000
AUTO_PREFIX = "auto-"
HITS_FILE = ".hits.json"
STALE_DAYS = 30

AUTOSKILL_SYSTEM = ("You distill reusable workflow skills from coding-agent "
                    "session transcripts. Output ONLY the raw SKILL.md file "
                    "content (frontmatter + markdown body), nothing else.")


def _skill_dirs(cwd) -> List[Path]:
    return [BUILTIN_DIR,
            Path.home() / ".minicode" / "skills",
            Path(cwd) / ".minicode" / "skills"]


def _project_skills_dir(cwd) -> Path:
    return Path(cwd) / ".minicode" / "skills"


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


def _hits(cwd) -> Dict[str, dict]:
    p = _project_skills_dir(cwd) / HITS_FILE
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _record_hit(cwd, name: str) -> None:
    try:
        data = _hits(cwd)
        entry = data.get(name) or {"hits": 0}
        entry["hits"] = int(entry.get("hits") or 0) + 1
        entry["last_used"] = time.strftime("%Y-%m-%d")
        data[name] = entry
        p = _project_skills_dir(cwd) / HITS_FILE
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                     encoding="utf-8")
    except (OSError, TypeError, ValueError):
        pass   # 遥测绝不影响主流程


def _is_stale_auto(cwd, skill_dir: Path) -> bool:
    """自生成技能：目录名 auto-* 且 30 天零命中。"""
    if not skill_dir.parent.name == "skills" or \
            not skill_dir.name.startswith(AUTO_PREFIX):
        return False
    if _hits(cwd).get(skill_dir.name, {}).get("hits"):
        return False
    try:
        age = time.time() - skill_dir.stat().st_mtime
    except OSError:
        return False
    return age > STALE_DAYS * 86400


def archive_stale_autoskills(cwd) -> int:
    """把过期未命中的自生成技能移入 archive/（单层 glob 不可见）。/skills 触发。"""
    d = _project_skills_dir(cwd)
    if not d.is_dir():
        return 0
    moved = 0
    for skill_dir in sorted(d.glob(f"{AUTO_PREFIX}*")):
        if not (skill_dir / SKILL_FILE).exists() or not _is_stale_auto(cwd, skill_dir):
            continue
        archive = d / "archive"
        try:
            archive.mkdir(exist_ok=True)
            skill_dir.rename(archive / skill_dir.name)
            moved += 1
        except OSError:
            pass
    return moved


def skills_catalog(cwd) -> Dict[str, Tuple[str, str]]:
    """name -> (description, source_label), project overrides bundled.
    过期未命中的自生成技能不出现在清单里（移动由 /skills 显式触发）。"""
    catalog: Dict[str, Tuple[str, str]] = {}
    for label, d in zip(["内置", "用户", "项目"], _skill_dirs(cwd)):
        if not d.is_dir():
            continue
        for f in sorted(d.glob(f"*/{SKILL_FILE}")):
            if label == "项目" and _is_stale_auto(cwd, f.parent):
                continue
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
            _record_hit(cwd, str(name))
            return body[:MAX_SKILL_CHARS]
    return None


def should_autoskill(files_changed: int, turn_errors: int,
                     verify_ok: Optional[bool]) -> bool:
    """经验引擎触发门（纯函数，便于测试）。

    跨多文件的成功回合 + (自检通过 或 经历过试错) 才值得沉淀——
    单文件顺手改 / 一把过的小任务提炼不出可复用流程。
    """
    if files_changed < 2:
        return False
    return bool(verify_ok) or turn_errors >= 2


def maybe_generate_skill(provider, cwd, transcript: str,
                         source_hint: str = "") -> Tuple[Optional[Path], bool]:
    """从回合转录提炼可复用技能 → .minicode/skills/auto-<name>/SKILL.md。

    Returns (path, created)。失败/重名一律静默降级（created=False）——
    这是附加能力，绝不能影响主回合。调用方负责触发门（should_autoskill）。
    """
    prompt = (
        "Below is the transcript of one coding-agent turn that ended in "
        "success. Distill a REUSABLE workflow skill from it — the repeatable "
        "approach, not the specific file names. Skip it and output nothing "
        "usable if there is nothing generalizable.\n\n"
        "Format (frontmatter required):\n"
        "---\nname: <short-kebab-name>\ndescription: <one line: when to load "
        "this skill>\n---\n<body: numbered steps, commands, verification>\n\n"
        + (f"Project: {source_hint}\n" if source_hint else "")
        + "Transcript:\n---\n" + transcript[:24000] + "\n---")
    try:
        raw = provider.stream_text([user_message(prompt)], AUTOSKILL_SYSTEM)
    except Exception:
        return None, False
    return write_autoskill(cwd, raw)


def write_autoskill(cwd, raw: str) -> Tuple[Optional[Path], bool]:
    """把模型输出的 SKILL.md 内容落盘（重名 → created=False）。"""
    name, desc, body = "", "", raw or ""
    if body.startswith("---"):
        parts = body.split("---", 2)
        if len(parts) == 3:
            for ln in parts[1].splitlines():
                if ln.lower().startswith("name:"):
                    name = ln.split(":", 1)[1].strip()
                elif ln.lower().startswith("description:"):
                    desc = ln.split(":", 1)[1].strip()
            body = parts[2]
    slug = re.sub(r"[^a-z0-9\-]+", "-", name.lower()).strip("-")[:40]
    if not slug or len(body.strip()) < 30:
        return None, False   # 无效输出：不生成
    if not desc:
        for ln in body.splitlines():
            s = ln.strip()
            if s and not s.startswith("#"):
                desc = s[:80]
                break
    target = _project_skills_dir(cwd) / (AUTO_PREFIX + slug)
    if (target / SKILL_FILE).exists():
        return target, False   # 已存在：不覆盖，视为沉淀过
    # frontmatter name 必须与目录名一致（auto- 前缀）：catalog 键、
    # load_skill 的目录定位、.hits.json 遥测键、归档判定键四处同源。
    # 此前 name=slug 与目录 auto-<slug> 错位，导致技能加载失败 + 命中
    # 永远查不到（30 天后重度使用也会被误归档）——v0.22.1 修复。
    try:
        target.mkdir(parents=True, exist_ok=True)
        front = (f"---\nname: {AUTO_PREFIX + slug}\ndescription: "
                 f"{(desc or slug)[:160]}\n---\n")
        (target / SKILL_FILE).write_text(front + body.strip() + "\n",
                                         encoding="utf-8")
    except OSError:
        return None, False
    return target, True


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
