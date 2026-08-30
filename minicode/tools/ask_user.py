"""AskUserQuestion tool: let the model ask the user a multiple-choice question."""
from __future__ import annotations

from .base import Tool, ToolContext, ToolError


class AskUserTool(Tool):
    name = "ask_user"
    kind = "meta"
    description = ("Ask the user a multiple-choice question when a decision is genuinely "
                   "theirs to make (approach, trade-offs, ambiguous requirements). "
                   "Do NOT use it for permission — tool confirmations already handle that.")
    input_schema = {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "Complete, specific question."},
            "options": {
                "type": "array",
                "minItems": 2,
                "maxItems": 4,
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "Short choice text."},
                        "description": {"type": "string", "description": "What choosing it means."},
                        "preview": {"type": "string", "description": "Optional details/mockup shown under the option."},
                    },
                    "required": ["label"],
                },
            },
            "multi_select": {"type": "boolean", "description": "Allow several choices (default false)."},
            "allow_other": {"type": "boolean",
                            "description": "Let the user type a custom answer (default true)."},
        },
        "required": ["question", "options"],
    }

    def describe_call(self, args: dict) -> str:
        return " ".join(str(args.get("question") or "").split())[:90]

    def run(self, args: dict, ctx: ToolContext) -> str:
        question = str(args.get("question") or "").strip()
        options = args.get("options") or []
        if not question or not isinstance(options, list) or len(options) < 2:
            raise ToolError("question and 2-4 options are required")
        allow_other = args.get("allow_other", True)
        labels = ctx.ui.choose(question, options, bool(args.get("multi_select")),
                               allow_other=bool(allow_other))
        if not labels:
            return "User skipped the question. Decide yourself with sensible defaults."
        custom = [lb for lb in labels if not any(
            isinstance(o, dict) and o.get("label") == lb for o in options)]
        if custom:
            return "User custom answer: " + "; ".join(custom)
        return "User selected: " + "; ".join(labels)
