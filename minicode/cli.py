"""minicode CLI: argument parsing, REPL, slash commands (builtin + custom),
MCP wiring, custom subagents, plan gate."""
from __future__ import annotations

import argparse
import atexit
import dataclasses
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import __version__
from .agent import MODES, Agent
from .checkpoints import CheckpointManager
from .config import load_config
from .llm import LLMError, make_provider
from .mcp import McpManager
from .prompts import SUBAGENT_PROMPT, REVIEW_PROMPT, build_system_prompt
from .session import SESSIONS_DIR, Session
from .tools import build_registry
from .tools.base import ToolContext, ToolRegistry
from .tools.shell import ShellState, detect_shell
from .ui import MODE_LABELS, Spinner, SubUI, UI, _fmt_tokens, cyan, gray, green, red, yellow

BUILTIN_COMMANDS = {
    "review": ("审查代码改动（git diff 或指定文件）", REVIEW_PROMPT),
}



class SilentUI(UI):
    """Suppresses all terminal output — used for -p --output-format json."""

    def stream_text(self, text):
        pass

    def stream_reasoning(self, text):
        pass

    def tool_line(self, name, summary="", prefix="  ● "):
        pass

    def tool_result_note(self, text):
        pass

    def info(self, msg):
        pass

    def warn(self, msg):
        pass

    def plain(self, msg=""):
        pass

    def token_note(self, *a, **k):
        pass

    def banner(self, *a, **k):
        pass


def _parse_args(argv=None):
    ap = argparse.ArgumentParser(
        prog="minicode",
        description="minicode — Claude Code 风格的终端编码智能体（零依赖）")
    ap.add_argument("prompt", nargs="*", help="启动时立即执行的提示词")
    ap.add_argument("-p", "--print", action="store_true",
                    help="非交互模式：执行提示词后退出（可配合管道输入）")
    ap.add_argument("--output-format", choices=["text", "json", "stream-json"],
                    default="text", help="-p 模式输出格式（默认 text）")
    ap.add_argument("--model", help="覆盖模型名称")
    ap.add_argument("--provider", choices=["openai", "anthropic"], help="覆盖 API 提供方")
    ap.add_argument("--yolo", "--dangerously-skip-permissions", dest="yolo",
                    action="store_true", help="自动批准所有工具调用")
    ap.add_argument("--allowed-tools", metavar="RULES",
                    help="逗号分隔的允许规则，如 'Bash(git *),web_fetch'")
    ap.add_argument("--disallowed-tools", metavar="RULES",
                    help="逗号分隔的禁止规则")
    ap.add_argument("--append-system-prompt", metavar="TEXT",
                    help="在系统提示词后追加内容")
    ap.add_argument("--profile", metavar="NAME",
                    help="使用配置文件中的 profiles.NAME 配置")
    ap.add_argument("--budget", type=int, metavar="TOKENS",
                    help="单回合 token 预算上限（超限自动停止）")
    ap.add_argument("--probe", action="store_true",
                    help="对当前 API 端点做兼容性探测（models/非流式/流式/工具调用）后退出")
    ap.add_argument("--serve", action="store_true",
                    help="以本地 HTTP API 模式运行（127.0.0.1，token 鉴权）")
    ap.add_argument("--port", type=int, default=8765, help="serve 模式端口（默认 8765）")
    ap.add_argument("-c", "--continue", dest="continue_last", action="store_true",
                    help="恢复上一次会话")
    ap.add_argument("--resume", action="store_true", help="交互式选择历史会话")
    ap.add_argument("--cwd", help="工作目录")
    ap.add_argument("--version", action="version", version=f"minicode {__version__}")
    return ap.parse_args(argv)


def _custom_commands() -> dict:
    out = {}
    for d in (Path.home() / ".minicode" / "commands", Path.cwd() / ".minicode" / "commands"):
        try:
            if not d.is_dir():
                continue
            for f in sorted(d.glob("*.md")):
                text = f.read_text(encoding="utf-8", errors="replace")
                desc, body = "", text
                if text.startswith("---"):
                    parts = text.split("---", 2)
                    if len(parts) == 3:
                        for ln in parts[1].splitlines():
                            if ln.lower().startswith("description:"):
                                desc = ln.split(":", 1)[1].strip()
                        body = parts[2]
                if not desc:
                    for ln in body.splitlines():
                        s = ln.strip()
                        if s and not s.startswith("#"):
                            desc = s[:60]
                            break
                out.setdefault(f.stem.lower(), (desc, body.strip()))
        except OSError:
            continue
    for k, v in BUILTIN_COMMANDS.items():
        out.setdefault(k, v)
    return out


def _custom_agents() -> dict:
    """Load .minicode/agents/*.md — frontmatter: description, tools, model."""
    out = {}
    for d in (Path.home() / ".minicode" / "agents", Path.cwd() / ".minicode" / "agents"):
        try:
            if not d.is_dir():
                continue
            for f in sorted(d.glob("*.md")):
                text = f.read_text(encoding="utf-8", errors="replace")
                desc, tools, model, body = "", "", "", text
                if text.startswith("---"):
                    parts = text.split("---", 2)
                    if len(parts) == 3:
                        for ln in parts[1].splitlines():
                            low = ln.lower()
                            if low.startswith("description:"):
                                desc = ln.split(":", 1)[1].strip()
                            elif low.startswith("tools:"):
                                tools = ln.split(":", 1)[1].strip()
                            elif low.startswith("model:"):
                                model = ln.split(":", 1)[1].strip()
                        body = parts[2]
                if not desc:
                    for ln in body.splitlines():
                        s = ln.strip()
                        if s and not s.startswith("#"):
                            desc = s[:60]
                            break
                out[f.stem.lower()] = {"name": f.stem, "description": desc,
                                       "tools": tools, "model": model,
                                       "prompt": body.strip()}
        except OSError:
            continue
    return out


