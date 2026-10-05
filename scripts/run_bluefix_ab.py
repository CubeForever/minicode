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

流程:重建沙箱 → 无头求解 → **红队对本轮求解现场重跑**(v0.24.1 对象
绑定——旧报告评审的是另一次求解的产物,B 组的"diff 含 float() 但文件
没有"即此)→ 两种蓝队输入各跑一次(A=仅发现 / B=全量)→ 独立自验门
(重跑蓝队声明的自验命令,自述不算数)→ 修复后 diff 落盘
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
from minicode.blueteam import (blue_input,  # noqa: E402
                               build_blueteam_prompt)
from minicode.redteam import build_redteam_prompt  # noqa: E402

PILOT_TASK = "feature-implement-spec"
MODES = ("findings", "full")


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
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")

    print(f"A/B 对照实验 — {args.task}\n")
    results = {}
    red_report = None   # 每次求解后新跑红队——评审对象与被校验对象绑定
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
            # 红队对本轮求解的 diff 现场重跑(v0.24.1,审查②:旧报告评审的
            # 是另一次求解的产物——B 组的"diff 含 float() 但文件没有"即此)
            if red_report is None:
                rprompt = build_redteam_prompt(pre_diff,
                                               f"eval 任务 {args.task} 的解法")
                rproc = subprocess.run(
                    [sys.executable, "-m", "minicode", "-p", rprompt,
                     "--yolo", "--no-save", "--output-format", "json"],
                    cwd=str(sandbox), capture_output=True,
                    timeout=args.timeout, env=_base_env())
                red_out, _rfail = _extract(
                    rproc.stdout.decode("utf-8", "replace"),
                    rproc.stderr.decode("utf-8", "replace"))
                red_report = outdir / f"redteam-fresh-{stamp}.md"
                red_report.write_text(
                    f"# 红队报告(实验内新跑,绑定本轮求解)— {args.task}\n\n"
                    + red_out, encoding="utf-8")
                print(f"    红队已对本轮求解重跑 → {red_report.name}")
            report_md = red_report.read_text(encoding="utf-8")
            prompt = build_blueteam_prompt(blue_input(report_md, mode), mode)
            fail = ""
            try:
                proc = subprocess.run(
                    [sys.executable, "-m", "minicode", "-p", prompt,
                     "--yolo", "--no-save", "--output-format", "json"],
                    cwd=str(sandbox), capture_output=True,
                    timeout=args.timeout, env=_base_env())
                blue_out, fail = _extract(
                    proc.stdout.decode("utf-8", "replace"),
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
            # 独立自验门:重跑蓝队声明的自验命令——自述不算数(v0.24.1)。
            # v0.24.2:环境不可用(python 探针失败)→ 整份报告作废,不进数据集
            from minicode.blueteam import (independent_verify,
                                           render_independent_verify)
            iv, env_ok = independent_verify(blue_out, sandbox)
            valid = env_ok          # 自验缺失(未声明命令)也按 v0.24.1 判不可信
            body = (f"# 蓝队 A/B — {args.task} · mode={mode}\n\n"
                    f"- 时间:{time.strftime('%Y-%m-%d %H:%M')}\n"
                    f"- 红队输入:{red_report.name}(实验内对本轮求解新跑)\n"
                    f"- 蓝队失败类别:{fail or '无'}\n"
                    f"- 修复后校验:{'通过' if check_ok else '未通过'}\n"
                    f"- 数据有效性:{'有效' if valid else '**作废(环境不可用/自验缺失)**'}\n\n"
                    f"## 修复前解法 diff\n\n```diff\n{pre_diff}\n```\n\n"
                    f"## 蓝队输出\n\n{blue_out}\n\n"
                    + render_independent_verify(iv, env_ok) + "\n\n"
                    f"## 修复后总 diff(蓝队改动 = 本节 − 上一节)\n\n"
                    f"```diff\n{post_diff}\n```\n")
            out = outdir / f"{args.task}-{mode}-{stamp}.md"
            out.write_text(body, encoding="utf-8")
            results[mode] = {"check_ok": check_ok, "fail": fail, "out": out,
                             "valid": valid}
            bad = fail or not valid
            mark = "✗" if bad else "✓"
            reason = ("环境不可用——数据作废" if not valid
                      else ("红队/蓝队失败:" + fail if fail else "通过"))
            print(f"[{mark}] mode={mode:8s} {reason} → {out}")
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
