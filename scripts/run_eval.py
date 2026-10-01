#!/usr/bin/env python
"""minicode eval 回路 —— 真实任务成功率度量（零第三方依赖）。

每个任务在独立临时沙箱里：写入初始文件 → 以 `python -m minicode -p <指令>
--yolo --no-save` 无头运行 → 执行校验命令判定通过。汇总成功率 / 时长 /
token 用量，输出 Markdown 表格并落盘 JSON。

用法：
    python scripts/run_eval.py                     # 跑全部任务
    python scripts/run_eval.py --filter json       # 只跑 id 含 "json" 的任务
    python scripts/run_eval.py --timeout 240       # 单任务超时（秒）
    python scripts/run_eval.py --list              # 只列出任务

模型与端点沿用 minicode 的环境变量（OPENAI_API_KEY / OPENAI_BASE_URL /
OPENAI_MODEL / ANTHROPIC_API_KEY ...）。离线自检可用
MINICODE_FAKE_LLM 指向脚本模型（tests/test_v021_eval.py 有示例）。

注意：任务以 --yolo 运行，请仅在可信环境执行（与 Codex/Claude Code 的
自动评测同一前提）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOKEN_RX = re.compile(r"▲ in ([0-9,]+) · out ([0-9,]+)")


def load_tasks(tasks_dir: Path) -> list:
    tasks = []
    for p in sorted(tasks_dir.glob("*.json")):
        try:
            t = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            print(f"[skip] {p.name}: {e}", file=sys.stderr)
            continue
        t.setdefault("id", p.stem)
        tasks.append(t)
    return tasks


def _setup_sandbox(sandbox: Path, task: dict) -> None:
    for rel, content in ((task.get("setup") or {}).get("files") or {}).items():
        p = sandbox / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    for cmd in (task.get("setup") or {}).get("commands") or []:
        subprocess.run(cmd, shell=True, cwd=str(sandbox), capture_output=True,
                       timeout=60)


def _check_command(task: dict, sandbox: Path, checks_dir: Path) -> str:
    chk = task["check"]
    cmd = chk["command"]
    check_file = checks_dir / f"{task['id']}.py"
    cmd = cmd.replace("{python}", f'"{sys.executable}"')
    if "{check}" in cmd:
        cmd = cmd.replace("{check}", f'"{check_file}"')
    if "{sandbox}" in cmd:
        cmd = cmd.replace("{sandbox}", f'"{sandbox}"')
    return cmd


def run_task(task: dict, timeout: int, checks_dir: Path) -> dict:
    sandbox = Path(tempfile.mkdtemp(prefix=f"minicode-eval-{task['id']}-"))
    rec = {"id": task["id"], "category": task.get("category", ""),
           "ok": False, "seconds": 0.0, "in_tok": 0, "out_tok": 0,
           "error": ""}
    try:
        _setup_sandbox(sandbox, task)
        env = {**os.environ, "PYTHONUTF8": "1", "PYTHONPATH": str(ROOT)}
        t0 = time.time()
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "minicode", "-p", task["instruction"],
                 "--yolo", "--no-save"],
                cwd=str(sandbox), capture_output=True, timeout=timeout,
                env=env)
            out = proc.stdout.decode("utf-8", errors="replace")
            rec["seconds"] = round(time.time() - t0, 1)
            for m in TOKEN_RX.finditer(out):
                rec["in_tok"] = int(m.group(1).replace(",", ""))
                rec["out_tok"] = int(m.group(2).replace(",", ""))
        except subprocess.TimeoutExpired:
            rec["seconds"] = round(time.time() - t0, 1)
            rec["error"] = f"agent timeout after {timeout}s"
            return rec
        try:
            cproc = subprocess.run(
                _check_command(task, sandbox, checks_dir), shell=True,
                cwd=str(sandbox), capture_output=True, timeout=120, env=env)
            cout = cproc.stdout.decode("utf-8", errors="replace")
            cerr = cproc.stderr.decode("utf-8", errors="replace")
            ok = cproc.returncode == 0
            expect = (task.get("check") or {}).get("expect_output")
            if ok and expect is not None:
                ok = expect in cout
            rec["ok"] = ok
            if not ok:
                rec["error"] = (f"check failed (rc={cproc.returncode}): "
                                + (cout.strip() or cerr.strip())[:300])
        except subprocess.TimeoutExpired:
            rec["error"] = "check timeout"
        return rec
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="minicode eval harness")
    ap.add_argument("--tasks", default=str(ROOT / "eval" / "tasks"))
    ap.add_argument("--filter", default="", help="只跑 id 含此子串的任务")
    ap.add_argument("--timeout", type=int, default=300, help="单任务秒数")
    ap.add_argument("--out", default=str(ROOT / "eval" / "results"))
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    tasks_dir = Path(args.tasks)
    checks_dir = tasks_dir.parent / "checks"   # eval/tasks → eval/checks
    tasks = [t for t in load_tasks(tasks_dir) if args.filter in t["id"]]
    if args.list:
        for t in tasks:
            print(f"{t['id']:24s} {t.get('category', '')}")
        return 0
    if not tasks:
        print("no tasks", file=sys.stderr)
        return 1

    print(f"运行 {len(tasks)} 个任务（超时 {args.timeout}s/个）…\n")
    rows = []
    for t in tasks:
        rec = run_task(t, args.timeout, checks_dir)
        rows.append(rec)
        mark = "✓" if rec["ok"] else "✗"
        tok = f" · {rec['in_tok']:,}+{rec['out_tok']:,} tok" if rec["in_tok"] else ""
        print(f"  [{mark}] {rec['id']:24s} {rec['seconds']:6.1f}s{tok}"
              + (f" — {rec['error'][:80]}" if rec["error"] else ""))

    passed = sum(1 for r in rows if r["ok"])
    rate = passed / len(rows) * 100
    model = os.environ.get("OPENAI_MODEL") or os.environ.get("ANTHROPIC_MODEL") \
        or "default"
    print(f"\n通过 {passed}/{len(rows)}（{rate:.0f}%） · 模型 {model} · "
          f"总耗时 {sum(r['seconds'] for r in rows):.0f}s")

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    report = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "model": model,
              "pass_rate": round(rate, 1), "results": rows}
    path = outdir / f"eval-{stamp}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    print(f"报告已写入 {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
