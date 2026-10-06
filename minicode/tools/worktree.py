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
import tempfile
import threading
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