def _project_trust_gate(cfg, ui) -> tuple:
    """项目自带的插件/钩子是第三方代码：首次遇到必须确认，
    非交互模式默认不加载（安全默认），信任后写入 ~/.minicode/trusted/。"""
    tools_dir = Path.cwd() / ".minicode" / "tools"
    has_plugins = tools_dir.is_dir() and any(tools_dir.glob("*.py"))
    project_hooks = cfg.hooks_from_project or {}
    if not has_plugins and not project_hooks:
        return True, False
    import hashlib
    key = hashlib.sha256(str(Path.cwd().resolve()).encode("utf-8")).hexdigest()[:16]
    marker = Path.home() / ".minicode" / "trusted" / f"{key}.json"
    if marker.exists():
        return True, False
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        ui.warn("检测到项目自带插件/钩子（第三方代码）。非交互模式默认不加载。")
        _strip_project_hooks(cfg)
        return False, False
    what = "、".join(x for x in ("插件 (.minicode/tools)" if has_plugins else "",
                                 "钩子 (project hooks)" if project_hooks else "") if x)
    ui.warn(f"此项目包含第三方代码：{what}——可能由他人编写，执行前请确认。")
    try:
        ans = input(yellow("  信任并加载？[y] 是 / [n] 本次否 ❯ ")).strip().lower()
    except (EOFError, KeyboardInterrupt):
        ui.plain()
        ans = "n"
    if ans.startswith("y"):
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(json.dumps(
                {"trusted": True, "time": time.strftime("%Y-%m-%d %H:%M")}),
                encoding="utf-8")
        except OSError:
            pass
        return True, True
    ui.info("已跳过项目插件与钩子（仅本次；下次仍会询问）。")
    _strip_project_hooks(cfg)
    return False, False


def _strip_project_hooks(cfg):
    for k, v in (cfg.hooks_from_project or {}).items():
        if (cfg.hooks or {}).get(k) == v:
            cfg.hooks.pop(k, None)


def _build_agent(cfg, provider, session, ui, mcp_manager=None) -> Agent:
    shell_state = ShellState(cfg.cwd, detect_shell(cfg.shell))
    ckpt_root = (Path.home() / ".minicode" / "checkpoints"
                 / time.strftime("%Y%m%d-%H%M%S"))
    base_tools = list(build_registry(shell_state).tools.values())
    if mcp_manager is not None:
        base_tools += mcp_manager.connect_all()
    allow_plugins, _trusted_now = _project_trust_gate(cfg, ui)
    if allow_plugins:
        from .plugins import load_plugin_tools
        plugin_tools, plugin_errors = load_plugin_tools(cfg.cwd)
        base_tools += plugin_tools
        for fname, err in plugin_errors:
            ui.warn(f"插件加载失败 {fname}：{err}")
        for t in plugin_tools:
            ui.info(f"已加载本地插件工具：{t.name}")
    agents = _custom_agents()

    def factory(prompt: str, subagent_type: str = None) -> str:
        sub_ui = SubUI(ui)
        sub_session = Session()
        defn = agents.get((subagent_type or "").lower())
        if defn is not None:
            sub_provider = provider
            if defn.get("model"):
                sub_provider = make_provider(dataclasses.replace(cfg,
                                                                 model=defn["model"]))
            all_tools = build_registry(shell_state)
            allowed = {t.strip().lower().replace("_", "")
                       for t in (defn.get("tools") or "").split(",") if t.strip()}
            if allowed:
                picked = [t for t in all_tools.tools.values()
                          if t.name.lower().replace("_", "") in allowed]
                tools = picked or list(build_registry(shell_state,
                                                      read_only=True).tools.values())
            else:
                tools = list(build_registry(shell_state, read_only=True).tools.values())
            sub_registry = ToolRegistry(tools)
            sub_system = defn["prompt"] or SUBAGENT_PROMPT.format(cwd=cfg.cwd)
        else:
            sub_provider = provider
            sub_registry = build_registry(shell_state, read_only=True)
            sub_system = SUBAGENT_PROMPT.format(cwd=cfg.cwd)
        sub = Agent(sub_provider, sub_session, sub_ui, cfg, sub_registry,
                    max_iterations=15)
        sub.ctx = ToolContext(cwd=cfg.cwd, config=cfg, session=sub_session,
                              ui=sub_ui, agent_factory=None)
        sub.system_prompt = sub_system
        return sub.run_turn(prompt)

    agent = Agent(provider, session, ui, cfg, ToolRegistry(base_tools),
                  checkpoints=CheckpointManager(ckpt_root))
    agent.ctx.agent_factory = factory
    from .tools.skills import skills_section_text
    agent.system_prompt = (build_system_prompt(cfg, cfg.cwd, agents)
                           + skills_section_text(cfg.cwd))
    append = (args_state.get("append_system_prompt") or "") if args_state else ""
    if append:
        agent.system_prompt += "\n\n" + append
    agent.mcp = mcp_manager
    agent._prompt_cwd = cfg.cwd
    agent._custom_agents = agents
    return agent


args_state = {}

def _save(agent: Agent) -> None:
    s = agent.session
    first = next((m.get("content") or "" for m in s.messages if m.get("role") == "user"), "")
    try:
        s.save_auto(first)
    except OSError:
        pass


def _after_turn(agent: Agent) -> None:
    _save(agent)
    ui, cfg, s = agent.ui, agent.config, agent.session
    ctx_tokens = s.context_tokens()  # real usage, or estimate when provider omits it
    if cfg.context_limit and ctx_tokens > 0:
        pct = ctx_tokens / cfg.context_limit * 100
        ui.token_note(s.total_usage["input"] or s.approx_tokens(),
                      s.total_usage["output"], pct)
    if s.todos:
        done = sum(1 for t in s.todos if t.get("status") == "completed")
        pending = len(s.todos) - done
        ui.plain(gray(f"  ☐ todos：{done}/{len(s.todos)} 完成"
                      f"（待办 {pending}）—— /todos 查看"))
    # microcompaction: elide big old tool results well before auto-compact
    if cfg.context_limit and ctx_tokens > cfg.context_limit * 0.6:
        elided = s.elide_old_tool_results()
        if elided:
            ui.info(f"已瘦身 {elided} 条旧工具结果（microcompaction），为上下文腾出空间")
    if cfg.context_limit and ctx_tokens > cfg.context_limit * 0.8:
        ui.warn(f"上下文已用 {ctx_tokens:,} / {cfg.context_limit:,} tokens，自动压缩…")
        try:
            agent.compact()
        except (LLMError, RuntimeError) as e:
            ui.error(f"压缩失败：{e}")
    _hmsg, _hout = agent._run_hook("turn_end", {"messages": len(s.messages)})


def _run_turn(agent: Agent, text: str) -> str:
    blocked, _hout = agent._run_hook("user_prompt_submit", {"prompt": text})
    if blocked:
        agent.ui.warn(f"user_prompt_submit hook 拦截了该提示词：{blocked}")
        return "blocked"
    n0 = len(agent.checkpoints.entries) if agent.checkpoints else 0
    try:
        agent.run_turn(text)
        status = "ok"
    except LLMError as e:
        agent.ui.error(str(e))
        status = "error"
    except KeyboardInterrupt:
        agent.ui.warn("已中断。")
        status = "interrupted"
    except Exception as e:  # keep the REPL alive on unexpected errors
        agent.ui.error(f"{type(e).__name__}: {e}")
        status = "error"
    _after_turn(agent)
    _postmortem(agent, status)
    if status == "ok":
        mutated = bool(agent.checkpoints and len(agent.checkpoints.entries) > n0)
        _self_verify(agent, mutated)
    return status


