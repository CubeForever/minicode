"""exit_plan tool: the model presents its plan for approval (Claude Code parity)."""
from __future__ import annotations


from .base import Tool, ToolContext, ToolError


class ExitPlanTool(Tool):
    name = "exit_plan"
    kind = "meta"
    description = ("Present your implementation plan for user approval. Use ONLY in plan "
                   "mode, after you have finished read-only investigation. The plan should "
                   "list files to change, ordered steps, and verification.")
    input_schema = {
        "type": "object",
        "properties": {
            "plan": {"type": "string", "description": "The full implementation plan in markdown."},
            "allowed_prompts": {"type": "array", "items": {"type": "string"},
                                "description": "Permission rules to pre-approve after plan "
                                               "approval, e.g. 'Bash(pytest *)', 'Bash(npm run build *)'."},
        },
        "required": ["plan"],
    }

    def describe_call(self, args: dict) -> str:
        return " ".join(str(args.get("plan") or "").split())[:90]

    def run(self, args: dict, ctx: ToolContext) -> str:
        plan = str(args.get("plan") or "").strip()
        if not plan:
            raise ToolError("plan is required")
        rules = [str(r).strip() for r in (args.get("allowed_prompts") or []) if str(r).strip()]
        if rules:
            session_rules = getattr(ctx.session, "plan_allowed_rules", [])
            session_rules.extend(rules)
        ctx.ui.newline()
        for line in plan.splitlines()[:60]:
            ctx.ui.plain("  │ " + line)
        if len(plan.splitlines()) > 60:
            ctx.ui.plain(f"  │ … (+{len(plan.splitlines()) - 60} lines)")
        if rules:
            ctx.ui.plain(f"  │ 批准后将预授权：{', '.join(rules)}")
        return ("The plan has been shown to the user. Stop now and wait for their "
                "approval — do not take further action in this turn.")
