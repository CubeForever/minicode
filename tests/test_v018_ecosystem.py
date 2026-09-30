"""v0.18：hooks 事件体系（多规则/matcher/JSON 决策协议/新事件）、MCP
resources/prompts、持久 shell 的 PowerShell/cmd 适配、扩展市场。"""
import json
import os
import shutil
import sys
import textwrap
from types import SimpleNamespace

import pytest

from minicode import config as config_mod
from minicode.agent import Agent
from minicode.config import Config
from minicode.fake import FakeProvider
from minicode.mcp import McpManager
from minicode.session import Session
from minicode.tools import build_registry
from minicode.tools.base import ToolRegistry
from minicode.tools.shell import ShellState
from minicode.ui import UI


def make_agent(tmp_path, provider, mode="default", **cfg_kwargs) -> Agent:
    cfg = Config(provider="fake", api_key="", model="fake", mode=mode, **cfg_kwargs)
    cfg.cwd = tmp_path
    return Agent(provider, Session(), UI(), cfg,
                 build_registry(ShellState(tmp_path, "bash")))


def py_hook_script(tmp_path, name, body: str) -> str:
    """写一个 hook 用 Python 脚本，返回可直接放进 shell=True 的命令。"""
    f = tmp_path / f"{name}.py"
    f.write_text(textwrap.dedent(body), encoding="utf-8")
    return f'"{sys.executable}" "{f}"'


# ---------- hooks：规则归一化与 matcher ----------

def test_hook_rules_normalization():
    from minicode.agent import Agent
    assert Agent._hook_rules(None) == []
    assert Agent._hook_rules("cmd")[0]["command"] == "cmd"
    rules = Agent._hook_rules([
        "plain-cmd",
        {"matcher": "Bash|edit_file", "command": "x", "timeout": 5},
        {"nope": 1},
    ])
    assert len(rules) == 2
    assert rules[1]["matcher"] == "Bash|edit_file" and rules[1]["timeout"] == 5


def test_hook_matcher_filters_by_tool_name(tmp_path):
    marker = tmp_path / "hook.log"
    cmd = py_hook_script(tmp_path, "mark",
                         'import sys\nopen(sys.argv[1], "a").write(sys.argv[2] + "\\n")')
    full_cmd = cmd + f' "{marker}" bash'
    agent = make_agent(tmp_path, FakeProvider([{"text": "ok"}]), hooks={
        "pre_tool_use": [{"matcher": "bash|edit_file", "command": full_cmd}],
    })
    # 直接驱动 _run_hook：matcher 命中 bash、不命中 read_file
    agent._run_hook("pre_tool_use", {}, tool_name="bash")
    lines1 = marker.read_text(encoding="utf-8").splitlines() if marker.exists() else []
    agent._run_hook("pre_tool_use", {}, tool_name="read_file")
    lines2 = marker.read_text(encoding="utf-8").splitlines() if marker.exists() else []
    assert len(lines1) == 1 and len(lines2) == 1   # read_file 被 matcher 过滤


def test_hook_json_block_protocol(tmp_path):
    cmd = py_hook_script(tmp_path, "block",
                         'import sys, json\n'
                         'sys.stdout.write(json.dumps({"decision": "block",'
                         ' "reason": "审查未通过"}))')
    agent = make_agent(tmp_path, FakeProvider([{"text": "ok"}]), hooks={
        "pre_tool_use": {"command": cmd}})
    blocked, out, approved = agent._run_hook("pre_tool_use", {}, tool_name="bash")
    assert blocked == "审查未通过" and approved is False


def test_hook_exit_code_still_blocks(tmp_path):
    cmd = py_hook_script(tmp_path, "fail", 'import sys\nsys.exit(3)')
    agent = make_agent(tmp_path, FakeProvider([{"text": "ok"}]), hooks={
        "pre_tool_use": cmd})
    blocked, _out, approved = agent._run_hook("pre_tool_use", {}, tool_name="bash")
    assert blocked and approved is False


def test_hook_timeout_is_block(tmp_path):
    cmd = py_hook_script(tmp_path, "slow", 'import time\ntime.sleep(8)')
    agent = make_agent(tmp_path, FakeProvider([{"text": "ok"}]), hooks={
        "pre_tool_use": {"command": cmd, "timeout": 1}})
    blocked, _out, _ok = agent._run_hook("pre_tool_use", {}, tool_name="bash")
    assert "hook timeout" in blocked


class NoConfirmUI(UI):
    def confirm(self, *a, **k):
        raise AssertionError("confirm 不应被调用——hook approve 应跳过标准确认")