def _self_verify(agent: Agent, mutated: bool) -> None:
    """The self-verify gate: after file mutations, run the configured verify
    command; on failure feed the output back to the agent to fix (max 2 rounds)."""
    cfg = agent.config
    cmd = getattr(cfg, "verify_command", "")
    if not cmd or not mutated or agent.checkpoints is None:
        return
    ui = agent.ui

    def run_verify() -> tuple:
        try:
            proc = subprocess.run(cmd, shell=True, capture_output=True,
                                  cwd=str(cfg.cwd), timeout=max(30, cfg.timeout))
            out = ((proc.stdout or b"") + (proc.stderr or b"")).decode(
                "utf-8", "replace")
            return proc.returncode == 0, out
        except subprocess.TimeoutExpired:
            return False, f"verify command timed out after {cfg.timeout}s"
        except OSError as e:
            return False, f"verify command failed to start: {e}"

    with Spinner("Self-verify"):
        ok, out = run_verify()
    if ok:
        ui.info(f"自检通过 ✔（{cmd}）")
        return
    for attempt in (1, 2):
        ui.warn(f"自检未通过（第 {attempt}/2 轮），自动修复中…")
        _run_turn(agent, "The self-verify gate failed. Fix the code so this command "
                         "passes, then confirm:\n"
                         f"command: {cmd}\noutput (tail):\n{out[-4000:]}")
        with Spinner("Self-verify"):
            ok, out = run_verify()
        if ok:
            ui.info(f"自检通过 ✔（{cmd}，第 {attempt} 轮修复后）")
            return
    ui.error(f"自检 {cmd} 两轮修复后仍未通过，请人工检查：\n{out[-1500:]}")
    try:
        from .tools.memory import append_brain
        append_brain(agent.config.cwd, "failed",
                     f"自检命令 {cmd} 两轮自动修复后仍未通过")
    except Exception:
        pass


def _postmortem(agent: Agent, status: str) -> None:
    """会话复盘：失败回合自动把教训写进项目大脑（机械提取，零额外 API 成本）。"""
    try:
        if not agent.checkpoints and status == "ok":
            return
        msgs = agent.session.messages
        start = len(msgs) - 1
        for i in range(len(msgs) - 1, -1, -1):
            if msgs[i].get("role") == "user":
                start = i
                break
        fails = []
        for m in msgs[start:]:
            if m.get("role") == "tool" and m.get("is_error"):
                content = m.get("content")
                body = content if isinstance(content, str) else "(多部分结果)"
                fails.append(f"工具 {m.get('name')} 失败：{' '.join(body.split())[:110]}")
        if status == "error":
            fails.append("模型调用异常（网络/限速/兼容）")
        if status == "blocked":
            fails.append("提示词被 user_prompt_submit hook 拦截")
        if not (status != "ok" or len(fails) >= 2):
            return
        summary = "；".join(fails[:3])[:280]
        from .tools.memory import append_brain
        added, msg = append_brain(agent.config.cwd, "failed", f"复盘：{summary}")
        if added:
            agent.ui.info("已把失败教训写入项目大脑（下次会话自动规避）")
    except Exception:
        pass


def _copy_clipboard(text: str) -> bool:
    import subprocess as _sp
    if os.name == "nt":
        _sp.run("clip", input=text.encode("utf-16"), check=True)
        return True
    for cmd in (["pbcopy"], ["xclip", "-selection", "clipboard"],
                ["wl-copy"]):
        if shutil.which(cmd[0]):
            _sp.run(cmd, input=text.encode("utf-8"), check=True)
            return True
    return False


def _parse_size(text: str):
    """'1M'/'500k'/'200000' -> int tokens; None if unparseable."""
    t = str(text).strip().lower().replace(" ", "")
    import re as _re
    m = _re.match(r"^(\d+(?:\.\d+)?)([km])?$", t)
    if not m:
        return None
    n = float(m.group(1))
    suffix = m.group(2)
    if suffix == "m":
        n *= 1_000_000
    elif suffix == "k":
        n *= 1000
    return int(n)


def _stats(ui: UI) -> None:
    import json as _json
    files = list(SESSIONS_DIR.glob("*.json"))
    total_msgs = total_tools = errors = 0
    tool_counts = {}
    for f in files:
        try:
            d = _json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for m in d.get("messages", []):
            total_msgs += 1
            if m.get("role") == "tool":
                total_tools += 1
                n = m.get("name") or "?"
                tool_counts[n] = tool_counts.get(n, 0) + 1
                if m.get("is_error"):
                    errors += 1
    ui.plain(f"  会话 {len(files)} · 消息 {total_msgs} · 工具调用 {total_tools} · "
             f"工具错误 {errors}（错误率 "
             f"{(errors / total_tools * 100 if total_tools else 0):.1f}%）")
    top = sorted(tool_counts.items(), key=lambda kv: -kv[1])[:5]
    if top:
        ui.plain("  最常用工具：" + "、".join(f"{n}×{c}" for n, c in top))


def _git_commit(agent: Agent, ui: UI, arg: str) -> None:
    files = []
    if agent.checkpoints:
        files = sorted({e["path"] for e in agent.checkpoints.entries})
    hint = f"（重点关注本会话改动：{', '.join(files[:10])}）" if files else ""
    extra = f"\n用户补充要求：{arg}" if arg else ""
    _run_turn(agent, COMMIT_PROMPT.format(files_hint=hint, extra=extra))


def _git_pr(agent: Agent, ui: UI, arg: str) -> None:
    _run_turn(agent, PR_PROMPT.format(base=arg or "main"))


def _plan_gate(agent: Agent, ui: UI) -> None:
    """After a plan-mode turn: offer to start implementation (like Claude Code)."""
    last = next((m for m in reversed(agent.session.messages)
                 if m.get("role") == "assistant" and m.get("content")), None)
    if last is None:
        return
    try:
        ans = input(green("  [Enter] 开始实施计划  [n] 继续讨论 ❯ ")).strip().lower()
    except (EOFError, KeyboardInterrupt):
        ui.plain()
        return
    if ans == "":
        try:
            from .plans import save_plan
            p = save_plan(Path.cwd(), last.get("content") or "")
            ui.info(f"计划已存档：{p}（/plans 管理，自动注入后续会话）")
        except OSError:
            pass
        agent.config.mode = "accept-edits"
        ui.info(f"权限模式：{MODE_LABELS[agent.config.mode]}，开始实施…")
        _run_turn(agent, "Plan approved. Start implementing it now, following the "
                         "plan exactly.")
    else:
        ui.info("保持计划模式，继续讨论即可。")


