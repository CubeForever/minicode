"""Spec 驱动模式(v0.23):需求契约 + 验收标准,[auto]/[manual] 第一天分流。

spec 存于 .minicode/specs/<id>.md:

    ---
    id: user-auth
    title: 用户登录
    status: draft            # draft | done
    ---
    # 需求
    支持用户名+密码登录,失败锁定 5 次。
    # 验收标准
    - [auto] `{python} -m pytest tests/test_auth.py -q` 全绿
    - [manual] 移动端登录页可用

硬约束(与审查共识一致):
- [auto] = 机器可判定(命令/断言),[manual] = 人工过;分类在起草时完成,
  不做事后猜测;
- **to-eval 只对存在 [auto] 的 spec 生效**——入口判定,"转化按钮"亮不亮
  在调用前就知道(预防,而非转化后过滤);
- [auto] 里只有含反引号命令的条目可转为 eval 校验;其余 [auto](如断言
  文件存在)作为任务要求写进 instruction。
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

AUTO_TAG = "[auto]"
MANUAL_TAG = "[manual]"
_CRITERIA_RX = re.compile(r"^-\s*\[(auto|manual)\]\s*(.+)$", re.I)
_CMD_RX = re.compile(r"`([^`]+)`")


def specs_dir(cwd) -> Path:
    return Path(cwd) / ".minicode" / "specs"


def spec_path(cwd, sid: str) -> Path:
    return specs_dir(cwd) / f"{sid}.md"


def parse_spec(path: Path) -> Tuple[Dict[str, str], str, List[Tuple[str, str]]]:
    """Returns (meta, requirements_text, criteria)。

    criteria = [(tag, text)],tag 归一为小写 'auto'/'manual';无标签的
    验收行归入 'manual'(保守:不可判定就不当 auto)。
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}, "", []
    meta: Dict[str, str] = {}
    body = text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            for ln in parts[1].splitlines():
                if ":" in ln:
                    k, _, v = ln.partition(":")
                    meta[k.strip()] = v.strip()
            body = parts[2]
    req_lines: List[str] = []
    criteria: List[Tuple[str, str]] = []
    in_criteria = False
    for ln in body.splitlines():
        if ln.startswith("#"):
            in_criteria = "验收" in ln
            continue
        m = _CRITERIA_RX.match(ln.strip())
        if m:
            tag = "auto" if m.group(1).lower() == "auto" else "manual"
            criteria.append((tag, m.group(2).strip()))
        elif ln.strip().startswith("- ") and in_criteria:
            criteria.append(("manual", ln.strip()[2:].strip()))
        elif not in_criteria:
            req_lines.append(ln)
    return meta, "\n".join(req_lines).strip(), criteria


def save_spec(cwd, sid: str, title: str, body: str,
              status: str = "draft") -> Path:
    sid = re.sub(r"[^a-z0-9\-]+", "-", str(sid).lower()).strip("-")[:40] or "spec"
    p = spec_path(cwd, sid)
    p.parent.mkdir(parents=True, exist_ok=True)
    front = (f"---\nid: {sid}\ntitle: {title or sid}\n"
             f"status: {status}\ncreated: "
             f"{time.strftime('%Y-%m-%d %H:%M')}\n---\n")
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(front + body.strip() + "\n")
    return p


def list_specs(cwd) -> List[Tuple[str, str, str, int, int]]:
    """(id, title, status, n_auto, n_manual),按 id 排序。"""
    d = specs_dir(cwd)
    out: List[Tuple[str, str, str, int, int]] = []
    if not d.is_dir():
        return out
    for p in sorted(d.glob("*.md")):
        meta, _req, criteria = parse_spec(p)
        n_auto = sum(1 for tag, _t in criteria if tag == "auto")
        out.append((meta.get("id") or p.stem, meta.get("title") or p.stem,
                    meta.get("status") or "draft", n_auto,
                    len(criteria) - n_auto))
    return out


def mark_spec(cwd, sid: str, status: str) -> Optional[Path]:
    p = spec_path(cwd, sid)
    if not p.exists():
        return None
    meta, req, criteria = parse_spec(p)
    lines = [f"- [{tag}] {text}" for tag, text in criteria]
    body = (f"# 需求\n{req}\n# 验收标准\n" + "\n".join(lines)).strip()
    return save_spec(cwd, sid, meta.get("title") or sid, body, status=status)


