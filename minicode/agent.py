"""The agent loop: model <-> tools. Includes plan mode, permission rules,
sensitive-path guard, hooks, file checkpoints, extended thinking, parallel
read-only tools."""
from __future__ import annotations

import difflib
import json
import re
import subprocess
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional, Tuple

from .guard import bash_guard_reason, file_guard_reason
from .llm import ToolUnsupportedError, tool_message, user_message
from .session import Session
from .tools.base import ToolContext, ToolError, summarize_result
from .ui import RawStream, Spinner, StreamRenderer

MODES = ("default", "accept-edits", "plan", "full-access")
MODE_ALIASES = {"yolo": "full-access"}  # legacy name kept for compatibility
AUTO_KINDS = {"read", "meta"}   # never prompt for these
WRITE_KINDS = {"write"}         # auto-approved in accept-edits mode
MUTATING_KINDS = {"write", "bash"}
PLAN_BLOCK_MSG = ("Plan mode is active: read-only. Investigate the codebase and present "
                  "an implementation plan; do not modify files.")
PLAN_SUFFIX = ("\n\n# Plan mode\n"
               "You are in PLAN MODE. Investigate the codebase with read-only tools, then "
               "present a concise implementation plan via the exit_plan tool (files to "
               "change, steps in order, how to verify), then STOP and wait for approval. "
               "Do not modify files or run mutating commands.")
EFFORT_THINKING = {"low": 4000, "medium": 10000, "high": 31999}

DESTRUCTIVE_PATTERNS = [
    re.compile(r"\brm\s+(?:[^;\n|]*\s)?-[a-zA-Z]*r", re.I),      # rm with -r/-rf/-fr
    re.compile(r"\bgit\s+push\b[^;\n|]*(-f\b|--force)"),
    re.compile(r"\bgit\s+reset\s+--hard"),
    re.compile(r"\bgit\s+clean\s+-[a-zA-Z]*f"),
    re.compile(r"\b(del\s+/[sq]|rd\s+/s)", re.I),
    re.compile(r"Remove-Item\s+[^;\n|]*-Recurse", re.I),
    re.compile(r"\b(mkfs|dd\s+if=|shutdown|reboot)\b", re.I),
    re.compile(r"\b(curl|wget|irm|invoke-webrequest|iwr)\b[^|;\n]*"
               r"\|\s*(?:sudo\s+)?(?:ba|z|da|fi)?sh\b", re.I),  # curl … | sh
    re.compile(r"\bfind\b[^;|\n]*-delete\b", re.I),              # find … -delete
    re.compile(r"\bxargs\b[^;|\n]*\brm\b", re.I),                # … | xargs rm
]

# Extended-thinking keyword budgets (same idea as Claude Code)
THINK_BUDGETS = (
    (re.compile(r"ultrathink", re.I), 31999),
    (re.compile(r"megathink|think hard(er)?|think intensely", re.I), 10000),
    (re.compile(r"\bthink\b", re.I), 4000),
)


class Interrupted(Exception):
    """协作式中断（终端 Esc / Web 停止按钮）。抛出时会话状态保持一致：
    已发出的工具调用都已回填结果，可以直接开始下一回合。"""


def thinking_budget(text: str) -> int:
    for rx, budget in THINK_BUDGETS:
        if rx.search(text or ""):
            return budget
    return 0


def rule_matches(rule: str, tool, args: dict) -> bool:
    """Permission rule: 'ToolName', 'ToolName*' wildcard, or 'Tool(prefix*)'
    for commands (case-insensitive, underscores ignored, so 'WebFetch'
    matches web_fetch and 'mcp__echo__*' matches every tool of a server)."""
    rule = str(rule).strip()
    if not rule:
        return False
    if "(" in rule:
        head, _, rest = rule.partition("(")
        prefix = rest.rstrip(")").strip()
        if head.strip().lower().replace("_", "") != tool.name.lower().replace("_", ""):
            return False
        if not prefix or prefix == "*":
            return True
        return str(args.get("command") or "").startswith(prefix.rstrip("*").strip())
    norm_rule = rule.lower().replace("_", "")
    norm_name = tool.name.lower().replace("_", "")
    if norm_rule.endswith("*"):
        return norm_name.startswith(norm_rule[:-1])
    return norm_name == norm_rule