def _print_probe(provider, cfg, ui: UI) -> None:
    from .llm import probe_provider
    ui.plain(f"  探测 {cfg.model} @ {cfg.base_url} …")
    rows = probe_provider(provider)
    all_ok = True
    for name, ok, detail, secs in rows:
        all_ok &= ok
        mark = green("✓") if ok else red("✗")
        ui.plain(f"  {mark} {name:<18} {secs:5.1f}s  {detail}")
    if all_ok:
        ui.info("全部通过 —— 该端点可以驱动 minicode 的完整能力。")
    else:
        ui.warn("存在失败项：工具调用失败时 minicode 会自动降级为纯对话模式；"
                "流式失败会自动改用非流式。")


def _print_models(provider, cfg, ui: UI) -> None:
    from .llm import list_models, LLMError
    try:
        ids = list_models(provider)
    except LLMError as e:
        ui.error(str(e))
        return
    cur = cfg.model
    shown = ids[:40]
    for i in shown:
        mark = green("●") if i == cur else gray("○")
        ui.plain(f"  {mark} {i}" + ("  ← 当前" if i == cur else ""))
    if len(ids) > len(shown):
        ui.plain(gray(f"  … 共 {len(ids)} 个"))


def _doctor(agent: Agent, ui: UI) -> None:
    cfg = agent.config
    rows = []  # (label, value, ok)
    rows.append(("python", f"{sys.version_info.major}.{sys.version_info.minor}."
                           f"{sys.version_info.micro}", True))
    shell = cfg.shell_name
    found = bool(shutil.which(shell) if shell != "cmd" else shutil.which("cmd"))
    rows.append(("shell", shell, found))
    rows.append(("api_key", "已配置" if cfg.api_key else "未配置", bool(cfg.api_key)))
    rows.append(("base_url", cfg.base_url or "（默认）", True))
    rows.append(("model", cfg.model, True))
    rows.append(("provider", cfg.provider, True))
    try:
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        rows.append(("sessions 目录", f"{SESSIONS_DIR} 可写", True))
    except OSError as e:
        rows.append(("sessions 目录", f"不可写：{e}", False))
    ctx_file = next((n for n in ("MINICODE.md", "AGENTS.md", "CLAUDE.md")
                     if (Path.cwd() / n).exists()), None)
    rows.append(("项目记忆", ctx_file or "未找到（/init 可生成）", bool(ctx_file)))
    from .lineinput import HAS_READLINE
    rows.append(("tab 补全", "可用" if HAS_READLINE
                 else "不可用（Windows 可 pip install pyreadline3）", HAS_READLINE))
    if agent.mcp is not None:
        for name, c in agent.mcp.clients.items():
            ok = c.status == "connected"
            rows.append((f"mcp:{name}",
                         c.status + (f" ({len(c.tools)} tools)" if ok else "")
                         + (f" — {c.error}" if c.error else ""), ok))
    for k, v, ok in rows:
        mark = green("✓") if ok else red("✗")
        ui.plain(f"  {mark} {k:<14} {v}")


def _rewind(agent: Agent, ui: UI) -> None:
    if agent.checkpoints is None:
        ui.warn("此会话没有检查点管理器。")
        return
    shown = agent.checkpoints.list(10)
    if not shown:
        ui.warn("没有检查点（还没有文件修改）。")
        return
    for i, e in enumerate(shown, 1):
        ui.plain(f"  [{i}] {e['time']} {e['tool']:<14} {e['path']}")
    try:
        pick = input("回退到第几步（回车取消）: ").strip()
    except (EOFError, KeyboardInterrupt):
        ui.plain()
        return
    if not pick.isdigit() or not (1 <= int(pick) <= len(shown)):
        ui.warn("已取消。")
        return
    undone = agent.checkpoints.rewind_to(int(pick) - 1, shown)
    ui.info(f"已回退 {len(undone)} 步文件修改。")


def _resume(agent: Agent, ui: UI) -> bool:
    files = Session.list_sessions()
    if not files:
        ui.warn("没有可恢复的历史会话。")
        return False
    for i, f in enumerate(files, 1):
        ui.plain(f"  [{i}] {f.stem}")
    try:
        pick = input("选择序号（回车取消）: ").strip()
    except (EOFError, KeyboardInterrupt):
        ui.plain()
        return False
    if not pick.isdigit() or not (1 <= int(pick) <= len(files)):
        ui.warn("已取消。")
        return False
    try:
        s = Session.load(files[int(pick) - 1])
    except (OSError, ValueError) as e:
        ui.error(f"加载失败：{e}")
        return False
    agent.replace_session(s)
    ui.info(f"已加载会话（{len(s.messages)} 条消息）。")
    return True


def _maybe_resume_tasks(agent: Agent, ui: UI) -> None:
    """断点续跑：恢复的会话里还有未完成的 todo 时，主动接续。"""
    pending = [t for t in agent.session.todos if t.get("status") != "completed"]
    if not pending:
        return
    ui.info(f"上个会话还有 {len(pending)} 个未完成任务。")
    try:
        ans = input(green("  [Enter] 继续执行  [n] 稍后 ❯ ")).strip().lower()
    except (EOFError, KeyboardInterrupt):
        ui.plain()
        return
    if ans == "":
        todo_text = "\n".join(f"- [{t.get('status')}] {t.get('content')}"
                              for t in agent.session.todos)
        _run_turn(agent, "Continue the unfinished work from the previous session. "
                         "Here is the todo list — pick up where it left off:\n"
                         + todo_text)


HELP_SECTIONS = [
    ("会话", [("/clear", "开始新会话"), ("/compact [提示]", "压缩上下文为摘要"),
             ("/resume", "恢复历史会话"), ("/export", "导出会话为 Markdown"),
             ("/transcript", "浏览最近对话")]),
    ("执行与安全", [("/mode [模式]", "default / accept-edits / plan / full-access"),
                  ("/yolo", "一键切到完全访问"), ("/undo", "撤销上一次文件修改"),
                  ("/rewind", "回退到任一检查点"),
                  ("/diff", "本会话改动总览"), ("/verify [命令]", "自检门禁：失败自动修复")]),
    ("上下文与记忆", [("/brain", "项目大脑（跨会话记忆）"), ("/memory", "项目记忆 MINICODE.md"),
                    ("/context", "上下文占用明细"), ("/cost", "累计 token 用量"),
                    ("/limit [1M]", "上下文长度自定义"), ("/stats", "历史会话统计")]),
    ("模型", [("/model [名称]", "查看/切换模型"), ("/models", "列出端点可用模型"),
             ("/probe", "端点兼容性实测"), ("/reasoning", "思考力度 low/medium/high")]),
    ("项目与扩展", [("/init", "生成 MINICODE.md"), ("/commit", "规范提交本会话改动"),
                  ("/pr [base]", "生成/创建 PR"), ("/plans", "存档计划管理"),
                  ("/agents", "子智能体列表"), ("/skills", "工作流技能列表"),
                  ("/mcp", "MCP 服务器状态"),
                  ("/add-dir <目录>", "授权额外目录")]),
    ("系统", [("/tools", "工具列表"), ("/status", "当前状态"), ("/output-style", "输出风格"),
             ("/doctor", "环境自检"), ("/help", "本帮助"), ("/exit", "退出（Ctrl+D）")]),
]


