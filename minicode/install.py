"""minicode install — 扩展包分发（零依赖）。

把一个「扩展包」（git 仓库或本地目录）安装进项目 ``.minicode/`` 或用户
``~/.minicode/``。包内按约定目录发现扩展，也可用可选的 ``minicode.json``
manifest 显式声明：

    {
      "name": "my-pack",
      "version": "1.0.0",
      "description": "…",
      "skills":   ["skills/review/SKILL.md"],   # 可选；缺省按约定目录自动发现
      "commands": ["commands/deploy.md"],
      "agents":   ["agents/architect.md"],
      "tools":    ["tools/db.py"]
    }

约定目录：``skills/``（子目录含 SKILL.md，或单个 .md 自动包装成技能目录）、
``commands/*.md``、``agents/*.md``、``tools/*.py``。

安全边界：tools/*.py 与技能/命令内容是任意指令——安装只复制文件，真正的
执行闸门沿用既有信任体系（插件首次加载强制确认、项目受限配置信任门禁）。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, List

PACK_MANIFEST = "minicode.json"
_KINDS = ("skills", "commands", "agents", "tools")
_GIT_HINTS = ("http://", "https://", "git@", "ssh://")


class InstallError(RuntimeError):
    pass


def _looks_like_git(source: str) -> bool:
    return source.startswith(_GIT_HINTS) or source.endswith(".git")


def _resolve_source(source: str) -> tuple:
    """返回 (pack_dir, temp_dir_or_None)。本地目录直接用；git URL 浅克隆到
    临时目录（temp_dir 由调用方负责清理——绝不清理用户自己的目录）。"""
    p = Path(source).expanduser()
    if p.exists():
        if not p.is_dir():
            raise InstallError(f"{source} 不是目录（扩展包需为目录或 git 仓库）")
        return p, None
    if not _looks_like_git(source):
        raise InstallError(f"源不存在，也不是 git 地址：{source}")
    if shutil.which("git") is None:
        raise InstallError("未找到 git——安装 git 仓库扩展包需要 git")
    tmp = Path(tempfile.mkdtemp(prefix="minicode-install-"))
    r = subprocess.run(["git", "clone", "--depth", "1", source, str(tmp / "pack")],
                       capture_output=True, timeout=180)
    if r.returncode != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        detail = (r.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise InstallError("git clone 失败：" + (detail[-1][:200] if detail else "未知错误"))
    return tmp / "pack", tmp


def _read_manifest(pack: Path) -> dict:
    f = pack / PACK_MANIFEST
    if not f.is_file():
        return {}
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def discover(pack: Path) -> Dict[str, list]:
    """返回 {kind: [源路径]}。manifest 显式列表优先，缺省按约定目录发现。"""
    manifest = _read_manifest(pack)
    out: Dict[str, list] = {}
    for kind in _KINDS:
        found: List[Path] = []
        seen = set()
        for rel in manifest.get(kind) or []:
            p = (pack / str(rel)).resolve()
            if kind == "skills" and p.is_file() and p.name == "SKILL.md":
                p = p.parent   # 指向 SKILL.md 的条目按技能目录安装
            if p.exists() and p not in seen:
                found.append(p)
                seen.add(p)
        if not found:
            d = pack / kind
            if d.is_dir():
                if kind == "skills":
                    for sub in sorted(d.iterdir()):
                        if sub.is_dir() and (sub / "SKILL.md").is_file():
                            found.append(sub)
                        elif sub.is_file() and sub.suffix == ".md":
                            found.append(sub)   # 单文件技能 → 安装时包装成目录
                else:
                    pattern = "*.py" if kind == "tools" else "*.md"
                    found.extend(sorted(d.glob(pattern)))
        out[kind] = found
    return out


def _copy_skill(src: Path, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dest_dir, dirs_exist_ok=True)
    else:
        shutil.copy2(src, dest_dir / "SKILL.md")
    return dest_dir


def install_pack(source: str, project_cwd: Path, user: bool = False,
                 force: bool = False) -> dict:
    """安装扩展包。返回 {installed, skipped, warnings, manifest}。"""
    pack, temp_dir = _resolve_source(source)
    try:
        base = (Path.home() / ".minicode") if user else (Path(project_cwd) / ".minicode")
        manifest = _read_manifest(pack)
        result = {"source": source,
                  "pack": manifest.get("name") or pack.name,
                  "version": manifest.get("version", ""),
                  "installed": [], "skipped": [], "warnings": []}
        disc = discover(pack)

        for src in disc["skills"]:
            name = re.sub(r"[^\w\-.]+", "-", src.stem if src.is_file() else src.name)
            dest = base / "skills" / name
            if dest.exists() and not force:
                result["skipped"].append(f"skill:{name}（已存在，--force 覆盖）")
                continue
            if dest.exists():
                shutil.rmtree(dest)
            _copy_skill(src, dest)
            result["installed"].append(f"skill:{name}")

        for kind in ("commands", "agents"):
            for src in disc[kind]:
                dest = base / kind / f"{src.stem}.md"
                if dest.exists() and not force:
                    result["skipped"].append(f"{kind[:-1]}:{src.stem}（已存在，--force 覆盖）")
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
                result["installed"].append(f"{kind[:-1]}:{src.stem}")

        for src in disc["tools"]:
            dest = base / "tools" / src.name
            if dest.exists() and not force:
                result["skipped"].append(f"tool:{src.stem}（已存在，--force 覆盖）")
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            result["installed"].append(f"tool:{src.stem}")

        if any(i.startswith("tool:") for i in result["installed"]):
            result["warnings"].append(
                "插件 .py 是任意代码：首次加载时会要求你确认（信任按内容指纹记录）")
        if manifest.get("mcpServers"):
            result["warnings"].append(
                "manifest 携带 mcpServers——出于安全考虑不会自动安装，请手动复制到 .minicode.json 后经信任门禁确认")
        if not result["installed"] and not result["skipped"]:
            result["warnings"].append("包内没有发现任何扩展（需要 skills/commands/agents/tools 目录或 minicode.json）")
        return result
    finally:
        if temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)


def format_report(result: dict) -> str:
    lines = []
    title = result.get("pack") or result.get("source", "")
    version = result.get("version")
    head = f"已安装扩展包：{title}" + (f" v{version}" if version else "")
    lines.append(head)
    for i in result.get("installed") or []:
        lines.append(f"  ✓ {i}")
    for s in result.get("skipped") or []:
        lines.append(f"  ○ 跳过 {s}")
    for w in result.get("warnings") or []:
        lines.append(f"  ⚠ {w}")
    if not (result.get("installed") or result.get("skipped")):
        lines.append("  （什么都没装）")
    return "\n".join(lines)
