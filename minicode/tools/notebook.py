"""NotebookEdit tool: edit Jupyter notebook (.ipynb) cells (stdlib json only)."""
from __future__ import annotations

import json
import uuid

from .base import Tool, ToolContext, ToolError

MODES = ("replace", "insert", "delete")


def _source_to_list(source: str) -> list:
    """nbformat stores source as a list of lines keeping trailing newlines."""
    lines = source.splitlines(keepends=True)
    return lines if lines else [""]


def _find_cell(cells: list, cell_id: str):
    for i, c in enumerate(cells):
        if isinstance(c, dict) and c.get("id") == cell_id:
            return i
    return None


class NotebookEditTool(Tool):
    name = "notebook_edit"
    kind = "write"
    description = ("Edit a Jupyter notebook (.ipynb) cell: replace, insert or delete. "
                   "Addresses cells by their id (or 0-based index). Replacing a code cell "
                   "clears its outputs.")
    input_schema = {
        "type": "object",
        "properties": {
            "notebook_path": {"type": "string", "description": "Path to the .ipynb file."},
            "cell_id": {"type": "string", "description": "Target cell id (or use cell_number)."},
            "cell_number": {"type": "integer", "description": "0-based cell index fallback."},
            "new_source": {"type": "string", "description": "New cell source (replace/insert)."},
            "cell_type": {"type": "string", "enum": ["code", "markdown"],
                          "description": "Cell type (insert, or change on replace)."},
            "edit_mode": {"type": "string", "enum": list(MODES), "description": "Default replace."},
        },
        "required": ["notebook_path", "new_source"],
    }

    def describe_call(self, args: dict) -> str:
        target = args.get("cell_id") or f"#{args.get('cell_number')}"
        return f"{args.get('notebook_path')} {args.get('edit_mode') or 'replace'} {target}"

    def mutated_path(self, args: dict, ctx: ToolContext):
        from .fs import _resolve
        return _resolve(ctx, str(args.get("notebook_path") or ""))

    def run(self, args: dict, ctx: ToolContext) -> str:
        from .fs import _resolve, _read_text, _write_text
        p = _resolve(ctx, str(args.get("notebook_path") or ""))
        if not p.exists():
            raise ToolError(f"notebook not found: {p}")
        if p.suffix.lower() != ".ipynb":
            raise ToolError("not a .ipynb file")
        try:
            nb = json.loads(_read_text(p))
        except json.JSONDecodeError as e:
            raise ToolError(f"invalid notebook JSON: {e}")
        cells = nb.get("cells")
        if not isinstance(cells, list):
            raise ToolError("notebook has no 'cells' array")
        mode = args.get("edit_mode") or "replace"
        if mode not in MODES:
            raise ToolError(f"edit_mode must be one of {MODES}")
        source = str(args.get("new_source") or "")
        cell_type = args.get("cell_type")
        if cell_type is not None and cell_type not in ("code", "markdown"):
            raise ToolError("cell_type must be code or markdown")

        if mode == "insert":
            idx = len(cells)
            cid = args.get("cell_id")
            if cid:
                at = _find_cell(cells, cid)
                if at is None:
                    raise ToolError(f"cell_id {cid!r} not found")
                idx = at + 1
            elif args.get("cell_number") is not None:
                idx = max(0, min(int(args["cell_number"]), len(cells)))
            cell = {"cell_type": cell_type or "code", "metadata": {},
                    "source": _source_to_list(source)}
            if cell["cell_type"] == "code":
                cell["execution_count"] = None
                cell["outputs"] = []
            if any("id" in c for c in cells):
                cell["id"] = uuid.uuid4().hex[:8]
            cells.insert(idx, cell)
            _write_text(p, json.dumps(nb, ensure_ascii=False, indent=1) + "\n")
            return f"inserted {cell['cell_type']} cell at index {idx} ({len(cells)} cells total)"

        idx = None
        if args.get("cell_id"):
            idx = _find_cell(cells, args["cell_id"])
            if idx is None:
                raise ToolError(f"cell_id {args['cell_id']!r} not found")
        elif args.get("cell_number") is not None:
            n = int(args["cell_number"])
            if not (0 <= n < len(cells)):
                raise ToolError(f"cell_number {n} out of range (0..{len(cells) - 1})")
            idx = n
        else:
            raise ToolError("provide cell_id or cell_number")

        if mode == "delete":
            removed = cells.pop(idx)
            _write_text(p, json.dumps(nb, ensure_ascii=False, indent=1) + "\n")
            return f"deleted cell {idx} ({removed.get('cell_type')}, {len(cells)} cells left)"

        cell = cells[idx]
        cell["source"] = _source_to_list(source)
        if cell_type:
            cell["cell_type"] = cell_type
        if cell.get("cell_type") == "code":
            cell["outputs"] = []
            cell["execution_count"] = None
        _write_text(p, json.dumps(nb, ensure_ascii=False, indent=1) + "\n")
        return f"replaced cell {idx} ({cell.get('cell_type')}, {len(cells)} cells total)"
