#!/usr/bin/env python
"""实验报告数据审计(v0.25 不变量 1 的可见性落点)。

连续六轮的静默失败,根因都是"没有可见的失败率指标"——这个脚本让
**作废率**成为一眼可见的数字,而不是靠人逐份读报告才发现。

用法:
    python scripts/data_audit.py            # 审计 eval/redteam + eval/bluefix
    python scripts/data_audit.py --strict   # 存在 void:missing 即非零退出
    python scripts/data_audit.py --json     # 机器可读输出

口径:
    有效(valid)        进统计
    作废(void:*)       不进统计,但保留存储(调参/排障证据链)
    void:missing       报告缺 frontmatter —— 不可解析,等同作废
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from report_meta import data_validity, parse_frontmatter  # noqa: E402

REPORT_DIRS = ("eval/redteam", "eval/bluefix")


def audit(base: Path) -> dict:
    rows = []
    for rel in REPORT_DIRS:
        d = base / rel
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.md")):
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            fm = parse_frontmatter(text)
            rows.append({
                "path": str(p.relative_to(base)).replace("\\", "/"),
                "dir": rel,
                "validity": data_validity(text),
                "verify_verdict": fm.get("verify_verdict", ""),
                "task": fm.get("task", ""),
                "kind": fm.get("kind", ""),
                "mode": fm.get("mode", ""),
                "seconds": fm.get("seconds") or fm.get("bluefix_seconds") or "",
            })
    return {"rows": rows}


def summarize(rows: list) -> dict:
    total = len(rows)
    valid = [r for r in rows if r["validity"] == "valid"]
    void = [r for r in rows if r["validity"].startswith("void:")]
    reasons = Counter(r["validity"].split(":", 1)[1] if ":" in r["validity"]
                      else r["validity"] for r in void)
    out = {
        "total": total,
        "valid": len(valid),
        "void": len(void),
        "valid_rate": round(len(valid) / total * 100, 1) if total else 0.0,
        "void_by_reason": dict(reasons),
        "missing": sum(1 for r in rows if r["validity"] == "void:missing"),
        "by_dir": {},
    }
    for rel in REPORT_DIRS:
        sub = [r for r in rows if r["dir"] == rel]
        if sub:
            sv = sum(1 for r in sub if r["validity"] == "valid")
            out["by_dir"][rel] = {"total": len(sub), "valid": sv,
                                  "valid_rate": round(sv / len(sub) * 100, 1)}
    # 修复质量判定维度(v0.25.2):仅对 valid 报告统计——链路可信 ≠ 修复达标
    verdicts = Counter(r["verify_verdict"] for r in valid if r["verify_verdict"])
    out["by_verdict"] = dict(verdicts)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="实验报告数据审计")
    ap.add_argument("--base", default=str(ROOT))
    ap.add_argument("--strict", action="store_true",
                    help="存在 void:missing(缺 frontmatter)即非零退出")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    rows = audit(Path(args.base))["rows"]
    s = summarize(rows)
    if args.json:
        print(json.dumps(s, ensure_ascii=False, indent=2))
    else:
        print(f"实验报告审计:共 {s['total']} 份 · 有效 {s['valid']} "
              f"({s['valid_rate']}%) · 作废 {s['void']}")
        for rel, d in s["by_dir"].items():
            print(f"  {rel:16s} {d['valid']}/{d['total']} 有效"
                  f"({d['valid_rate']}%)")
        if s["void_by_reason"]:
            print("  作废原因:" + "、".join(
                f"{k} {v}" for k, v in sorted(s["void_by_reason"].items())))
        if s["missing"]:
            print(f"  ⚠ {s['missing']} 份缺 frontmatter(void:missing)"
                  "——不变量 1 要求 harness 独占写入")
        if s.get("by_verdict"):
            print("  修复质量判定(valid 报告):" + "、".join(
                f"{k} {v}" for k, v in sorted(s["by_verdict"].items())))
    if args.strict and s["missing"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