class Agent:
    def __init__(self, provider, session: Session, ui, config, registry,
                 max_iterations: int = 40, checkpoints=None):
        self.provider = provider
        self.session = session
        self.ui = ui
        self.config = config
        self.registry = registry
        self.max_iterations = max_iterations
        self.checkpoints = checkpoints   # CheckpointManager or None
        self.system_prompt = ""
        self.tools_disabled = False      # set when the model rejects tools
        self._error_history = []         # (tool_name, error_signature) for repeat detection
        self._lint_cache = None          # None=未探测 / ""=探测过但无 / 模板
        self.turn_errors = 0             # 本回合工具错误数（经验引擎信号）
        self.last_verify_ok = None       # 最近一次自检门结果（True/False/未跑 None）
        self.interrupt_event = threading.Event()  # set → 协作式中断当前回合
        cwd = getattr(config, "cwd", None) or Path.cwd()
        self.ctx = ToolContext(cwd=cwd, config=config, session=session, ui=ui,
                               agent_factory=None)

    @property
    def mode(self) -> str:
        return self.config.mode

    def _interrupted(self) -> bool:
        return self.interrupt_event.is_set()

    def replace_session(self, session: Session) -> None:
        self.session = session
        self.ctx.session = session

    def reset_session(self) -> None:
        self.replace_session(Session())

    # ---------- main loop ----------

    def run_turn(self, user_text: str) -> str:
        self.interrupt_event.clear()   # 上一回合遗留的停止请求不带入新回合
        self.turn_errors = 0
        self.session.add(user_message(user_text))
        budget = 0
        if self.mode != "plan":
            budget = max(thinking_budget(user_text),
                         EFFORT_THINKING.get(getattr(self.config, "reasoning_effort", "")
                                             or "", 0))
        turn_tokens = 0
        turn_budget = int(getattr(self.config, "turn_budget", 0) or 0)
        for _ in range(self.max_iterations):
            if self._interrupted():
                raise Interrupted()
            try:
                chars_before = self.session.content_chars()
                msg, usage = self._step(budget)
            except ToolUnsupportedError:
                if not self.tools_disabled:
                    self.tools_disabled = True
                    self.ui.warn("该模型/中转不支持工具调用 —— 已降级为纯对话模式"
                                 "（无法读写文件与执行命令）。可换支持 tools 的模型。")
                    continue
                raise
            except Interrupted:
                raise
            except KeyboardInterrupt:
                self.ui.newline()
                self.ui.warn("已中断生成。")
                return ""
            self.session.add(msg)
            self.session.note_usage(usage)
            self.session.calibrate_usage(chars_before, usage)
            turn_tokens += ((usage or {}).get("input", 0)
                            + (usage.get("output", 0) if usage else 0))
            tool_calls = msg.get("tool_calls") or []
            if tool_calls and turn_budget and turn_tokens > turn_budget:
                self.ui.warn(f"已达本回合 token 预算（{turn_tokens:,}/{turn_budget:,}），"
                             "停止执行。可用 /cost 查看用量，或提高 --budget。")
                return ""
            if not tool_calls:
                text = msg.get("content") or ""
                if not text:
                    self.ui.warn("(模型返回了空回复)")
                return text
            results = None
            if len(tool_calls) > 1:
                try:
                    results = self._try_parallel(tool_calls)
                except KeyboardInterrupt:
                    self.ui.newline()
                    self.ui.warn("已中断并行工具执行。")
                    results = [("Interrupted by user", True)] * len(tool_calls)
            if results is None:
                results = []
                for tc in tool_calls:
                    if self._interrupted():
                        results.append(("Interrupted by user", True))
                        continue
                    try:
                        results.append(self._execute(tc))
                    except KeyboardInterrupt:
                        self.ui.newline()
                        self.ui.warn("已中断工具执行。")
                        results.append(("Interrupted by user", True))
            for tc, (content, is_error) in zip(tool_calls, results):
                c = content["blocks"] if isinstance(content, dict) else content
                self.session.add(tool_message(tc.get("id") or "", tc.get("name") or "",
                                              c, is_error))
            if self._interrupted():
                raise Interrupted()   # 所有工具结果已回填，会话状态一致
        self.ui.warn(f"已达单轮工具调用上限（{self.max_iterations} 次迭代）。")
        return ""

    def _step(self, thinking: int = 0) -> Tuple[dict, Optional[dict]]:
        """One model call. Returns (assistant_message, usage)."""
        system = self.system_prompt
        if self.mode == "plan":
            system += PLAN_SUFFIX
        schemas = [] if self.tools_disabled else self.registry.schemas()
        stream = self.provider.stream(self.session.messages, schemas, system, thinking)
        spinner = Spinner("Thinking")
        spinner.start()
        # 终端做 Markdown 转换；Web 桥接要原始文本（浏览器自己渲染）
        if getattr(self.ui, "wants_raw_markdown", False):
            renderer = RawStream(self.ui.stream_text)
        else:
            renderer = StreamRenderer(self.ui.stream_text)
        msg = None
        usage = None
        partial: list = []   # 已流出的正文，用于中断时保留部分回复
        try:
            for ev in stream:
                if self._interrupted():
                    break
                t = ev["type"]
                if t == "text_delta":
                    spinner.stop()
                    partial.append(ev["text"])
                    renderer.feed(ev["text"])
                elif t == "reasoning_delta":
                    spinner.stop()
                    self.ui.stream_reasoning(ev["text"])
                elif t == "tool_call":
                    spinner.stop()
                    renderer.flush()
                    self.ui.newline()  # terminate any streamed text line
                elif t == "finish":
                    spinner.stop()
                    renderer.flush()
                    msg = ev["message"]
                    usage = ev.get("usage")
        finally:
            spinner.stop()
            renderer.flush()
            close = getattr(stream, "close", None)
            if close:
                close()
        self.ui.newline()
        if msg is None:
            if self._interrupted():
                text = "".join(partial).strip()
                if text:   # 保留已流出的部分回复（与 Claude Code 行为一致）
                    return {"role": "assistant",
                            "content": text + "\n\n（回复被用户中断）"}, None
                raise Interrupted()
            raise RuntimeError("LLM stream ended without a finish event")
        return msg, usage

    # ---------- tools ----------

    def _attribute_failure(self, tool_name: str, error_msg: str) -> str:
        """Attach actionable suggestions to tool errors, and detect repeat failures.

        Returns the (possibly enriched) error message. When the same tool fails
        with the same signature 3+ times in a row, appends a strong nudge to
        change strategy instead of retrying blindly.
        """
        msg_lower = error_msg.lower()
        suggestions = []
        if any(k in msg_lower for k in ("no such file", "not found", "不存在", "找不到")):
            if tool_name in ("read_file", "edit_file", "write_file", "bash"):
                suggestions.append("先用 glob 或 list_dir 确认正确路径，注意相对路径基准是项目根目录")
        if any(k in msg_lower for k in ("permission", "权限", "access denied", "eacces")):
            suggestions.append("检查文件权限，或用 /add-dir 授权额外目录")
        if any(k in msg_lower for k in ("invalid", "json", "参数", "argument", "schema")):
            suggestions.append("检查工具参数是否匹配 input_schema，必要时用 /tools 查看定义")
        if any(k in msg_lower for k in ("timeout", "timed out", "超时")):
            suggestions.append("命令耗时过长，改用 bash(command, background=true) 后台执行")
        if any(k in msg_lower for k in ("stale", "modified since", "已被修改")):
            suggestions.append("文件在读取后被外部修改，先重新 read_file 再编辑")
        # repeat-failure detection: same tool + same error prefix
        sig = (tool_name, error_msg[:80])
        self._error_history.append(sig)
        self._error_history = self._error_history[-10:]
        self.turn_errors += 1
        repeat_count = sum(1 for s in self._error_history[-3:] if s == sig)
        if repeat_count >= 3:
            suggestions.append("同类错误已连续出现 3 次——停止重试，换策略或用 ask_user 向用户澄清")
        if suggestions:
            return error_msg + "\n\n[归因建议] " + "；".join(suggestions)
        return error_msg

    def _maybe_summarize(self, tool_name: str, result: str) -> str:
        """Apply tool-aware summarization to oversized results."""
        if not isinstance(result, str):
            return result
        limit = getattr(self.session, "result_limit", 30000)
        return summarize_result(tool_name, result, limit)

    def _try_parallel(self, tool_calls):
        """Run a batch of read-only tool calls concurrently. Returns a list of
        (content, is_error) in call order, or None when the batch isn't eligible."""
        if self._interrupted():
            return None   # 顺序路径会把每个调用统一标记为已中断
        planned = []
        for tc in tool_calls:
            tool = self.registry.get(tc.get("name") or "")
            if tool is None or tool.kind != "read":
                return None
            try:
                args = json.loads(tc.get("args") or "{}")
                if not isinstance(args, dict):
                    return None
            except json.JSONDecodeError:
                return None
            allowed, _reason = self._authorized(tool, args)
            if not allowed:
                return None  # rare (deny rule) — let the sequential path explain
            if self._run_hook("pre_tool_use", {"tool": tool.name, "args": args},
                              tool_name=tool.name)[0]:
                return None
            planned.append((tool, args))
        self.ui.newline()
        for tool, args in planned:
            self.ui.tool_line(tool.name, "∥ " + tool.describe_call(args))

        def run_one(item):
            tool, args = item
            try:
                r = tool.run(args, self.ctx)
            except ToolError as e:
                return self._attribute_failure(tool.name, f"Error: {e}"), True
            except Exception as e:
                return self._attribute_failure(tool.name,
                                               f"Error: {type(e).__name__}: {e}"), True
            if isinstance(r, dict) and "_blocks" in r:
                blocks = r["_blocks"]
                text = " ".join(b.get("text", "") for b in blocks
                                if b.get("type") == "text") or "(image)"
                return {"blocks": blocks, "text": text}, False
            return self._maybe_summarize(tool.name, r), False

        with Spinner(f"parallel ×{len(planned)}"):
            with ThreadPoolExecutor(max_workers=min(4, len(planned))) as ex:
                results = list(ex.map(run_one, planned))
        for (tool, args), (content, _err) in zip(planned, results):
            text = content["text"] if isinstance(content, dict) else content
            self.ui.tool_result_note(text)
            self._run_hook("post_tool_use", {"tool": tool.name, "args": args},
                           tool_name=tool.name)
        return results

    def _execute(self, tc: dict) -> Tuple[object, bool]:
        name = tc.get("name") or ""
        tool = self.registry.get(name)
        if tool is None:
            msg = (f"Error: unknown tool '{name}'. "
                   f"Available: {', '.join(self.registry.names())}")
            self.ui.tool_line(name or "?", "unknown tool")
            self.ui.tool_result_note(msg)
            return msg, True
        raw = tc.get("args") or "{}"
        try:
            args = json.loads(raw)
            if not isinstance(args, dict):
                raise ValueError("arguments must be a JSON object")
        except (json.JSONDecodeError, ValueError) as e:
            msg = f"Error: invalid tool arguments: {e}. Raw: {raw[:300]}"
            self.ui.tool_result_note(msg)
            return msg, True
        blocked, _out, hook_approved = self._run_hook(
            "pre_tool_use", {"tool": tool.name, "args": args}, tool_name=tool.name)
        if blocked:
            self.ui.tool_line(tool.name, "blocked by hook")
            msg = f"Blocked by pre_tool_use hook: {blocked}"
            self.ui.tool_result_note(msg)
            return msg, True
        if tool.name == "bash":
            command = str(args.get("command") or "")
            if any(p.search(command) for p in DESTRUCTIVE_PATTERNS):
                self.ui.warn(f"高危命令：{' '.join(command.split())[:100]}")
        allowed, reason = self._authorized(tool, args, hook_approved=hook_approved)
        if not allowed:
            self.ui.tool_line(tool.name, reason or "declined")
            return (reason or
                    "User declined this tool call. Do not retry it unless the user asks.",
                    bool(reason))
        self.ui.tool_line(tool.name, tool.describe_call(args))
        targets: list = []
        before_map: dict = {}
        if tool.kind == "write":
            for getter in ("mutated_paths", "mutated_path"):
                fn = getattr(tool, getter, None)
                if fn is not None:
                    v = fn(args, self.ctx)
                    targets = v if isinstance(v, list) else ([v] if v else [])
                    break
            for target in targets:
                if self.checkpoints is not None:
                    try:
                        self.checkpoints.snapshot(Path(target), tool.name)
                    except OSError:
                        pass
                before_map[str(target)] = self._read_text_safe(Path(target))
        try:
            result = tool.run(args, self.ctx)
        except ToolError as e:
            msg = self._attribute_failure(tool.name, f"Error: {e}")
            self.ui.tool_result_note(msg)
            return msg, True
        except KeyboardInterrupt:
            raise
        except Exception as e:
            if getattr(self.config, "debug", False):
                traceback.print_exc()
            msg = self._attribute_failure(tool.name,
                                          f"Error: {type(e).__name__}: {e}")
            self.ui.tool_result_note(msg)
            return msg, True
        if isinstance(result, dict) and "_blocks" in result:
            blocks = result["_blocks"]
            text = " ".join(b.get("text", "") for b in blocks
                            if b.get("type") == "text") or "(image)"
            self.ui.tool_result_note(text)
            _hmsg, hout, _ok = self._run_hook("post_tool_use",
                                              {"tool": tool.name, "args": args},
                                              tool_name=tool.name)
            if hout:
                blocks.append({"type": "text", "text": f"[hook feedback] {hout[:300]}"})
            return {"blocks": blocks}, False
        result = self._maybe_summarize(tool.name, result)
        # 自动放行的编辑也要看得见变更（Claude Code 行为：自动 ≠ 不可见）
        self._show_edit_diff(targets, before_map)
        # Aider 式快速回路：编辑落盘立即 lint，报错当场喂回模型自修
        if isinstance(result, str):
            lint_feedback = self._lint_targets(targets)
            if lint_feedback:
                result += lint_feedback
        self.ui.tool_result_note(result)
        _hmsg, hout, _ok = self._run_hook("post_tool_use",
                                          {"tool": tool.name, "args": args},
                                          tool_name=tool.name)
        if hout and isinstance(result, str):
            result += f"\n[hook feedback] {hout[:300]}"
        return result, False

    @staticmethod
    def _read_text_safe(path: Path) -> Optional[str]:
        try:
            if path.is_file():
                return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
        return None

    def _show_edit_diff(self, targets: list, before_map: dict) -> None:
        """Auto-approved edits render a compact diff instead of a bare summary."""
        if not targets or self.mode not in ("accept-edits", "full-access"):
            return
        chunks = []
        for t in targets[:5]:
            p = Path(t)
            after = self._read_text_safe(p)
            before = before_map.get(str(p))
            if before == after:
                continue
            rel = self._rel_display(p, self.ctx.cwd)
            diff = list(difflib.unified_diff(
                (before or "").splitlines(), (after or "").splitlines(),
                fromfile=f"{rel} (before)", tofile=f"{rel} (after)",
                lineterm=""))
            if diff:
                chunks.append("\n".join(diff[:30]))
        if chunks:
            self.ui.tool_diff("\n\n".join(chunks)[:4000])

    def _lint_targets(self, targets: list) -> str:
        """Run the project linter on just-mutated files; failures come back
        as [lint] feedback appended to the tool result (advisory, non-blocking)."""
        if not targets:
            return ""
        template = getattr(self.config, "lint_command", "") or self._lint_cache
        if template is None:
            from .lint import detect_lint_command
            self._lint_cache = detect_lint_command(Path(self.ctx.cwd))
            template = self._lint_cache
        if not template:
            return ""
        from .lint import render_command, run_lint
        cmd = render_command(template, [str(t) for t in targets], Path(self.ctx.cwd))
        out = run_lint(Path(self.ctx.cwd), cmd)
        if out:
            return ("\n\n[lint] 项目 linter 对刚修改的文件报错：\n" + out
                    + "\n请立即修复上述 lint 问题后继续。")
        return ""

    def _guard_reason(self, tool, args: dict) -> Optional[str]:
        """Sensitive-path guard: fires before yolo/confirm for critical assets."""
        if tool.kind == "write":
            for getter in ("mutated_paths", "mutated_path"):
                fn = getattr(tool, getter, None)
                if fn is None:
                    continue
                try:
                    targets = fn(args, self.ctx)
                except ToolError:
                    return None  # parse problems surface later in run()
                targets = targets if isinstance(targets, list) else \
                    ([targets] if targets else [])
                for t in targets:
                    reason = file_guard_reason(Path(t), self.ctx.cwd)
                    if reason:
                        return reason
                break
            return None
        if tool.name == "bash":
            return bash_guard_reason(str(args.get("command") or ""))
        return None

    def _guard_confirm(self, reason: str, tool, args: dict) -> Tuple[bool, str]:
        """Forced confirmation for high-risk operations — never bypassed by
        any mode; auto-declined in non-interactive sessions."""
        if not sys.stdin.isatty():
            return False, f"{reason}（非交互模式自动拒绝）"
        ans = self.ui.confirm(f"⚠ {reason}", tool.preview(args, self.ctx))
        return (ans in ("y", "a")), ""

    @staticmethod
    def _is_destructive(args: dict) -> bool:
        command = str(args.get("command") or "")
        return any(p.search(command) for p in DESTRUCTIVE_PATTERNS)

    @staticmethod
    def _bash_prefix(args: dict) -> str:
        parts = str(args.get("command") or "").strip().split()
        return parts[0] if parts else ""

    def _auto_approved_matches(self, tool, args: dict) -> bool:
        """'a'（本次总是）的记录：普通工具按 kind，bash 按首词前缀
        （`bash:npm` 匹配 npm 开头的命令），避免批准一条命令放行所有命令。"""
        if tool.kind in self.session.auto_approved:
            return True
        if tool.name == "bash":
            command = str(args.get("command") or "").strip()
            for entry in self.session.auto_approved:
                if entry.startswith("bash:"):
                    pfx = entry[5:]
                    if command == pfx or command.startswith(pfx + " "):
                        return True
        return False

    @staticmethod
    def _is_under(path: Path, root: Path) -> bool:
        try:
            Path(path).resolve().relative_to(Path(root).resolve())
            return True
        except (ValueError, OSError):
            return False

    def _rel_display(self, path, cwd) -> str:
        try:
            return str(Path(path).resolve().relative_to(Path(cwd).resolve())) or "."
        except (ValueError, OSError):
            return str(path)

    def _workspace_violation(self, tool, args: dict) -> Optional[str]:
        """workspace_lock 开启时，写操作的目标落在本工作区（及 /add-dir 授权
        目录）之外 → 硬拒绝。bash 无法可靠静态判定，不在其列（与终端同一局限）。"""
        extra = [Path(d) for d in (getattr(self.config, "extra_dirs", None) or [])]
        cwd = Path(self.config.cwd or Path.cwd())
        for getter in ("mutated_paths", "mutated_path"):
            fn = getattr(tool, getter, None)
            if fn is None:
                continue
            try:
                targets = fn(args, self.ctx)
            except ToolError:
                return None  # 参数问题由 run() 报错
            targets = targets if isinstance(targets, list) else \
                ([targets] if targets else [])
            for t in targets:
                if not t:
                    continue
                if not self._is_under(Path(t), cwd) and \
                        not any(self._is_under(Path(t), e) for e in extra):
                    return (f"工作区边界：{self._rel_display(t, cwd)} 在工作区之外"
                            "（Web 工作区已锁定；可用 /add-dir 授权额外目录）")
            break
        return None

    def _authorized(self, tool, args: dict, hook_approved: bool = False) -> Tuple[bool, str]:
        perms = getattr(self.config, "permissions", {}) or {}
        for rule in (perms.get("deny") or []):   # deny rules are hard blocks, even in yolo
            if rule_matches(rule, tool, args):
                return False, f"denied by permission rule: {rule}"
        # Web 工作区锁定：deny 之后立即生效，任何模式（含 yolo）都不放行
        if getattr(self.config, "workspace_lock", False) and tool.kind == "write":
            bad = self._workspace_violation(tool, args)
            if bad:
                return False, bad
        # sensitive-path guard: force confirmation in every mode
        guard = self._guard_reason(tool, args)
        if guard:
            return self._guard_confirm(f"敏感路径保护：{guard}", tool, args)
        # full-access: everything runs automatically, except high-risk operations
        if self.mode in ("full-access", "yolo"):
            if tool.name == "bash" and self._is_destructive(args):
                return self._guard_confirm(
                    "高危操作：检测到破坏性命令，需要你本人确认", tool, args)
            return True, ""
        if tool.kind in AUTO_KINDS:
            return True, ""
        if self.mode == "plan" and tool.kind in MUTATING_KINDS:
            return False, PLAN_BLOCK_MSG
        for rule in getattr(self.session, "plan_allowed_rules", []):
            if rule_matches(rule, tool, args):  # pre-approved by plan approval
                return True, ""
        for rule in (perms.get("allow") or []):
            if rule_matches(rule, tool, args):
                return True, ""
        if self._auto_approved_matches(tool, args):
            if tool.name == "bash" and self._is_destructive(args):
                # 前缀预授权不放行破坏性命令（如批准了 npm，git push -f 仍要确认）
                return self._guard_confirm(
                    "高危操作：检测到破坏性命令，需要你本人确认", tool, args)
            return True, ""
        if self.mode == "accept-edits" and tool.kind in WRITE_KINDS:
            return True, ""
        # pre_tool_use hook 的 approve：只跳过标准确认——deny / 工作区锁 /
        # 敏感路径门禁 / plan 只读在上面已全部先行判定，安全边界不变
        if hook_approved:
            return True, ""
        ans = self.ui.confirm(f"允许 {tool.name}？", tool.preview(args, self.ctx))
        if ans == "a":
            if tool.name == "bash":
                self.session.auto_approved.add(f"bash:{self._bash_prefix(args)}")
            else:
                self.session.auto_approved.add(tool.kind)
            return True, ""
        return (ans == "y"), ""

    def _run_hook(self, event: str, payload: dict,
                  tool_name: str = "") -> Tuple[Optional[str], str, bool]:
        """运行一条 hook 规则链。返回 (blocked_reason, stdout, approved)。

        配置支持三种形态（向后兼容）：
            "cmd"                              单命令，无 matcher
            {"matcher": "Bash|edit_file", "command": "...", "timeout": 10}
            [ 上面两种的列表 ]                  多条规则顺序执行

        阻断：非零退出码（stderr 为原因）或 stdout JSON
            {"decision": "block", "reason": "..."}
        approve：stdout JSON {"decision": "approve"} 仅对 pre_tool_use 生效，
            跳过标准权限确认——但 deny 规则 / 工作区锁 / 敏感路径门禁 /
            plan 只读约束不受影响，hook 永远不能放宽安全边界。
        带 matcher 的规则只作用于工具事件（按工具名正则，大小写不敏感）。
        """
        cfg = getattr(self.config, "hooks", {}) or {}
        rules = self._hook_rules(cfg.get(event))
        if not rules:
            return None, "", False
        body = json.dumps({"hook": event, **payload}, ensure_ascii=False)
        combined: list = []
        approved = False
        for rule in rules:
            if rule["matcher"]:
                if not tool_name or not re.search(rule["matcher"], tool_name,
                                                  re.I):
                    continue
            try:
                r = subprocess.run(rule["command"], shell=True,
                                   input=body.encode("utf-8"),
                                   capture_output=True, timeout=rule["timeout"])
            except subprocess.TimeoutExpired:
                return f"hook timeout after {rule['timeout']}s", "", False
            except Exception as e:
                return f"hook failed: {e}", "", False
            try:
                from .tools.shell import _decode
                out = _decode(r.stdout or b"").strip()
                err = _decode(r.stderr or b"").strip()
            except Exception:
                out, err = "", ""
            if r.returncode != 0:
                return (err[:500] or out[:200] or "blocked"), "", False
            decision = {}
            if out.startswith("{"):
                try:
                    d = json.loads(out)
                    if isinstance(d, dict):
                        decision = d
                except json.JSONDecodeError:
                    pass
            if decision.get("decision") == "block":
                return (str(decision.get("reason") or "blocked by hook"),
                        "", False)
            if decision.get("decision") == "approve" and event == "pre_tool_use":
                approved = True
            if out:
                combined.append(out[:300])
        return None, "\n".join(combined), approved

    @staticmethod
    def _hook_rules(spec) -> list:
        if spec is None:
            return []
        if isinstance(spec, str):
            return [{"matcher": None, "command": spec, "timeout": 30}]
        rules = []
        items = spec if isinstance(spec, list) else [spec]
        for item in items:
            if isinstance(item, str):
                rules.append({"matcher": None, "command": item, "timeout": 30})
            elif isinstance(item, dict) and item.get("command"):
                try:
                    timeout = max(1, int(item.get("timeout") or 30))
                except (TypeError, ValueError):
                    timeout = 30
                rules.append({"matcher": item.get("matcher"),
                              "command": str(item["command"]),
                              "timeout": timeout})
        return rules

    # ---------- compaction ----------

    def _carry_over(self) -> str:
        """Mechanically preserved structured state for compaction."""
        parts = []
        if self.checkpoints and self.checkpoints.entries:
            files = sorted({e["path"] for e in self.checkpoints.entries})
            parts.append("## 本会话修改过的文件\n"
                         + "\n".join(f"- {p}" for p in files))
        if self.session.todos:
            todo_text = "\n".join(f"- [{t.get('status')}] {t.get('content')}"
                                  for t in self.session.todos)
            parts.append("## 任务清单\n" + todo_text)
        commands = []
        for m in self.session.messages:
            for tc in m.get("tool_calls") or []:
                if tc.get("name") == "bash":
                    try:
                        cmd = json.loads(tc.get("args") or "{}").get("command") or ""
                    except json.JSONDecodeError:
                        continue
                    one = " ".join(cmd.split())
                    if one and one not in commands:
                        commands.append(one)
        if commands:
            shown = commands[:20]
            parts.append("## 执行过的关键命令\n"
                         + "\n".join(f"- {c[:120]}" for c in shown))
        return "\n\n".join(parts)

    def compact(self, instructions: Optional[str] = None) -> dict:
        self._run_hook("pre_compact",
                       {"context_tokens": self.session.context_tokens()})
        with Spinner("Compacting context"):
            stats = self.session.compact(self.provider, instructions,
                                         carry_over=self._carry_over())
        parts = []
        if stats["summary_chars"]:
            parts.append(f"摘要 {stats['summary_chars']} 字符")
        if stats["digest_chars"]:
            parts.append(f"操作骨架 {stats['digest_chars']} 字符")
        detail = " + ".join(parts) or "结构化状态"
        self.ui.info(f"上下文已压缩：{stats['before']} 条消息 → {detail}，"
                     f"最近 {stats['kept_turns']} 个回合保留原文")
        return stats