def _disp_width(s: str) -> int:
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1
               for ch in s)


def _pad(s: str, width: int) -> str:
    return s + " " * max(0, width - _disp_width(s))


def _render_help() -> str:
    lines = []
    for title, cmds in HELP_SECTIONS:
        lines.append(cyan(f"  {title}"))
        for cmd, desc in cmds:
            lines.append(f"    {_pad(cmd, 20)} {gray(desc)}")
        lines.append("")
    lines.append(gray("  快捷方式：!cmd 直接本地执行（不经模型）· @文件 提及附件 · "
                      "/yolo /plan /edits 切模式 · /comp → /compact 前缀匹配 · "
                      "提示词含 think / ultrathink 加大思考预算 · 行尾 \\ 续行 · "
                      ".minicode/commands/*.md 自定义命令"))
    return "\n".join(lines)


def _rebuild_prompt(agent: Agent) -> None:
    from .tools.skills import skills_section_text
    agent.system_prompt = (build_system_prompt(agent.config, agent._prompt_cwd,
                                               agent._custom_agents)
                           + skills_section_text(agent._prompt_cwd))


def _shell_passthrough(agent: Agent, ui: UI, command: str) -> None:
    """`!cmd` — run a shell command directly, no model involved."""
    if not command:
        ui.info("用法：!git status —— ! 开头的命令直接本地执行，不经过模型。")
        return
    bash = agent.registry.get("bash")
    if bash is None:
        ui.error("bash 工具不可用。")
        return
    try:
        out = bash.run({"command": command}, agent.ctx)
    except ToolError as e:
        ui.error(str(e))
        return
    for ln in out.splitlines()[:60]:
        ui.plain("  " + ln)
    if len(out.splitlines()) > 60:
        ui.plain(gray(f"  … (+{len(out.splitlines()) - 60} lines)"))


MENTION_RE = None  # compiled lazily


def _expand_mentions(text: str, agent: Agent) -> object:
    """@path mentions: attach file contents (text) or image blocks (visual)
    to the user message. Non-existent paths stay untouched."""
    import re as _re
    global MENTION_RE
    if MENTION_RE is None:
        MENTION_RE = _re.compile(r"@([\w\-./\\ \u4e00-\u9fff]+?)(?=\s|$)")
    from .tools.fs import IMAGE_TYPES, _read_text, _resolve
    import base64 as _b64

    cwd = agent.config.cwd or Path.cwd()
    texts, images = [], []
    seen = set()
    for m in MENTION_RE.finditer(text):
        token = m.group(1).strip().strip('"').strip("'")
        if not token or token in seen:
            continue
        p = (cwd / token).resolve() if not Path(token).is_absolute() else Path(token)
        if not p.is_file() or str(p) in seen:
            continue
        seen.add(token)
        rel = token
        if p.suffix.lower() in IMAGE_TYPES:
            data = p.read_bytes()
            if len(data) <= 8 * 1024 * 1024:
                images.append({"type": "image", "media_type": IMAGE_TYPES[p.suffix.lower()],
                               "data": _b64.b64encode(data).decode("ascii"),
                               "label": rel})
        elif p.stat().st_size <= 200_000:
            try:
                body = _read_text(p)[:4000]
                texts.append(f"[附文件 {rel}]\n{body}")
            except (OSError, ToolError):
                continue
    if not texts and not images:
        return text
    attached = ("\n\n".join(texts)) if texts else ""
    new_text = text + (("\n\n" + attached) if attached else "")
    if images:
        blocks = [{"type": "text", "text": new_text}]
        blocks += [{"type": "image", "media_type": im["media_type"],
                    "data": im["data"]} for im in images]
        blocks.append({"type": "text",
                       "text": "附图：" + "、".join(im["label"] for im in images)
                               + "（如上，供视觉分析）"})
        return blocks
    return new_text


COMMAND_ALIASES = {"/yolo": "/mode full-access", "/plan": "/mode plan",
                   "/edits": "/mode accept-edits"}


def _resolve_command(line: str, agent: Agent, ui: UI):
    """Resolve aliases and unique command prefixes. Returns (name, arg, ok)."""
    name, _, arg = line.partition(" ")
    name = name.lower().strip()
    arg = arg.strip()
    if name in COMMAND_ALIASES:
        alias_cmd, _, extra = COMMAND_ALIASES[name].partition(" ")
        arg = (extra + " " + arg).strip()
        return alias_cmd, arg, True
    known = ["/help", "/clear", "/compact", "/model", "/models", "/probe", "/mode",
             "/undo", "/rewind", "/diff", "/reasoning", "/cost", "/context",
             "/memory", "/brain", "/verify", "/todos", "/agents", "/mcp",
             "/add-dir", "/transcript", "/output-style", "/doctor", "/tools",
             "/status", "/resume", "/export", "/init", "/plans", "/stats",
             "/commit", "/pr", "/exit", "/quit", "/q"]
    custom = ["/" + n for n in _custom_commands()]
    if name not in known + custom and not name.startswith("/"):
        return name, arg, False
    if name not in known + custom:
        candidates = [c for c in known + custom if c.startswith(name)]
        if len(candidates) == 1:
            return candidates[0], arg, True
        if len(candidates) > 1:
            ui.error(f"{name} 有多个匹配：{'、'.join(candidates[:6])}")
            return name, arg, False
    return name, arg, True


