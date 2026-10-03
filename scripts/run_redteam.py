#!/usr/bin/env python
"""红队 precision 标注工作流(零依赖)。

对指定 eval 任务:无头求解 → 对解法跑只读红队 → 报告 + 空白三列标注表
存到 eval/redteam/<task>-<时间戳>.md,由人工按盲标流程填写。

用法:
    python scripts/run_redteam.py --task bugfix-off-by-one
    python scripts/run_redteam.py --all          # 跑默认 3 个标注任务

盲标流程(成败关键,顺序不能反):
    1. 先自己读任务代码,列出"我看到的潜在问题";
    2. 再打开红队报告逐条对照,填三列表:
       | 红队提出的问题 | 是否真实存在(是/否/部分) | 严重程度 |
    3. "否"的条目最有价值——它们直接度量提示词误报率,
       决定 v0.24 蓝队循环是否默认开启、攻击性强度要不要调。
    原理:先看报告会被锚定,"我也觉得对"会被误算成命中。

注意:红队跑在"通过校验的正确解法"上——理论上无真问题,任何发现默认
存疑;人工判定为"是"的发现说明任务校验存在盲区,同样有价值。
"""
from __future__ import annotations

import argparse
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
import run_eval  # noqa: E402  复用任务装载与沙箱搭建
from minicode.redteam import LABELING_TABLE  # noqa: E402

DEFAULT_TASKS = ["bugfix-off-by-one", "feature-cli-flag", "refactor-rename-symbol"]
# 第二批(中等难度):红队能发挥的场域——解析逻辑/造测试/多步实现。
# 第一批(简单任务)误报率 0%,但"无发现"多为正确行为,不足以判断
# 提示词上限(2026-10-03 第一批标注基线,详见 eval/README)。
BATCH2_TASKS = ["bugfix-json-syntax", "feature-write-tests",
                "feature-implement-spec"]


def _solve(task: dict, timeout: int, sandbox: Path) -> bool:
    env = {**{k: v for k, v in os.environ.items()},
           "PYTHONUTF8": "1", "PYTHONPATH": str(ROOT)}
    subprocess.run(
        [sys.executable, "-m", "minicode", "-p", task["instruction"],
         "--yolo", "--no-save", "--output-format", "json"],
        cwd=str(sandbox), capture_output=True, timeout=timeout, env=env)
    cproc = subprocess.run(
        run_eval._check_command(task, sandbox, Path(task["_checks_dir"])),
        shell=True, cwd=str(sandbox), capture_output=True, timeout=120, env=env)
    return cproc.returncode == 0


def _diff_vs_setup(task: dict, sandbox: Path) -> str:
    """当前沙箱 vs 初始文件的 unified diff(红队的评审对象)。"""
    import difflib
    chunks = []
    setup = (task.get("setup") or {}).get("files") or {}
    for rel, old in setup.items():
        p = sandbox / rel
        new = p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""
        if old != new:
            d = difflib.unified_diff(old.splitlines(), new.splitlines(),
                                     fromfile=rel, tofile=rel, lineterm="")
            chunks.append("\n".join(d))
    for p in sandbox.rglob("*"):
        if p.is_file() and p.relative_to(sandbox).as_posix() not in setup \
                and "node_modules" not in p.parts and ".git" not in p.parts \
                and "__pycache__" not in p.parts:
            try:
                data = p.read_bytes()[:8192]
            except OSError:
                continue
            if b"\x00" in data:
                continue   # 二进制文件(.pyc 等):null 字符会炸掉子进程 argv
            new = p.read_text(encoding="utf-8", errors="replace")[:400]
            chunks.append(f"--- /dev/null\n+++ {p.relative_to(sandbox).as_posix()}\n"
                          + "\n".join("+ " + ln for ln in new.splitlines()[:60]))
    return "\n\n".join(chunks)[:14000]


