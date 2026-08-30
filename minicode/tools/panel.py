"""consult_panel — a roundtable of independent subagent perspectives.

Parallel by default (3 subagents at once), with an optional debate mode:
each perspective sees the others' reports and writes a rebuttal/final
position before the main agent synthesizes.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from .base import Tool, ToolContext, ToolError
from ..ui import Spinner

DEFAULT_PERSPECTIVES = [
    "务实派：最短可行路径，最小改动，能落地",
    "架构派：长期结构、可维护性、耦合与扩展",
    "风险猎手：边界条件、失败模式、生产环境会坏在哪",
]
MAX_WORKERS = 3


class ConsultPanelTool(Tool):
    name = "consult_panel"
    kind = "read"
    description = ("Roundtable: convene independent subagents in PARALLEL, each arguing "
                   "from a different perspective (default: pragmatic / architecture / "
                   "risk), and get all reports to synthesize. With debate=true, a second "
                   "parallel round lets each perspective see and rebut the others before "
                   "final positions. Use for design decisions, tricky root-causes, risky "
                   "plans. Costly (multiple LLM calls) — not for simple lookups.")
    input_schema = {
        "type": "object",
        "properties": {
            "question": {"type": "string",
                         "description": "The question for the panel, with enough context "
                                        "to answer standalone."},
            "perspectives": {"type": "array", "items": {"type": "string"},
                             "minItems": 2, "maxItems": 4,
                             "description": "Custom perspective hints (default 3 built-in)."},
            "debate": {"type": "boolean",
                       "description": "Enable a second rebuttal round (default false)."},
        },
        "required": ["question"],
    }

    def describe_call(self, args: dict) -> str:
        mode = "辩论" if args.get("debate") else "并行"
        return f"{mode} " + " ".join(str(args.get("question") or "").split())[:80]

    def run(self, args: dict, ctx: ToolContext) -> str:
        question = str(args.get("question") or "").strip()
        if not question:
            raise ToolError("question is required")
        if ctx.agent_factory is None:
            raise ToolError("subagents are not available in this mode")
        perspectives = list(args.get("perspectives") or DEFAULT_PERSPECTIVES)
        if not (2 <= len(perspectives) <= 4):
            raise ToolError("perspectives must have 2-4 entries")

        def ask(spec: str) -> str:
            prompt = (f"圆桌讨论，视角【{spec}】。\n问题：{question}\n"
                      "只从你被指派的视角给出独立分析与具体建议，简洁、有结论。")
            return (ctx.agent_factory(prompt) or "(no output)").strip()

        with Spinner(f"圆桌并行 ×{len(perspectives)}"):
            with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(perspectives))) as ex:
                reports = list(ex.map(ask, perspectives))

        sections = [f"### 视角 {i}：{spec}\n{r}"
                    for i, (spec, r) in enumerate(zip(perspectives, reports), 1)]

        if args.get("debate"):
            def rebuttal(i: int) -> str:
                others = "\n\n".join(f"【{perspectives[j]}】\n{reports[j]}"
                                     for j in range(len(reports)) if j != i)
                prompt = (f"圆桌辩论轮。问题：{question}\n你的视角：【{perspectives[i]}】\n"
                          f"你第一轮的发言：\n{reports[i]}\n\n其他视角的发言：\n{others}\n\n"
                          "针对与你相左的观点进行反驳或修正，给出你的最终立场（简洁）。")
                return (ctx.agent_factory(prompt) or "(no output)").strip()

            with Spinner(f"辩论轮 ×{len(perspectives)}"):
                with ThreadPoolExecutor(max_workers=min(MAX_WORKERS,
                                                       len(perspectives))) as ex:
                    finals = list(ex.map(rebuttal, range(len(perspectives))))
            sections += [f"### 辩论后终稿：{perspectives[i]}\n{f}"
                         for i, f in enumerate(finals)]

        tail = ("圆桌报告齐了（含辩论终稿）。综合以下内容，给出你自己的最终结论"
                "（不要逐字复述）：\n\n") if args.get("debate") else \
               ("圆桌报告齐了。综合以下视角，给出你自己的最终结论（不要逐字复述）：\n\n")
        return tail + "\n\n".join(sections)