def build_implementation_prompt(sid: str, title: str, req: str,
                                criteria: List[Tuple[str, str]]) -> str:
    auto = [t for tag, t in criteria if tag == "auto"]
    manual = [t for tag, t in criteria if tag == "manual"]
    parts = [f"实现以下 spec(id: {sid},title: {title})。\n\n# 需求\n{req}",
             "# 验收标准(必须全部满足)"]
    if auto:
        parts.append("机器可判定项([auto])——完成后逐条自验:\n"
                     + "\n".join(f"- {t}" for t in auto))
    if manual:
        parts.append("人工判定项([manual])——尽力满足并在完成后说明状态:\n"
                     + "\n".join(f"- {t}" for t in manual))
    return "\n\n".join(parts)


def to_eval_task(cwd, sid: str) -> Tuple[Optional[Path], Optional[Path], str]:
    """spec → eval 任务 + 校验脚本。**仅对存在 [auto] 的 spec 生效**
    (入口判定,调用方据此决定提示文案)。返回 (task_path, checker_path, msg)。

    校验脚本逐条运行含反引号命令的 [auto](支持 {python} 占位符),
    任一非零即失败;不含命令的 [auto] 写进任务 instruction 作为要求。
    """
    p = spec_path(cwd, sid)
    if not p.exists():
        return None, None, f"spec {sid!r} 不存在"
    meta, req, criteria = parse_spec(p)
    auto = [t for tag, t in criteria if tag == "auto"]
    if not auto:
        return None, None, (f"spec {sid!r} 没有 [auto] 验收标准——"
                            "转化仅对存在 [auto] 的 spec 生效")
    cmd_items = [t for t in auto if _CMD_RX.search(t)]
    other = [t for t in auto if not _CMD_RX.search(t)]
    manual = [t for tag, t in criteria if tag == "manual"]
    checks_dir = Path(cwd) / "eval" / "checks"
    tasks_dir = Path(cwd) / "eval" / "tasks"
    task_id = f"spec-{sid}"
    instruction = (f"实现 spec {sid}({meta.get('title') or sid})。\n# 需求\n{req}"
                   + ("\n# 附加要求\n" + "\n".join(f"- {t}" for t in other + manual)
                      if other or manual else ""))
    task = {"id": task_id, "category": "spec",
            "instruction": instruction,
            "setup": {"files": {}},
            "check": {"command": '{python} "{check}" "{sandbox}"'}}
    checker = ['"""checker: 由 /spec to-eval 生成,逐条运行 [auto] 命令。"""',
               "import subprocess", "import sys", "",
               f"COMMANDS = {[_CMD_RX.search(t).group(1) for t in cmd_items]!r}",
               'cmd0 = sys.argv[1] if len(sys.argv) > 1 else ""',
               'if cmd0:',
               '    COMMANDS = [c.replace("{python}", cmd0).replace("{sandbox}", sys.argv[2] if len(sys.argv) > 2 else ".") for c in COMMANDS]',
               "for c in COMMANDS:",
               "    r = subprocess.run(c, shell=True)",
               "    if r.returncode != 0:",
               "        print(f'FAILED: {c}')",
               "        raise SystemExit(1)",
               "print('OK')"]
    try:
        tasks_dir.mkdir(parents=True, exist_ok=True)
        checks_dir.mkdir(parents=True, exist_ok=True)
        import json
        tp = tasks_dir / f"{task_id}.json"
        cp = checks_dir / f"{task_id}.py"
        tp.write_text(json.dumps(task, ensure_ascii=False, indent=2),
                      encoding="utf-8")
        with open(cp, "w", encoding="utf-8", newline="") as f:
            f.write("\n".join(checker) + "\n")
    except OSError as e:
        return None, None, f"写入失败:{e}"
    return tp, cp, (f"已生成 eval 任务 {task_id}"
                    + (f"({len(other)} 条无命令 [auto] 已并入 instruction)"
                       if other else ""))


SPEC_DRAFT_PROMPT = """把下面的需求起草成一份 spec 文件,写入 .minicode/specs/<id>.md
(id 用小写连字符)。严格使用以下结构——验收标准必须逐条标注 [auto]
(机器可判定:给出可直接运行的反引号命令或可断言的条件)或 [manual]
(需要人判断);拿不准的一律标 [manual]。
**隐含契约要显式化**:除功能行为外,逐条考虑 元素类型/数值精度与容差/
边界语义(空、单元素、k 越界)/平台差异——这些是红队与蓝队的攻击目标,
写出来才可测(v0.24.2:红队盲区集中在契约的隐含部分):

---
id: <小写连字符>
title: <一句话>
status: draft
---
# 需求
<需求正文>
# 验收标准
- [auto] `<命令>`
- [manual] <人工判定项>

需求:
"""
