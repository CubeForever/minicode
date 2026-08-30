"""Todo list tool for multi-step tasks."""
from __future__ import annotations

from .base import Tool, ToolContext, ToolError

STATUSES = ("pending", "in_progress", "completed")


class TodoWriteTool(Tool):
    name = "todo_write"
    kind = "read"  # harmless — never needs confirmation
    description = ("Maintain a short task list for the current request. Rewrite the whole "
                   "list on each call. Use it for multi-step work; mark an item in_progress "
                   "before starting it and completed right after finishing it.")
    input_schema = {
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "Short imperative task."},
                        "status": {"type": "string", "enum": list(STATUSES)},
                        "priority": {"type": "string", "enum": ["high", "medium", "low"],
                                     "description": "Optional priority (default medium)."},
                    },
                    "required": ["content", "status"],
                },
            }
        },
        "required": ["todos"],
    }

    def describe_call(self, args: dict) -> str:
        return f"{len(args.get('todos') or [])} item(s)"

    def run(self, args: dict, ctx: ToolContext) -> str:
        todos = args.get("todos")
        if not isinstance(todos, list):
            raise ToolError("todos must be an array")
        clean = []
        for t in todos:
            if not isinstance(t, dict) or not t.get("content"):
                continue
            status = t.get("status") or "pending"
            if status not in STATUSES:
                status = "pending"
            priority = t.get("priority") or "medium"
            if priority not in ("high", "medium", "low"):
                priority = "medium"
            clean.append({"content": str(t["content"]), "status": status,
                          "priority": priority})
        ctx.session.todos = clean
        ctx.ui.todo_render(clean)
        return (f"todo list updated ({len(clean)} items)"
                + (" — keep at most one item in_progress" if
                   sum(1 for t in clean if t["status"] == "in_progress") > 1 else ""))
