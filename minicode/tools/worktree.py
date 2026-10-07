"""Worktree 并行探索(v0.26 第一版:只读场景)。

为 N 个调查任务各创建一个独立的 git worktree(基于当前 HEAD),派只读
子代理并行调查,汇总报告后清理 worktree。

v0.26 范围限定(与审查共识一致):
- **只读**:子代理用只读 registry,不写任何文件——不需要 _record_hit
  跳过(skills.py 方案 C)、不需要独立 CheckpointManager、无合并策略;
- **并行实现(写权限 + 多方案取优)留给 v0.26 后续**——那才需要 hits
  跳过与合并机制;
- 只用于"有多个可行方向"的调查任务——单一最优路径的任务用
  dispatch_agent 即可,worktree 的 N 倍成本换不来收益;
- 基于当前 HEAD 的**已提交状态**——未提交改动不在探索范围内。

worktree 与 hits(方案 C,v0.25.3 定稿):子代理加载技能**会**把命中写
进 worktree 自己的 .hits.json,worktree 删除时随之消失——主仓库数据
不受污染(准确说:是"写了但随删除",不是"不写";v0.26.2 勘误)。
若将来 worktree 引入存活/复用机制,"写了又删"会成为问题,届时需
实现跳过逻辑(判据:git rev-parse --show-toplevel != 主仓根)。

**dogfooding 记录**:本工具首次真实任务(minicode 仓库自省,双路)即
发现 3 个真实缺陷(elide 重入无防护 P1 / server.py /api/compact 无
异常兜底 P1 / 压缩后仍超限无告知 P2),全部当日修复并经重探索确认
——worktree 隔离探索的工程价值由此实测。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List, Tuple

from .base import Tool, ToolContext, ToolError

MAX_TASKS = 4
MIN_TASKS = 2

EXPLORE_PROMPT = """你在为一个独立的 git worktree 做只读探索(与主工作区和其他 \
worktree 完全隔离)。

## 任务
{task}

