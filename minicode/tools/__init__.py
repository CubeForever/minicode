from .base import Tool, ToolContext, ToolError, ToolRegistry, truncate_middle
from .fs import (EditFileTool, GlobTool, GrepTool, ListDirTool,
                 ReadFileTool, WriteFileTool)
from .shell import BashTool, BashKillTool, BashOutputTool, ShellState, detect_shell
from .subagent import DispatchAgentTool
from .todo import TodoWriteTool
from .webfetch import WebFetchTool
from .websearch import WebSearchTool
from .notebook import NotebookEditTool
from .patch import ApplyPatchTool
from .plan import ExitPlanTool
from .ask_user import AskUserTool
from .memory import BrainWriteTool
from .panel import ConsultPanelTool
from .skills import SkillTool

__all__ = [
    "Tool", "ToolContext", "ToolError", "ToolRegistry", "truncate_middle",
    "ReadFileTool", "WriteFileTool", "EditFileTool", "GlobTool", "GrepTool",
    "ListDirTool", "BashTool", "BashOutputTool", "BashKillTool",
    "ShellState", "detect_shell", "TodoWriteTool", "DispatchAgentTool",
    "WebFetchTool", "WebSearchTool", "NotebookEditTool", "ApplyPatchTool",
    "ExitPlanTool", "AskUserTool", "BrainWriteTool", "ConsultPanelTool",
    "SkillTool", "build_registry",
]


def build_registry(shell_state, read_only: bool = False) -> ToolRegistry:
    tools = [ReadFileTool(), GlobTool(), GrepTool(), ListDirTool(),
             WebFetchTool(), WebSearchTool(), ConsultPanelTool(), SkillTool()]
    if not read_only:
        tools += [WriteFileTool(), EditFileTool(), ApplyPatchTool(),
                  NotebookEditTool(),
                  BashTool(shell_state), BashOutputTool(shell_state),
                  BashKillTool(shell_state), TodoWriteTool(), DispatchAgentTool(),
                  ExitPlanTool(), AskUserTool(), BrainWriteTool()]
    return ToolRegistry(tools)