def _command(line: str, agent: Agent, ui: UI, prompt_cwd: Path) -> bool:
    """Handle a /command. Returns False to exit the REPL."""
    name, arg, _ok = _resolve_command(line, agent, ui)
    if name in ("/exit", "/quit", "/q"):
        return False
    if name == "/help":
        ui.plain(_render_help())
        custom = _custom_commands()
        shown = {k: v for k, v in custom.items() if k not in BUILTIN_COMMANDS}
        if shown:
            ui.plain(cyan("  自定义命令（.minicode/commands/）"))
            for cname, (desc, _) in shown.items():
                ui.plain(f"    /{cname:<16} {gray(desc)}")
    elif name == "/clear":
        agent.reset_session()
        ui.info("已开始新会话。")
    elif name == "/compact":
        try:
            agent.compact(arg or None)
        except (LLMError, RuntimeError) as e:
            ui.error(f"压缩失败：{e}")
    elif name == "/model":
        if arg:
            resolved = arg
            ids = None
            if hasattr(agent.provider, "available_models"):
                try:
                    ids = agent.provider.available_models()
                except LLMError:
                    ids = None  # endpoint unreachable — switch anyway, fail loudly later
            if ids and arg.isdigit():
                n = int(arg)
                if 1 <= n <= len(ids):
                    resolved = ids[n - 1]
                    ui.info(f"选择第 {n} 个模型：{resolved}")
                else:
                    ui.error(f"序号超出范围（1-{len(ids)}），/models 查看列表")
                    return True
            if ids and resolved == arg:
                exact = [i for i in ids if i == arg]
                if not exact:
                    partial = [i for i in ids if arg.lower() in i.lower()]
                    if len(partial) == 1:
                        resolved = partial[0]
                        ui.info(f"匹配到模型：{resolved}")
                    elif len(partial) > 1:
                        ui.error(f"{arg!r} 匹配到多个模型，请用完整名称或序号：")
                        for i in partial[:10]:
                            ui.plain(f"  · {i}")
                        return True
                    else:
                        ui.error(f"端点上没有模型 {arg!r}。可用模型：")
                        for i in ids[:10]:
                            ui.plain(f"  · {i}")
                        ui.plain(gray("  （/model <名称或序号> 切换）"))
                        return True
            agent.config.model = resolved
            agent.provider.model = resolved
            ui.info(f"模型已切换为 {resolved}")
        else:
            ui.info(f"当前模型：{agent.config.model}（provider: {agent.config.provider}）"
                    "，/models 查看可用模型")
    elif name == "/models":
        _print_models(agent.provider, agent.config, ui)
    elif name == "/probe":
        _print_probe(agent.provider, agent.config, ui)
    elif name == "/mode":
        if arg == "yolo":
            arg = "full-access"
        if arg in MODES:
            agent.config.mode = arg
        else:
            agent.config.mode = MODES[(MODES.index(agent.config.mode) + 1) % len(MODES)]
        ui.info(f"权限模式：{MODE_LABELS.get(agent.config.mode, agent.config.mode)}")
    elif name == "/output-style":
        style = arg if arg in ("default", "explanatory") else (
            "explanatory" if agent.config.output_style == "default" else "default")
        agent.config.output_style = style
        _rebuild_prompt(agent)
        ui.info(f"输出风格：{style}")
    elif name == "/undo":
        e = agent.checkpoints.undo() if agent.checkpoints else None
        if e:
            ui.info(f"已撤销 {e['tool']} 对 {e['path']} 的修改。")
        else:
            ui.warn("没有可撤销的更改。")
    elif name == "/rewind":
        _rewind(agent, ui)
    elif name == "/diff":
        if agent.checkpoints is None:
            ui.warn("此会话没有检查点管理器。")
            return True
        diffs = agent.checkpoints.session_diff()
        if not diffs:
            ui.info("本次会话没有未还原的文件改动。")
        for path, diff in diffs:
            ui.plain(f"  {path}:")
            lines = diff.splitlines()
            for ln in lines[:80]:
                color = green if ln.startswith("+") else (red if ln.startswith("-") else gray)
                ui.plain("    " + color(ln))
            if len(lines) > 80:
                ui.plain(f"    … (+{len(lines) - 80} lines)")
    elif name == "/reasoning":
        order = ["", "low", "medium", "high"]
        if arg in order:
            agent.config.reasoning_effort = arg
        else:
            cur = order.index(agent.config.reasoning_effort or "")
            agent.config.reasoning_effort = order[(cur + 1) % len(order)]
        effort = agent.config.reasoning_effort or "默认"
        agent.provider.reasoning_effort = agent.config.reasoning_effort \
            if hasattr(agent.provider, "reasoning_effort") else None
        ui.info(f"reasoning effort：{effort}"
                + ("（OpenAI 兼容端即时生效；Anthropic 转为思考预算）"
                   if agent.config.reasoning_effort else ""))
    elif name == "/cost":
        t = agent.session.total_usage
        ui.plain(f"  累计 tokens：in {t['input']:,} · out {t['output']:,}（费用按服务商定价）")
    elif name == "/context":
        s = agent.session
        ui.plain(f"  系统提示 ~{len(agent.system_prompt) // 3:,} tok")
        by_role = {}
        for m in s.messages:
            key = m.get("role") or "?"
            by_role[key] = by_role.get(key, 0) + s._content_chars(m.get("content"))
            for tc in m.get("tool_calls") or []:
                by_role[key] += len(tc.get("args") or "")
        for role, chars in by_role.items():
            ui.plain(f"  {role:<10} ~{chars // 3:,} tok")
        ui.plain(f"  合计       ~{s.context_tokens():,} tok (上限 {agent.config.context_limit:,})")
    elif name == "/memory":
        p = Path("MINICODE.md")
        if p.exists():
            ui.info(f"项目记忆：{p.resolve()}（已自动加载，支持 @文件 导入）")
            try:
                os.startfile(str(p))  # Windows
            except AttributeError:
                editor = os.environ.get("EDITOR")
                if editor:
                    subprocess.run([editor, str(p)])
        else:
            ui.warn("MINICODE.md 不存在，可用 /init 生成。")
    elif name == "/brain":
        from .tools.memory import brain_path, load_brain_text
        if arg == "clear":
            p = brain_path(Path.cwd())
            if p.exists():
                p.unlink()
                _rebuild_prompt(agent)
                ui.info("项目大脑已清空。")
            else:
                ui.warn("还没有大脑文件。")
        else:
            text = load_brain_text(Path.cwd()).strip()
            if text:
                ui.plain(text)
            else:
                ui.info("大脑还是空的。智能体学到项目知识后会自动写入 "
                        "(.minicode/BRAIN.md)，也可让它现在总结。")
    elif name == "/verify":
        if not arg:
            cur = agent.config.verify_command
            ui.info(f"自检命令：{cur or '未设置'}（例：/verify pytest -q；/verify off 关闭）"
                    if cur else "自检未开启。用法：/verify pytest -q —— 之后每次改完文件自动跑，"
                                "失败自动修复（最多两轮）。")
        elif arg.lower() == "off":
            agent.config.verify_command = ""
            ui.info("自检已关闭。")
        else:
            agent.config.verify_command = arg
            ui.info(f"自检命令已设置：{arg} —— 每次文件改动后自动执行，失败自动修复。")
    elif name == "/todos":
        if agent.session.todos:
            ui.todo_render(agent.session.todos)
        else:
            ui.warn("当前没有任务清单。")
    elif name == "/agents":
        ui.plain("  内置：dispatch_agent（只读调研子智能体，subagent_type 可选自定义）")
        if agent._custom_agents:
            for n, info in agent._custom_agents.items():
                extra = f" · tools: {info['tools']}" if info.get("tools") else ""
                extra += f" · model: {info['model']}" if info.get("model") else ""
                ui.plain(f"  {n:<16} {info.get('description', '')}{extra}")
        else:
            ui.plain("  自定义：无（.minicode/agents/*.md 可定义，frontmatter 支持 "
                     "description/tools/model）")
    elif name == "/mcp":
        if agent.mcp is not None:
            for ln in agent.mcp.status_lines():
                ui.plain(ln)
        else:
            ui.warn("未配置 MCP。")
    elif name == "/add-dir":
        if not arg:
            ui.error("用法：/add-dir <目录>")
        else:
            p = Path(arg).expanduser().resolve()
            if p.is_dir():
                agent.config.extra_dirs.append(str(p))
                _rebuild_prompt(agent)
                ui.info(f"已授权访问目录：{p}")
            else:
                ui.error(f"目录不存在：{p}")
    elif name == "/transcript":
        s = agent.session
        shown = 0
        for m in s.messages[-16:]:
            role = m.get("role")
            content = m.get("content")
            text = content if isinstance(content, str) else "(多部分内容)"
            who = {"user": "你 ", "assistant": "AI", "tool": "工具"}.get(role, "·")
            ui.plain(f"  {gray(who)} {' '.join((text or '').split())[:160]}")
            shown += 1
        if not shown:
            ui.warn("会话为空。")
    elif name == "/doctor":
        _doctor(agent, ui)
    elif name == "/tools":
        for t in agent.registry.tools.values():
            ui.plain(f"  {t.name:<24} [{t.kind}] {t.description.splitlines()[0][:64]}")
    elif name == "/status":
        s = agent.session
        ui.plain(f"  model     {agent.config.model}")
        ui.plain(f"  provider  {agent.config.provider}   mode  {agent.config.mode}"
                 f"   style  {agent.config.output_style}")
        ui.plain(f"  messages  {len(s.messages)}   checkpoints  "
                 f"{len(agent.checkpoints.entries) if agent.checkpoints else 0}")
        ui.plain(f"  context   ~{s.context_tokens():,} tokens (limit {agent.config.context_limit:,})")
        ui.plain(f"  sessions  {SESSIONS_DIR}")
    elif name == "/resume":
        _resume(agent, ui)
    elif name == "/export":
        out = Path(f"minicode-chat-{time.strftime('%Y%m%d-%H%M%S')}.md")
        try:
            agent.session.export_markdown(out)
            ui.info(f"已导出到 {out.resolve()}")
        except OSError as e:
            ui.error(f"导出失败：{e}")
    elif name == "/init":
        _run_turn(agent, INIT_PROMPT)
    elif name == "/commit":
        _git_commit(agent, ui, arg)
    elif name == "/pr":
        _git_pr(agent, ui, arg)
    elif name == "/plans":
        from . import plans as _plans
        if arg.startswith("done"):
            pick = arg.split()[-1]
            p = _plans.mark_plan(Path.cwd(), int(pick), "done") \
                if pick.isdigit() else None
            if p:
                agent.system_prompt = build_system_prompt(
                    agent.config, agent._prompt_cwd, agent._custom_agents)
                ui.info(f"计划 #{pick} 已标记完成。")
            else:
                ui.error(f"没有 #{pick} 号计划。")
        else:
            rows = _plans.list_plans(Path.cwd())
            if not rows:
                ui.info("还没有存档计划（plan 模式批准后自动存档）。")
            for i, p, s, t in rows:
                mark = green("●") if s == "in-progress" else gray("○")
                ui.plain(f"  {mark} [{i}] ({s}) {t}")
            active = _plans.latest_active_plan(Path.cwd())
            if active:
                ui.info("最新 in-progress 计划已注入系统提示。")
    elif name == "/copy":
        last = next((m for m in reversed(agent.session.messages)
                     if m.get("role") == "assistant" and m.get("content")), None)
        if not last:
            ui.warn("还没有可复制的回复。")
            return True
        text = last["content"] if isinstance(last["content"], str) else \
            "\n".join(b.get("text", "") for b in last["content"]
                      if isinstance(b, dict) and b.get("type") == "text")
        try:
            if _copy_clipboard(text):
                ui.info("已复制上一条回复到剪贴板。")
            else:
                ui.error("未找到剪贴板工具（Windows 用 clip，macOS 用 pbcopy）。")
        except Exception as e:
            ui.error(f"复制失败：{e}")
    elif name == "/limit":
        if not arg:
            ui.info(f"上下文长度：{_fmt_tokens(agent.config.context_limit)}"
                    f"（{agent.config.context_limit:,} tok）—— /limit 1M 或 /limit 200k 自定义")
        else:
            n = _parse_size(arg)
            if not n or n < 1000:
                ui.error(f"无法解析 {arg!r}——示例：/limit 1M、/limit 500k、/limit 200000")
                return True
            agent.config.context_limit = n
            ui.info(f"上下文长度已设置为 {_fmt_tokens(n)}（{n:,} tok）")
    elif name == "/skills":
        from .tools.skills import skills_catalog, load_skill
        catalog = skills_catalog(Path.cwd())
        if not catalog:
            ui.info("没有可用技能。可在 .minicode/skills/<name>/SKILL.md 放置工作流技能。")
        else:
            for sname, (desc, src_label) in catalog.items():
                ui.plain(f"  {sname:<28} {gray('[' + src_label + ']')} {desc}")
            ui.info("模型会在任务匹配时自动通过 skill 工具加载；也可让它‘用 xx 技能’。")
        if arg:
            body = load_skill(Path.cwd(), arg)
            if body:
                ui.plain(dim(body[:600]))
    elif name == "/stats":
        _stats(ui)
    else:
        cmd = _custom_commands().get(name.lstrip("/"))
        if cmd:
            _, body = cmd
            _run_turn(agent, body.replace("$ARGUMENTS", arg).replace("{args}", arg))
        else:
            ui.error(f"未知命令 {name}（/help 查看帮助）")
    return True


