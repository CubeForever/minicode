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
            try:
                mtime = p.stat().st_mtime
            except OSError:
                mtime = 0.0
            rows.append({
                "path": str(p.relative_to(base)).replace("\\", "/"),
                "dir": rel,
                "validity": data_validity(text),
                "verify_verdict": fm.get("verify_verdict", ""),
                "task": fm.get("task", ""),
                "kind": fm.get("kind", ""),
                "mode": fm.get("mode", ""),
                "seconds": fm.get("seconds") or fm.get("bluefix_seconds") or "",
                "mtime": mtime,
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
    # 修复质量判定维度(v0.25.2):仅对 valid 且蓝队类的报告统计——
    # 链路可信 ≠ 修复达标;红队报告无自验环节,不参与 verdict 统计。
    # verdict 缺失的蓝队报告单独计数(v0.25.3 门禁会拦,这里先可见)
    bluefix_valid = [r for r in valid if "bluefix" in r.get("kind", "")]
    verdicts = Counter(r["verify_verdict"] for r in bluefix_valid
                       if r["verify_verdict"])
    out["by_verdict"] = dict(verdicts)
    out["verdict_missing"] = sum(1 for r in bluefix_valid
                                 if not r["verify_verdict"])

    # 按日期分桶(v0.25.3,审查④):累计统计会随数据量稀释,"近 7 天有效率"
    # 才是当前机制健康度的真实指标;存量(pre_invariant)单独归类。
    import time as _time
    now = _time.time()
    buckets = {"recent_7d": [], "older": [], "pre_invariant": []}
    for r in rows:
        if r["validity"] == "void:pre_invariant":
            buckets["pre_invariant"].append(r)
        elif now - r.get("mtime", 0) <= 7 * 86400:
            buckets["recent_7d"].append(r)
        else:
            buckets["older"].append(r)
    out["by_date"] = {}
    for name, sub in buckets.items():
        if not sub:
            continue
        sv = sum(1 for r in sub if r["validity"] == "valid")
        out["by_date"][name] = {"total": len(sub), "valid": sv,
                                "valid_rate": round(sv / len(sub) * 100, 1)
                                if sub else 0.0}
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
        if s["verdict_missing"]:
            print(f"  ⚠ {s['verdict_missing']} 份 valid 报告缺 verify_verdict"
                  "——统计修复率时不可计入")
        for name, d in s.get("by_date", {}).items():
            label = {"recent_7d": "近 7 天", "older": "更早",
                     "pre_invariant": "历史存量(pre_invariant)"}[name]
            print(f"  {label:24s} {d['valid']}/{d['total']} 有效"
                  f"({d['valid_rate']}%)")
    if args.strict and s["missing"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