## 要求
- 只调查、不修改任何文件;
- 结论必须给出可指认的证据(文件:行 / 命令输出);
- 这是并行探索的一路,其他 worktree 在调查别的方向——把你这条路**查实的**
  与**排除的**都写清楚,避免与其他路重复。"""


def _git(args: list, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git"] + args, cwd=str(cwd), capture_output=True,
                          timeout=60)


class WorktreeExploreTool(Tool):
    name = "worktree_explore"
    kind = "read"
    description = (
        "Parallel read-only exploration in isolated git worktrees (2-4 tasks). "
        "Each task gets its own worktree checked out from HEAD and a read-only "
        "subagent; reports are merged. Use for multi-angle investigations "
        "(root-cause hunts, design-space surveys) on the committed state — "
        "not for single-path tasks, and uncommitted changes are not visible. "
        "Requires a git repository. Need more lanes? Run in batches of 2-4. "
        "NOTE: worktrees are ephemeral sandboxes — brain/skill/experience "
        "deposits made inside them are NOT preserved in the main repository.")
    input_schema = {
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": MIN_TASKS,
                "maxItems": MAX_TASKS,
                "description": "2-4 个调查任务,每个一句明确的探索目标。",
            },
        },
        "required": ["tasks"],
    }

    def describe_call(self, args: dict) -> str:
        tasks = args.get("tasks") or []
        return f"{len(tasks)} 个并行 worktree: " + " / ".join(
            str(t)[:40] for t in tasks[:3])

    def run(self, args: dict, ctx: ToolContext) -> str:
        import inspect
        tasks = args.get("tasks") or []
        tasks = [str(t).strip() for t in tasks if str(t).strip()]
        if len(tasks) < MIN_TASKS or len(tasks) > MAX_TASKS:
            raise ToolError(f"tasks 需要 {MIN_TASKS}-{MAX_TASKS} 条,收到 "
                            f"{len(tasks)} 条(更多路请分批调用)")
        if not callable(getattr(ctx, "agent_factory", None)):
            raise ToolError("当前上下文没有子代理工厂")
        # 显式签名检查,一次性放循环外——except TypeError 会把子代理内部
        # 的类型错误误报成"工厂签名不对",误导排查方向(v0.26.1 审查①)
        if "cwd" not in inspect.signature(ctx.agent_factory).parameters:
            raise ToolError("子代理工厂不支持 cwd 参数——需要 v0.26 的"
                            "factory(cwd=) 签名")
        base = Path(ctx.cwd)
        r = _git(["rev-parse", "--is-inside-work-tree"], base)
        if r.returncode != 0:
            raise ToolError("当前目录不是 git 仓库——worktree 探索需要 git")
        r = _git(["rev-parse", "--verify", "HEAD"], base)
        if r.returncode != 0:
            raise ToolError("仓库没有任何提交(无 HEAD)——先提交再探索")

        worktrees: List[Tuple[int, Path]] = []
        lock = threading.Lock()
        results: List[Tuple[int, str, str]] = []
        failures: List[str] = []

        def one(i: int, task: str) -> None:
            # worktree 放在 %TEMP%(通常 C 盘)而仓库可能在 D 盘——跨盘仍正常:
            # git worktree 靠 .git 文件指针而非同盘硬链接,实测 0.3s(v0.26.1)
            wt = Path(tempfile.mkdtemp(prefix=f"minicode-wt{i+1}-"))
            add = _git(["worktree", "add", "--detach", str(wt), "HEAD"], base)
            with lock:
                if add.returncode != 0:
                    failures.append(
                        f"worktree {i+1} 创建失败: "
                        f"{add.stderr.decode('utf-8', 'replace')[:200]}")
                    shutil.rmtree(wt, ignore_errors=True)
                    return
                worktrees.append((i, wt))
            out = ctx.agent_factory(
                EXPLORE_PROMPT.format(task=task), cwd=wt)
            with lock:
                results.append((i, task, str(out or "(无输出)")))

        try:
            with ThreadPoolExecutor(max_workers=len(tasks)) as ex:
                list(ex.map(lambda pair: one(*pair), enumerate(tasks)))
            if failures and not results:
                raise ToolError("; ".join(failures))   # 全军覆没才抛
        finally:
            for _i, wt in worktrees:
                rm = _git(["worktree", "remove", "--force", str(wt)], base)
                if rm.returncode != 0:
                    shutil.rmtree(wt, ignore_errors=True)
            _git(["worktree", "prune"], base)

        ordered = sorted(results, key=lambda t: t[0])
        out = [f"## 并行探索报告({len(ordered)}/{len(tasks)} 路完成)"]
        for i, task, text in ordered:
            out.append(f"### 路 {i + 1}:{task}\n{text.strip()}\n")
        if failures:
            out.append("### 失败的路\n" + "\n".join(failures))
        return "\n".join(out)


def _decode_out(b: bytes) -> str:
    """命令输出解码:utf-8 与 GBK 各解一次,取替换字符更少者
    (Windows cmd 的报错文案是 GBK,直接 utf-8 会乱码)。"""
    if not b:
        return ""
    best = b.decode("utf-8", errors="replace")
    try:
        alt = b.decode("gbk", errors="replace")
        if alt.count("�") < best.count("�"):
            best = alt
    except (ValueError, UnicodeDecodeError):
        pass
    return best.strip()


def _ckpt_dir(worktree: Path) -> "object":
    """lane 检查点放 worktree 内部(要点 1+8):随 worktree 删除,
    避免主仓 prune_checkpoint_roots 误删正在使用的检查点。"""
    from ..checkpoints import CheckpointManager
    return CheckpointManager(worktree / ".minicode" / "checkpoints")


def _run_check(check: str, worktree: Path) -> Tuple[bool, str]:
    """在 lane worktree 里跑客观校验命令,返回 (是否通过, 输出尾部)。"""
    try:
        proc = subprocess.run(check, shell=True, cwd=str(worktree),
                              capture_output=True, timeout=300)
    except subprocess.TimeoutExpired:
        return False, "(check 超时 300s)"
    out = _decode_out((proc.stdout or b"") + (proc.stderr or b""))
    return proc.returncode == 0, out[-400:]


# lane 产物过滤:这些是运行副产物(如 check 命令跑 Python 生成的
# __pycache__),不是方案改动——与 run_redteam / run_bluefix_ab 的
# harness 卫生同款(三处各自维护)。v0.26.4 审查:第 4 处无头链路
# 出现时,抽公共模块(如 minicode/harness.py 的 RUN_ARTIFACTS)统一。
_LANE_SKIP = ("__pycache__", ".pytest_cache", ".minicode", ".git",
              "node_modules")


def _lane_paths(worktree: Path) -> List[str]:
    """lane 相对 HEAD 的改动路径(porcelain,已过滤运行副产物)。

    过滤是必需的:check 命令(如 python -c)会生成 __pycache__,
    不分青红皂白地应用会撞上目录复制/权限问题,也污染零污染验证。

    已知局限(v0.26.4 记录):git 对含中文/特殊字符的路径会做 C 风格
    转义(\344\270\255 形式),strip('"') 只去引号不解转义——这类路径会
    被静默跳过(罕见;根治需改用 git status --porcelain=-z 的 NUL
    分隔输出)。绝大多数 ASCII 路径不受影响。
    """
    st = _git(["status", "--porcelain"], worktree)
    paths: List[str] = []
    for ln in st.stdout.decode("utf-8", "replace").splitlines():
        if len(ln) < 4:
            continue
        raw = ln[3:].strip()
        path = raw.strip('"')
        if not path or raw.endswith("/"):
            continue
        parts = Path(path).parts
        if any(part in _LANE_SKIP for part in parts):
            continue
        paths.append(path)
    return paths


def _apply_lane(worktree: Path, main: Path) -> List[str]:
    """把胜出 lane 的改动(相对 HEAD)逐文件应用到主仓库。

    主仓在入口已验证 porcelain 干净,故只触碰 lane 改过的文件 = 零污染。
    worktree 里存在而 HEAD 没有的路径 → 复制;worktree 里删除的 → 主仓删除。
    """
    applied: List[str] = []
    for path in _lane_paths(worktree):
        src = worktree / path
        dst = main / path
        if src.exists():
            existed = dst.exists()
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            applied.append(("M " if existed else "A ") + path)
        elif dst.exists():
            dst.unlink()
            applied.append("D " + path)
    return applied


class WorktreeImplementTool(Tool):
    """写权限并行实现(v0.26.2):N 路 worktree 各自实现方案,N 选 1 应用回主仓。

    流程:主仓 porcelain 干净门 → 两路并行实现(可写子代理,检查点在
    worktree 内部)→ 各路跑 check 命令客观选优 → 胜出方案的改动逐文件
    应用回主仓 → 零污染验证(应用后主仓 porcelain 应恰好等于应用清单)。
    红队+蓝队质量链**不在本工具内**(要点 7)——应用后手动 /redteam →
    /bluefix,只对胜出方案跑。无 check 命令时不自动应用(缺客观选优依据)。
    """
    name = "worktree_implement"
    kind = "write"
    description = (
        "Implement 2 solution variants in parallel, each in its own isolated "
        "git worktree with a WRITABLE subagent, then apply the winner back to "
        "the main repository (N-choose-1, no auto-merge). Objective ranking "
        "via the required `check` command run in each worktree. Preconditions: "
        "main repo must be clean (git status --porcelain empty); lane agents "
        "must make their solution self-contained. Red/blue quality chain "
        "should be run manually on the applied winner afterwards.")
    input_schema = {
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 2,
                "maxItems": 2,
                "description": "两份实现指令(方案 A / 方案 B),各自必须自洽。",
            },
            "check": {
                "type": "string",
                "description": ("客观校验命令(在每路 worktree 里运行,退出码 "
                                "0 = 通过;支持 {python} 占位符)。提供后才自动"
                                "应用胜出方案。"),
            },
        },
        "required": ["tasks", "check"],
    }

    def describe_call(self, args: dict) -> str:
        tasks = args.get("tasks") or []
        return ("双路并行实现 + N 选 1 应用: "
                + " / ".join(str(t)[:36] for t in tasks[:2]))

    def run(self, args: dict, ctx: ToolContext) -> str:
        if not callable(getattr(ctx, "worktree_factory", None)):
            raise ToolError("当前上下文没有写权限 worktree 工厂")
        tasks = [str(t).strip() for t in (args.get("tasks") or [])
                 if str(t).strip()]
        if len(tasks) != 2:
            raise ToolError("tasks 需要 2 份实现指令(方案 A / 方案 B)")
        check = str(args.get("check") or "").replace(
            "{python}", f'"{sys.executable}"')
        if not check:
            raise ToolError("check 命令必填——没有客观选优依据就不自动应用")
        base = Path(ctx.cwd)
        st = _git(["status", "--porcelain"], base)
        if st.returncode != 0:
            raise ToolError("主仓 git status 失败——无法安全应用")
        if st.stdout.strip():
            raise ToolError("主仓库有未提交改动——请先提交或 stash 再并行"
                            "实现(写权限版硬约束,防止覆盖你的改动)")

        lanes: List[dict] = []
        lock = threading.Lock()

        def one(i: int, task: str) -> None:
            t0 = time.time()
            wt = Path(tempfile.mkdtemp(prefix=f"minicode-impl{i+1}-"))
            add = _git(["worktree", "add", "--detach", str(wt), "HEAD"], base)
            if add.returncode != 0:
                with lock:
                    lanes.append({"i": i, "task": task, "wt": None,
                                  "error": add.stderr.decode(
                                      "utf-8", "replace")[:200]})
                shutil.rmtree(wt, ignore_errors=True)
                return
            ckpt = _ckpt_dir(wt)
            blue = ctx.worktree_factory(task, wt, ckpt)
            check_ok, check_out = _run_check(check, wt)
            changed = _lane_paths(wt)
            with lock:
                lanes.append({"i": i, "task": task, "wt": wt, "error": "",
                              "seconds": round(time.time() - t0, 1),
                              "blue": str(blue or "(无输出)"),
                              "check_ok": check_ok,
                              "check_out": check_out[-400:],
                              "changed": changed})

        try:
            with ThreadPoolExecutor(max_workers=2) as ex:
                list(ex.map(lambda pair: one(*pair), enumerate(tasks)))
        finally:
            # lane 的改动要在清理前应用——先记录胜者,再统一清理
            pass

        crashed = [lane for lane in lanes if lane.get("error")]
        if crashed and not any(lane.get("changed") is not None
                               for lane in lanes):
            raise ToolError("两路均未产出改动: "
                            + "; ".join(lane["error"] for lane in crashed))
        passing = [lane for lane in lanes if lane.get("check_ok")]
        applied_to = None
        applied_files = []
        archives: List[str] = []
        winner_paths: List[str] = []
        if passing:
            # 选优边界(v0.26.4,审查):多路并列通过 check 时**不做智能
            # 裁决**——应用第一路,其余通过路的完整 diff 存档供用户换选
            winner = passing[0]
            winner_paths = _lane_paths(Path(winner["wt"]))
            applied_files = _apply_lane(Path(winner["wt"]), base)
            applied_to = winner["i"]
            arch_dir = base / ".minicode" / "worktree"
            for lane in passing:
                pdiff = _git(["diff", "HEAD"], Path(lane["wt"]))
                arch_dir.mkdir(parents=True, exist_ok=True)
                ap = arch_dir / f"{time.strftime('%Y%m%d-%H%M%S')}" \
                     f"-lane{lane['i'] + 1}.patch"
                ap.write_text(pdiff.stdout.decode("utf-8", "replace"),
                              encoding="utf-8")
                archives.append(str(ap))
        # 清理全部 worktree(检查点在 worktree 内部,随之删除——要点 1+8)
        for lane in lanes:
            if lane.get("wt"):
                rm = _git(["worktree", "remove", "--force",
                           str(lane["wt"])], base)
                if rm.returncode != 0:
                    shutil.rmtree(lane["wt"], ignore_errors=True)
        _git(["worktree", "prune"], base)

        # 零污染验证(v0.26.4):应用后主仓 porcelain 的路径集应恰好等于
        # 胜出 lane 的改动路径集(含 D 删除;过滤副产物后逐路径比对)
        after_paths = _lane_paths(base)
        zero_pollution = sorted(winner_paths) == sorted(after_paths)
        # 第四指标(v0.26.4,审查补充):应用后主仓能否直接通过 check——
        # 这是"应用回主仓"的最终验收(防止方案依赖 worktree 里未应用的文件)
        post_check_ok, post_check_out = _run_check(check, base)
        out = [f"## 并行实现报告({len(lanes)} 路)"]
        for lane in lanes:
            tag = f"路 {lane['i'] + 1}"
            if lane.get("error"):
                out.append(f"### {tag}:创建失败 — {lane['error']}")
                continue
            mark = "PASS" if lane["check_ok"] else "FAIL"
            win = " ← 已应用" if applied_to == lane["i"] else ""
            out.append(f"### {tag}{win} — check [{mark}] "
                       f"({lane['seconds']}s)\n{lane['blue']}\n"
                       f"check 输出尾部: {lane['check_out']}")
        if applied_to is not None:
            tie_note = ""
            if len(passing) > 1:
                tie_note = (f"\n\n### 选优边界(并列不裁决)\n"
                            f"{len(passing)} 路并列通过 check,已应用第一路;"
                            "其余通过路的完整 diff 已存档,如需换选可 "
                            "`git apply` 对应 patch:\n"
                            + "\n".join(f"- {a}" for a in archives))
            out.append("### 已应用回主仓(逐文件)\n"
                       + "\n".join(applied_files)
                       + "\n\n零污染验证:应用后主仓改动路径"
                       + ("恰好等于胜出 lane 的改动 ✔"
                          if zero_pollution
                          else "⚠ 与胜出 lane 不一致——请 git status 检查")
                       + f"\n\n### 应用后主仓 check(第四指标)\n"
                       f"[{'PASS' if post_check_ok else 'FAIL'}] "
                       f"{check}\n{post_check_out[-400:]}" + tie_note)
        else:
            out.append("### 未应用\n"
                       "无一路通过 check(或未提供客观依据)——"
                       "两路报告如上,请人工决策")
        return "\n".join(out)