INIT_PROMPT = """分析当前项目并创建 MINICODE.md 上下文文件，内容包括：
1. 一句话项目概述；2. 目录结构要点；3. 构建/测试/运行命令；4. 代码风格约定（若有）。
先用工具浏览仓库，再写入文件，保持简洁（不超过 60 行）。"""

COMMIT_PROMPT = """请为当前改动创建一个 git commit：
1. 用 bash 运行 git status 和 git diff 查看改动{files_hint}
2. 写规范的 commit message（Conventional Commits：feat/fix/refactor/docs/chore，第一行 ≤50 字符，正文列要点）
3. 用 bash 执行 git add -A && git commit，并把最终 message 展示给用户
4. 没有 git 仓库或没有任何改动时，直接说明，不要创建空提交
{extra}"""

PR_PROMPT = """请为当前分支生成 PR：
1. 用 bash 运行 git log --oneline -20 与 git diff {base}...HEAD 了解改动
2. 产出：PR 标题 + Markdown 描述（背景 / 改动点 / 验证方式），保存到 .minicode/PR_DRAFT.md 并展示
3. 若 gh 命令可用，用 gh pr create --title ... --body-file .minicode/PR_DRAFT.md --base {base} 创建；不可用则只输出文案并提示安装 gh
"""


def _read_multiline(first_line: str, ui: UI) -> str:
    """Trailing backslash continues on the next line (like Claude Code)."""
    line = first_line
    while line.endswith("\\"):
        try:
            line = line[:-1] + "\n" + input(cyan_cont())
        except (EOFError, KeyboardInterrupt):
            ui.plain()
            break
    return line