def test_hook_approve_skips_confirm_but_not_deny(tmp_path):
    from minicode.tools import build_registry
    approve_cmd = py_hook_script(
        tmp_path, "approve",
        'import sys, json\nsys.stdout.write(json.dumps({"decision": "approve"}))')
    cfg = Config(provider="fake", api_key="", model="fake", mode="default",
                 hooks={"pre_tool_use": {"matcher": "write_file",
                                         "command": approve_cmd}})
    cfg.cwd = tmp_path
    # 场景一：approve 跳过标准确认，default 模式写文件不再询问
    agent = Agent(FakeProvider([
        {"tool_calls": [{"id": "t", "name": "write_file",
                         "args": json.dumps({"path": "x.txt", "content": "hi"})}]},
        {"text": "done"}]), Session(), NoConfirmUI(), cfg,
        ToolRegistry(build_registry(ShellState(tmp_path, "bash")).tools.values()))
    agent.run_turn("write")
    assert (tmp_path / "x.txt").read_text(encoding="utf-8") == "hi"
    # 场景二：approve 不能越过 deny 规则
    cfg2 = Config(provider="fake", api_key="", model="fake", mode="default",
                  permissions={"deny": ["write_file"]},
                  hooks={"pre_tool_use": {"matcher": "write_file",
                                          "command": approve_cmd}})
    cfg2.cwd = tmp_path
    agent2 = Agent(FakeProvider([
        {"tool_calls": [{"id": "t", "name": "write_file",
                         "args": json.dumps({"path": "y.txt", "content": "hi"})}]},
        {"text": "done"}]), Session(), NoConfirmUI(), cfg2,
        ToolRegistry(build_registry(ShellState(tmp_path, "bash")).tools.values()))
    agent2.run_turn("write")
    assert not (tmp_path / "y.txt").exists()


def test_stop_and_pre_compact_hooks_fire(tmp_path):
    marker = tmp_path / "events.log"
    hook = (py_hook_script(tmp_path, "mark",
                           'import sys\nopen(sys.argv[1], "a").write(sys.argv[2] + "\\n")')
            + f' "{marker}"')
    agent = make_agent(tmp_path, FakeProvider([{"text": "ok"}]),
                       hooks={"stop": hook + ' stop',
                              "pre_compact": hook + ' pre_compact'})
    from minicode.cli import _run_turn
    _run_turn(agent, "hi")
    agent.compact()
    events = marker.read_text(encoding="utf-8").splitlines()
    assert "stop" in events and "pre_compact" in events


# ---------- 持久 shell：PowerShell / cmd ----------

def test_parse_env_lines_skips_cmd_trivia():
    env = ShellState._parse_env_lines("=C:=C:\\some\r\nGOOD=1\r\n__MCC_PWD__x\r\n")
    assert env == {"GOOD": "1"}


def test_env_prefix_powershell_and_cmd_rendering():
    st = ShellState.__new__(ShellState)
    st.shell = "powershell"
    st.cwd = __import__("pathlib").Path(".")
    st.background = {}
    st._bg_counter = 0
    st._env_baseline = {"A": "1"}
    st._env_overrides = {"A": "it's", "B OK_1": "plain"}
    st._env_unsets = {"GONE"}
    prefix = st.env_export_prefix()
    assert "Remove-Item Env:GONE" in prefix
    assert "$env:A='it''s';" in prefix          # 单引号翻倍转义
    assert "$env:B OK_1='plain';" not in prefix  # 非法变量名跳过
    st.shell = "cmd"
    st._env_overrides = {"A": "ok", "BAD": 'has"quote'}
    prefix = st.env_export_prefix()
    assert "set A=ok&" in prefix                 # cmd 无引号 + & 紧贴值尾
    assert "BAD" not in prefix                   # 含引号的值对 cmd 不安全，跳过
    assert "it's" not in st.env_export_prefix()  # PS 分支才有该值


@pytest.mark.skipif(os.name != "nt", reason="cmd 仅 Windows")
def test_cmd_env_persists_across_calls(tmp_path):
    st = ShellState(tmp_path, "cmd")
    from minicode.tools.shell import BashTool
    tool = BashTool(st)
    from minicode.tools.base import ToolContext
    ctx = ToolContext(cwd=tmp_path, config=Config(), session=Session(), ui=UI())
    tool.run({"command": "set MINI_T=hello"}, ctx)
    bl = st._env_baseline or {}
    out = tool.run({"command": "echo !MINI_T!"}, ctx)
    assert "hello" in out, (
        f"baseline_vars={len(bl)} overrides={list(st._env_overrides)[:8]}"
        f" n_overrides={len(st._env_overrides)} out={out[:160]!r}"
        f" sample_baseline={sorted(bl)[:6]}")


@pytest.mark.skipif(not (shutil.which("powershell") or shutil.which("pwsh")),
                    reason="需要 PowerShell")
def test_powershell_env_persists_across_calls(tmp_path):
    st = ShellState(tmp_path, "powershell")
    from minicode.tools.shell import BashTool
    tool = BashTool(st)
    from minicode.tools.base import ToolContext
    ctx = ToolContext(cwd=tmp_path, config=Config(), session=Session(), ui=UI())
    tool.run({"command": "$env:MINI_T = 'persisted'"}, ctx)
    out = tool.run({"command": "Write-Output \"v=$env:MINI_T\""}, ctx)
    assert "v=persisted" in out


# ---------- MCP resources / prompts ----------

