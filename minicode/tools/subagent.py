"""dispatch_agent tools: spawn subagents, including custom ones
defined in .minicode/agents/*.md (selected via subagent_type).
dispatch_agents spawns several at once, in parallel."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from .base import Tool, ToolContext, ToolError, truncate_middle


class DispatchAgentTool(Tool):
    name = "dispatch_agent"
    kind = "read"
    description = ("Spawn a subagent to investigate a focused question and report findings. "
                   "Default subagent is read-only (read_file/glob/grep/list_dir/web_search). "
                   "Use subagent_type to select a custom agent defined in "
                   ".minicode/agents/*.md. Use for broad exploration that would flood the "
                   "main context.")
    input_schema = {
        "type": "object",
        "properties": {
            "prompt": {"type": "string",
                       "description": "Self-contained task for the subagent, including paths "
                                      "and what to report."},
            "subagent_type": {"type": "string",
                              "description": "Custom agent name from .minicode/agents/ "
                                             "(optional)."},
        },
        "required": ["prompt"],
    }

    def describe_call(self, args: dict) -> str:
        who = f"[{args['subagent_type']}] " if args.get("subagent_type") else ""
        return who + " ".join(str(args.get("prompt") or "").split())[:90]

    def run(self, args: dict, ctx: ToolContext) -> str:
        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            raise ToolError("prompt is required")
        if ctx.agent_factory is None:
            raise ToolError("subagents are not available in this mode")
        report = ctx.agent_factory(prompt, args.get("subagent_type"))
        return report or "(subagent returned no output)"


class DispatchAgentsTool(Tool):
    """并行多子智能体：一个调用同时分派多个独立调研任务。"""

    name = "dispatch_agents"
    kind = "read"
    MAX_TASKS = 6
    MAX_WORKERS = 4

    description = ("Spawn MULTIPLE subagents in PARALLEL — one per task in the list. "
                   "Use when there are several INDEPENDENT investigations (e.g. review "
                   "3 modules at once, explore 2 alternative approaches, audit different "
                   "directories). Each task must be self-contained; results come back "
                   "labeled per task. Tasks share nothing — do not split work that has "
                   "ordering dependencies.")
    input_schema = {
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "minItems": 2,
                "maxItems": MAX_TASKS,
                "items": {
                    "type": "object",
                    "properties": {
                        "prompt": {"type": "string",
                                   "description": "Self-contained task for this subagent."},
                        "subagent_type": {"type": "string",
                                          "description": "Custom agent name (optional)."},
                    },
                    "required": ["prompt"],
                },
                "description": "2-6 independent tasks, run concurrently.",
            },
        },
        "required": ["tasks"],
    }

    def describe_call(self, args: dict) -> str:
        tasks = args.get("tasks") or []
        heads = [" ".join(str(t.get("prompt") or "").split())[:24] for t in tasks[:3]]
        return f"∥ {len(tasks)} tasks: {'; '.join(heads)}" + (
            "…" if len(tasks) > 3 else "")

    def run(self, args: dict, ctx: ToolContext) -> str:
        tasks = args.get("tasks")
        if not isinstance(tasks, list) or not (2 <= len(tasks) <= self.MAX_TASKS):
            raise ToolError(f"tasks must be an array of 2-{self.MAX_TASKS} items")
        if ctx.agent_factory is None:
            raise ToolError("subagents are not available in this mode")

        def one(idx: int, task: dict) -> str:
            if not isinstance(task, dict) or not str(task.get("prompt") or "").strip():
                return "Error: each task needs a prompt"
            try:
                return (ctx.agent_factory(str(task["prompt"]).strip(),
                                          task.get("subagent_type"))
                        or "(no output)")
            except Exception as e:  # 单个任务失败不影响其他任务
                return f"Error: {type(e).__name__}: {e}"

        with ThreadPoolExecutor(max_workers=min(self.MAX_WORKERS, len(tasks))) as ex:
            futures = [ex.submit(one, i, t) for i, t in enumerate(tasks)]
            results = [f.result() for f in futures]

        sections = []
        for i, (task, report) in enumerate(zip(tasks, results), 1):
            head = " ".join(str(task.get("prompt") or "").split())[:60]
            tag = f"[{task.get('subagent_type')}] " if task.get("subagent_type") else ""
            body = truncate_middle(report.strip() or "(no output)", 4000)
            sections.append(f"### 任务 {i}：{tag}{head}\n{body}")
        return (f"全部 {len(tasks)} 个子智能体已完成（并行）。"
                "综合以下报告，注意它们相互独立：\n\n" + "\n\n".join(sections))