def cyan_cont():
    from .ui import cyan
    return cyan("… ")


def main(argv=None) -> int:
    global args_state
    args = _parse_args(argv)
    args_state = vars(args)
    if args.cwd:
        try:
            os.chdir(args.cwd)
        except OSError as e:
            print(f"无法切换工作目录: {e}")
            return 2
    cwd = Path.cwd()
    ui = UI()
    cfg = load_config(args)
    if cfg is None:
        return 2
    cfg.cwd = cwd
    if args.budget:
        cfg.turn_budget = args.budget

    # CLI tool rules merge into permissions
    if args.allowed_tools or args.disallowed_tools:
        perms = dict(cfg.permissions or {})
        if args.allowed_tools:
            perms["allow"] = list(perms.get("allow") or [])
            perms["allow"] += [t.strip() for t in args.allowed_tools.split(",") if t.strip()]
        if args.disallowed_tools:
            perms["deny"] = list(perms.get("deny") or [])
            perms["deny"] += [t.strip() for t in args.disallowed_tools.split(",") if t.strip()]
        cfg.permissions = perms

    provider = make_provider(cfg)
    mcp_manager = McpManager(cfg.mcp_servers, cwd)
    if mcp_manager.clients:
        atexit.register(mcp_manager.stop_all)

    if args.probe:
        _print_probe(provider, cfg, ui)
        return 0

    if args.serve:
        from .server import MinicodeServer
        agent = _build_agent(cfg, provider, Session(), SilentUI(), mcp_manager)
        srv = MinicodeServer(agent, host="127.0.0.1", port=args.port)
        ui.plain(f"  ⚡ minicode serve → http://127.0.0.1:{srv.port}")
        ui.plain(gray(f"  token: {srv.token}"))
        ui.plain(gray("  POST /api/turn {\"prompt\": \"...\"}，Header: X-Minicode-Token"))
        ui.plain(gray("  Ctrl+C 停止"))
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            mcp_manager.stop_all()
        return 0

    if args.print:
        prompt = " ".join(args.prompt).strip()
        if not prompt and not sys.stdin.isatty():
            prompt = sys.stdin.read().strip()
        if not prompt:
            ui.error("-p 需要提示词参数，或通过管道传入内容。")
            return 2
        if args.output_format in ("json", "stream-json"):
            ui = SilentUI()
        agent = _build_agent(cfg, provider, Session(), ui, mcp_manager)
        if args.output_format == "stream-json":
            agent.session.on_append = lambda m: print(
                json.dumps(m, ensure_ascii=False, default=str), flush=True)
        try:
            agent.run_turn(prompt)
        except (LLMError, RuntimeError) as e:
            ui.error(str(e))
            return 1
        finally:
            mcp_manager.stop_all()
        if args.output_format == "json":
            print(json.dumps({"result": agent.session.messages[-1].get("content")
                              if agent.session.messages else "",
                              "messages": agent.session.messages,
                              "usage": agent.session.total_usage},
                             ensure_ascii=False, default=str))
        return 0

    if args.continue_last:
        session = Session.load_last() or Session()
    elif args.resume:
        session = Session()  # picker runs after banner
    else:
        session = Session()
    agent = _build_agent(cfg, provider, session, ui, mcp_manager)
    ui.banner(__version__, cfg, cwd)
    resumed = False
    if args.continue_last and session.messages:
        ui.info(f"已恢复上次会话（{len(session.messages)} 条消息）。")
        resumed = True
    if args.resume:
        resumed = _resume(agent, ui)
    if resumed:
        _maybe_resume_tasks(agent, ui)
    if args.prompt:
        _run_turn(agent, " ".join(args.prompt))
        if cfg.mode == "plan":
            _plan_gate(agent, ui)

    from .lineinput import read_line
    command_names = ["/help", "/clear", "/compact", "/model", "/models", "/probe",
                     "/mode", "/copy", "/undo",
                     "/rewind", "/diff", "/reasoning", "/cost", "/context", "/memory",
                     "/brain", "/verify", "/todos", "/agents", "/mcp", "/add-dir",
                     "/transcript", "/output-style", "/doctor", "/tools", "/status",
                     "/resume", "/export", "/init", "/plans", "/stats", "/commit",
                     "/pr", "/copy", "/limit", "/skills", "/exit"]
    command_names += ["/" + n for n in _custom_commands()]

    turn_no = 0
    last_interrupt = 0.0
    while True:
        try:
            ui.context_bar(agent.session.context_tokens(),
                           cfg.context_limit, cfg.model)
            line = read_line(cyan("❯ ") if ui_colored() else "❯ ", command_names)
        except EOFError:
            break
        except KeyboardInterrupt:
            now = time.time()
            if now - last_interrupt < 2.0:
                break
            last_interrupt = now
            ui.plain(gray("Ctrl+C 再按一次退出；继续输入即可"))
            continue
        line = _read_multiline(line.strip(), ui).strip()
        if not line:
            continue
        if line.startswith("!"):  # shell passthrough — no model involved
            _shell_passthrough(agent, ui, line[1:].strip())
            ui.rule()
            continue
        if line.startswith("/"):
            if not _command(line, agent, ui, cwd):
                break
            ui.rule()
            continue
        turn_no += 1
        _run_turn(agent, _expand_mentions(line, agent))
        if agent.config.mode == "plan":
            _plan_gate(agent, ui)
        ui.rule(f"回合 {turn_no} 完成")

    _save(agent)
    mcp_manager.stop_all()
    ui.plain(gray("再见。"))
    return 0


def ui_colored():
    import sys as _s
    from .ui import USE_COLOR
    return USE_COLOR


if __name__ == "__main__":
    raise SystemExit(main())