_MCP_FULL_SERVER = textwrap.dedent("""
    import json, sys
    def send(obj):
        sys.stdout.write(json.dumps(obj) + "\\n")
        sys.stdout.flush()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method, rid = msg.get("method"), msg.get("id")
        if rid is None:
            continue
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}, "resources": {}, "prompts": {}}}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": rid, "result": {"tools": []}})
        elif method == "resources/list":
            send({"jsonrpc": "2.0", "id": rid, "result": {"resources": [
                {"uri": "mem://notes", "name": "project notes",
                 "mimeType": "text/plain", "description": "团队笔记"}]}})
        elif method == "resources/read":
            send({"jsonrpc": "2.0", "id": rid, "result": {"contents": [
                {"uri": "mem://notes", "type": "text",
                 "text": "笔记正文：先测后写"}]}})
        elif method == "prompts/list":
            send({"jsonrpc": "2.0", "id": rid, "result": {"prompts": [
                {"name": "greet", "description": "问候",
                 "arguments": [{"name": "topic", "description": "主题"}]}]}})
        elif method == "prompts/get":
            args = (msg.get("params") or {}).get("arguments") or {}
            send({"jsonrpc": "2.0", "id": rid, "result": {"messages": [
                {"role": "user", "content": {"type": "text",
                 "text": "请就 " + args.get("topic", "通用") + " 给出建议"}}]}})
    """)


def test_mcp_resources_and_prompts(tmp_path):
    stub = tmp_path / "full_server.py"
    stub.write_text(_MCP_FULL_SERVER, encoding="utf-8")
    mgr = McpManager({"full": {"command": sys.executable,
                               "args": [str(stub)], "timeout": 5}}, tmp_path)
    try:
        tools = mgr.connect_all()
        client = mgr.clients["full"]
        assert client.status == "connected"
        # resources → 只读工具 mcp__full__get__project_notes
        names = [t.name for t in tools]
        assert "mcp__full__get__project_notes" in names
        res_tool = next(t for t in tools if t.name.endswith("get__project_notes"))
        assert res_tool.kind == "read"
        assert "先测后写" in res_tool.run({}, None)
        # prompts → /prompt 调用入口
        assert len(client.prompts) == 1
        text = client.get_prompt("greet", {"topic": "缓存"})
        assert "请就 缓存 给出建议" in text
        lines = "\n".join(mgr.status_lines())
        assert "1 resources" in lines and "1 prompts" in lines
    finally:
        mgr.stop_all()


# ---------- 扩展市场 ----------

@pytest.fixture
def market_env(tmp_path, monkeypatch):
    pack = tmp_path / "pack-src"
    (pack / "commands").mkdir(parents=True)
    (pack / "commands" / "hi.md").write_text("你好 $ARGUMENTS", encoding="utf-8")
    index = tmp_path / "index.json"
    index.write_text(json.dumps({"marketplace": "test", "packs": [
        {"name": "greet-pack", "description": "问候命令包", "version": "1.0",
         "author": "tester", "source": str(pack)}]}), encoding="utf-8")
    user_cfg = tmp_path / "user.json"
    user_cfg.write_text(json.dumps({"marketplaces": [str(index)]}),
                        encoding="utf-8")
    monkeypatch.setattr("minicode.config.USER_CONFIG", user_cfg)
    monkeypatch.chdir(tmp_path)
    return SimpleNamespace(marketplaces=[str(index)]), pack


def test_market_list_and_resolve(market_env):
    from minicode import market
    cfg, pack = market_env
    packs, errors = market.list_packs(cfg)
    assert not errors
    assert packs[0]["name"] == "greet-pack"
    hit = market.resolve_pack(cfg, "greet-pack")
    assert hit["source"] == str(pack)
    assert market.resolve_pack(cfg, "nope") is None
    table = market.format_market(packs)
    assert "greet-pack" in table and "源：" in table


def test_market_errors_do_not_block_other_sources(market_env, tmp_path):
    from minicode import market
    cfg, _ = market_env
    cfg.marketplaces = [str(tmp_path / "missing.json"), cfg.marketplaces[0]]
    packs, errors = market.list_packs(cfg)
    assert len(errors) == 1 and packs


def test_cli_install_by_market_name(market_env, tmp_path, monkeypatch, capsys):
    from minicode.cli import main
    market_env
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.chdir(project)
    rc = main(["--install", "greet-pack"])
    assert rc == 0
    assert (project / ".minicode" / "commands" / "hi.md").exists()
    assert "greet-pack" in capsys.readouterr().out


def test_cli_market_flag(market_env, capsys):
    from minicode.cli import main
    cfg, _ = market_env
    rc = main(["--market"])
    assert rc == 0
    assert "greet-pack" in capsys.readouterr().out


def test_project_marketplaces_are_restricted(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(config_mod, "USER_CONFIG", tmp_path / "no-user.json")
    (tmp_path / ".minicode.json").write_text(json.dumps(
        {"marketplaces": ["https://evil.example/index.json"]}), encoding="utf-8")
    cfg = config_mod.load_config(None)
    assert cfg.marketplaces == []
    assert "marketplaces" in cfg.project_restricted
    config_mod.apply_restricted_config(cfg)
    assert cfg.marketplaces == ["https://evil.example/index.json"]
