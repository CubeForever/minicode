"""dispatch_agent tool: spawn subagents, including custom ones
defined in .minicode/agents/*.md (selected via subagent_type)."""
from __future__ import annotations

from .base import Tool, ToolContext, ToolError


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