def assemble_report(tid: str, ok: bool, diff: str, report: str,
                    sandbox: Path) -> str:
    """盲标材料。小节的顺序就是协议本身:
    元信息 → diff(自查材料)→ 红队报告(对照对象)→ 空白三列表。
    diff 必须先于红队报告落盘——沙箱已删,没有这一节盲标无法执行
    (v0.23.2 修复:此前 diff 只进了红队 prompt,标注被报告锚定)。
    """
    seen = min(len(diff), 14000)
    trunc = (f"\n> 红队实际收到前 {seen} / {len(diff)} 字符(超出部分其未评审;"
             "报告如含「未覆盖区域」一节,以此为准)\n") if len(diff) > 14000 else ""
    return (f"# 红队 precision 标注 — {tid}\n\n"
            f"- 时间:{time.strftime('%Y-%m-%d %H:%M')}\n"
            f"- 校验结果:{'通过' if ok else '未通过(解法本身有问题,发现需重新定性)'}\n"
            f"- 沙箱:{sandbox}\n\n"
            "## 标注协议(顺序不能反)\n\n"
            "1. **只读下一节 diff**,把自己看到的潜在问题写下来;\n"
            "2. 写完后再往下看红队报告,逐条对照;\n"
            "3. 填最后的空白三列表。直接跳到报告 = 本份标注作废(锚定)。\n\n"
            "## 待评审改动(diff)— 自查材料\n\n"
            f"```diff\n{diff}\n```\n" + trunc
            + "## 红队报告\n\n" + (report or "(红队无输出)").strip()
            + "\n\n" + LABELING_TABLE)


def main() -> int:
    from minicode.redteam import build_redteam_prompt
    ap = argparse.ArgumentParser(description="红队 precision 标注工作流")
    ap.add_argument("--task", default="")
    ap.add_argument("--all", action="store_true", help="跑默认 3 个标注任务")
    ap.add_argument("--batch2", action="store_true",
                    help="跑第二批(中等难度:json-syntax/write-tests/implement-spec)")
    ap.add_argument("--tasks", default=str(ROOT / "eval" / "tasks"))
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--out", default=str(ROOT / "eval" / "redteam"))
    ap.add_argument("--keep-sandbox", action="store_true",
                    help="保留任务沙箱目录(默认评审后清理)")
    args = ap.parse_args()

    if args.batch2:
        ids = BATCH2_TASKS
    elif args.all:
        ids = DEFAULT_TASKS
    else:
        ids = [args.task] if args.task else []
    if not ids:
        print("用 --task <id> 或 --all", file=sys.stderr)
        return 1
    tasks = {t["id"]: t for t in run_eval.load_tasks(Path(args.tasks))}
    tasks_dir = Path(args.tasks)
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)

    for tid in ids:
        task = tasks.get(tid)
        if not task:
            print(f"[skip] 任务 {tid} 不存在", file=sys.stderr)
            continue
        task["_checks_dir"] = tasks_dir.parent / "checks"
        sandbox = Path(tempfile.mkdtemp(prefix=f"minicode-redteam-{tid}-"))
        try:
            run_eval._setup_sandbox(sandbox, task)
            ok = _solve(task, args.timeout, sandbox)
            diff = _diff_vs_setup(task, sandbox)
            env = {**{k: v for k, v in os.environ.items()},
                   "PYTHONUTF8": "1", "PYTHONPATH": str(ROOT)}
            prompt = build_redteam_prompt(diff, f"eval 任务 {tid} 的解法")
            proc = subprocess.run(
                [sys.executable, "-m", "minicode", "-p", prompt,
                 "--yolo", "--no-save", "--output-format", "json"],
                cwd=str(sandbox), capture_output=True, timeout=args.timeout,
                env=env)
            try:
                report = json.loads(
                    proc.stdout.decode("utf-8", "replace")
                    .strip().splitlines()[-1]).get("result") or "(红队无输出)"
            except (json.JSONDecodeError, IndexError, KeyError):
                report = f"(红队调用失败: {proc.stderr.decode('utf-8', 'replace')[:300]})"
            text = assemble_report(tid, ok, diff, report, sandbox)
            out = outdir / f"{tid}-{time.strftime('%Y%m%d-%H%M%S')}.md"
            out.write_text(text, encoding="utf-8")
            print(f"[✓] {tid} → {out}")
        finally:
            if args.keep_sandbox:
                print(f"    沙箱保留:{sandbox}")
            else:
                shutil.rmtree(sandbox, ignore_errors=True)
    print("\n下一步:按文件内盲标流程人工填写三列表;"
          "统计\"否\"的比例 = 红队提示词误报率。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
