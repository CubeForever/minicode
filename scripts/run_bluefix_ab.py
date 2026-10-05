#!/usr/bin/env python
"""蓝队 A/B 对照实验(v0.24)——决定蓝队该读什么。

实验设计(审查定稿):
    同一份红队报告,两种蓝队输入各跑一次:
        A(findings):仅「发现」列表
        B(full):全量报告(含「已证伪的可疑点」与「存疑」——证伪过程)
    人工比对:两组各自补上了哪些人工标注时发现的漏项
    (implement-spec 已知三漏项:float() 对 Decimal 丢精度 / 浮点无容差 /
    元素类型未校验)。若 B 明显补上漏项 → 蓝队正式提示词必须读全量。

用法:
    python scripts/run_bluefix_ab.py --task feature-implement-spec
    python scripts/run_bluefix_ab.py --task feature-implement-spec --keep-sandbox

流程(每模式):重建沙箱 → 无头求解 → 取该任务最新红队报告为蓝队输入 →
无头蓝队(yolo,带写权限)→ 复跑 eval 校验 → 修复后 diff + 报告落盘
eval/bluefix/<task>-<mode>-<ts>.md。
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import run_eval  # noqa: E402
from minicode.blueteam import blue_input, build_blueteam_prompt  # noqa: E402

PILOT_TASK = "feature-implement-spec"
MODES = ("findings", "full")


def _redteam_report(cwd: Path, tid: str) -> Path:
    cands = sorted(Path(cwd).glob("eval/redteam") / f"{tid}-*.md")
    if not cands:
        raise SystemExit(f"没有 {tid} 的红队报告——先跑 scripts/run_redteam.py")
    return cands[-1]


def _base_env() -> dict:
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONPATH": str(ROOT)}
    from pathlib import Path as _P
    env["PATH"] = (str(_P(sys.executable).parent) + os.pathsep
                   + env.get("PATH", ""))
    return env


def _diff_snapshot(sandbox: Path, setup: dict) -> str:
    chunks = []
    setup = setup or {}
    for rel, old in setup.items():
        p = sandbox / rel
        new = p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""
        d = difflib.unified_diff(old.splitlines(), new.splitlines(),
                                 fromfile=rel, tofile=rel, lineterm="")
        chunks.append("\n".join(d))
    return "\n\n".join(chunks)[:12000]


def main() -> int:
    ap = argparse.ArgumentParser(description="蓝队 A/B 对照实验")
    ap.add_argument("--task", default=PILOT_TASK)
    ap.add_argument("--tasks", default=str(ROOT / "eval" / "tasks"))
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--out", default=str(ROOT / "eval" / "bluefix"))
    ap.add_argument("--keep-sandbox", action="store_true")
    args = ap.parse_args()

    tasks = {t["id"]: t for t in run_eval.load_tasks(Path(args.tasks))}
    task = tasks.get(args.task)
    if not task:
        print(f"任务 {args.task} 不存在", file=sys.stderr)
        return 1
    task["_checks_dir"] = Path(args.tasks).parent / "checks"
    red_md_path = _redteam_report(ROOT, args.task)
    report_md = red_md_path.read_text(encoding="utf-8")
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")

    print(f"A/B 对照实验 — {args.task}(红队输入:{red_md_path.name})\n")
    results = {}
    for mode in MODES:
        sandbox = Path(tempfile.mkdtemp(
            prefix=f"minicode-bluefix-{args.task}-{mode}-"))
        try:
            run_eval._setup_sandbox(sandbox, task)
            # 求解(与红队实验同一套,重建"待修复"的代码状态)
            subprocess.run(
                [sys.executable, "-m", "minicode", "-p", task["instruction"],
                 "--yolo", "--no-save", "--output-format", "json"],
                cwd=str(sandbox), capture_output=True, timeout=args.timeout,
                env=_base_env())
            pre_diff = _diff_snapshot(sandbox,
                                      (task.get("setup") or {}).get("files"))
            prompt = build_blueteam_prompt(blue_input(report_md, mode), mode)
            fail = ""
            try:
                proc = subprocess.run(
                    [sys.executable, "-m", "minicode", "-p", prompt,
                     "--yolo", "--no-save", "--output-format", "json"],
                    cwd=str(sandbox), capture_output=True,
                    timeout=args.timeout, env=_base_env())
                blue_out, fail = run_eval.__dict__.get("_noop") or \
                    _extract(proc.stdout.decode("utf-8", "replace"),
                             proc.stderr.decode("utf-8", "replace"))
            except subprocess.TimeoutExpired:
                fail = "timeout"
                blue_out = f"(蓝队调用失败:超时 {args.timeout}s)"
            post_diff = _diff_snapshot(sandbox,
                                       (task.get("setup") or {}).get("files"))
            cproc = subprocess.run(
                run_eval._check_command(task, sandbox,
                                        Path(task["_checks_dir"])),
                shell=True, cwd=str(sandbox), capture_output=True,
                timeout=120, env=_base_env())
            check_ok = cproc.returncode == 0
            body = (f"# 蓝队 A/B — {args.task} · mode={mode}\n\n"
                    f"- 时间:{time.strftime('%Y-%m-%d %H:%M')}\n"
                    f"- 红队输入:{red_md_path.name}({mode} 模式)\n"
                    f"- 蓝队失败类别:{fail or '无'}\n"
                    f"- 修复后校验:{'通过' if check_ok else '未通过'}\n\n"
                    f"## 修复前解法 diff\n\n```diff\n{pre_diff}\n```\n\n"
                    f"## 蓝队输出\n\n{blue_out}\n\n"
                    f"## 修复后总 diff(蓝队改动 = 本节 − 上一节)\n\n"
                    f"```diff\n{post_diff}\n```\n")
            out = outdir / f"{args.task}-{mode}-{stamp}.md"
            out.write_text(body, encoding="utf-8")
            results[mode] = {"check_ok": check_ok, "fail": fail, "out": out}
            mark = "✓" if not fail else "✗"
            print(f"[{mark}] mode={mode:8s} 修复后校验:"
                  f"{'通过' if check_ok else '未通过'} → {out}")
        finally:
            if args.keep_sandbox:
                print(f"    沙箱保留:{sandbox}")
            else:
                shutil.rmtree(sandbox, ignore_errors=True)

    print("\n人工判定:对照已知漏项(float()/Decimal 精度、浮点容差、"
          "元素类型校验),比较 A/B 各补上了哪些——"
          "结论决定蓝队正式提示词的输入模式。")
    _ = results
    return 0


def _extract(stdout_text: str, stderr_text: str) -> tuple:
    for line in reversed(stdout_text.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "result" not in d:
            continue
        result = d.get("result")
        if result:
            return result, ""
        return "(蓝队无输出——模型未返回正文)", "empty-result"
    return (f"(蓝队调用失败——stdout 无 JSON。stdout 尾部: "
            f"{stdout_text[-400:]!r} | stderr 尾部: {stderr_text[-400:]!r})",
            "no-json")


if __name__ == "__main__":
    sys.exit(main())
