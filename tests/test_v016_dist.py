"""v0.16：扩展分发（minicode install）、MCP 升级（协议版本/超时/非阻塞）。"""
import json
import os
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from minicode.install import InstallError, install_pack
from minicode.mcp import PROTOCOL_VERSION, McpClient, McpError


def make_pack(root: Path, manifest: dict = None) -> Path:
    """标准结构扩展包：skills/commands/agents/tools 各一。"""
    (root / "skills" / "review").mkdir(parents=True)
    (root / "skills" / "review" / "SKILL.md").write_text(
        "---\nname: review\ndescription: 代码审查\n---\n步骤…", encoding="utf-8")
    (root / "skills" / "tip.md").write_text("---\nname: tip\ndescription: 小技巧\n---\n内容",
                                            encoding="utf-8")
    (root / "commands").mkdir()
    (root / "commands" / "deploy.md").write_text("部署到 $ARGUMENTS", encoding="utf-8")
    (root / "agents").mkdir()
    (root / "agents" / "arch.md").write_text("架构审查员", encoding="utf-8")
    (root / "tools").mkdir()
    (root / "tools" / "db.py").write_text("TOOL = {}\ndef run(a, c):\n    return 'ok'\n",
                                          encoding="utf-8")
    if manifest is not None:
        (root / "minicode.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return root


# ---------- 扩展分发 ----------

def test_install_local_pack(tmp_path):
    pack = make_pack(tmp_path / "pack")
    project = tmp_path / "proj"
    project.mkdir()
    r = install_pack(str(pack), project)
    kinds = {i.split(":")[0] for i in r["installed"]}
    assert kinds == {"skill", "command", "agent", "tool"}
    assert (project / ".minicode" / "skills" / "review" / "SKILL.md").exists()
    # 单文件技能自动包装成技能目录
    assert (project / ".minicode" / "skills" / "tip" / "SKILL.md").exists()
    assert (project / ".minicode" / "commands" / "deploy.md").exists()
    assert (project / ".minicode" / "agents" / "arch.md").exists()
    assert (project / ".minicode" / "tools" / "db.py").exists()
    assert any("任意代码" in w for w in r["warnings"])   # 插件安全提示


def test_install_conflict_skip_and_force(tmp_path):
    pack = make_pack(tmp_path / "pack")
    project = tmp_path / "proj"
    project.mkdir()
    install_pack(str(pack), project)
    (project / ".minicode" / "commands" / "deploy.md").write_text("旧版本",
                                                                  encoding="utf-8")
    r = install_pack(str(pack), project)
    assert any("跳过" in s or "已存在" in s for s in r["skipped"])
    assert (project / ".minicode" / "commands" / "deploy.md").read_text(
        encoding="utf-8") == "旧版本"          # 未覆盖
    r2 = install_pack(str(pack), project, force=True)
    assert not r2["skipped"]
    assert (project / ".minicode" / "commands" / "deploy.md").read_text(
        encoding="utf-8") == "部署到 $ARGUMENTS"


def test_install_manifest_explicit_paths(tmp_path):
    pack = tmp_path / "oddpack"
    (pack / "custom").mkdir(parents=True)
    (pack / "custom" / "my-skill").mkdir()
    (pack / "custom" / "my-skill" / "SKILL.md").write_text("x", encoding="utf-8")
    make_manifest = {"name": "odd", "version": "1.2.0",
                     "skills": ["custom/my-skill/SKILL.md"]}
    (pack / "minicode.json").write_text(json.dumps(make_manifest), encoding="utf-8")
    r = install_pack(str(pack), tmp_path)
    assert r["pack"] == "odd" and r["version"] == "1.2.0"
    assert "skill:my-skill" in r["installed"]


def test_install_mcp_servers_never_autoinstalled(tmp_path):
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "minicode.json").write_text(json.dumps(
        {"name": "p", "mcpServers": {"evil": {"command": "rm"}}}), encoding="utf-8")
    r = install_pack(str(pack), tmp_path)
    assert any("mcpServers" in w for w in r["warnings"])
    assert not (tmp_path / ".minicode.json").exists()


def test_install_source_must_exist(tmp_path):
    with pytest.raises(InstallError):
        install_pack(str(tmp_path / "nope"), tmp_path)


@pytest.mark.skipif(not shutil.which("git"), reason="需要 git")
def test_install_from_git_repo(tmp_path):
    """git 源：本地仓库浅克隆安装，临时目录清理干净。"""
    repo = tmp_path / "repo"
    make_pack(repo)
    env = dict(os.environ)
    r = subprocess.run(["git", "init", "-q"], cwd=repo, capture_output=True, env=env)
    assert r.returncode == 0
    (repo / "f.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, env=env)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "init"], cwd=repo, capture_output=True, env=env)
    project = tmp_path / "proj"
    project.mkdir()
    before = set(tmp_path.glob("minicode-install-*"))
    r = install_pack(str(repo), project)
    assert "skill:review" in r["installed"]
    assert (project / ".minicode" / "skills" / "review" / "SKILL.md").exists()
    # 克隆产生的临时目录已清理
    assert set(tmp_path.glob("minicode-install-*")) == before


def test_cli_install_flag(tmp_path, monkeypatch, capsys):
    from minicode.cli import main
    pack = make_pack(tmp_path / "pack")
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.chdir(project)
    rc = main(["--install", str(pack)])
    assert rc == 0
    assert (project / ".minicode" / "skills" / "review" / "SKILL.md").exists()
    out = capsys.readouterr().out
    assert "已安装扩展包" in out


# ---------- MCP 升级 ----------

def test_protocol_version_upgraded():
    assert PROTOCOL_VERSION == "2025-06-18"


def test_mcp_timeout_from_config(tmp_path):
    c = McpClient("t", {"command": "x", "timeout": 2}, tmp_path)
    assert c.timeout == 2
    c = McpClient("t", {"command": "x"}, tmp_path)
    assert c.timeout == 30


_STUB_HANG = textwrap.dedent("""
    import json, sys, time
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
            send({"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": "2025-06-18"}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": rid, "result": {"tools": [
                {"name": "slow", "description": "never answers",
                 "inputSchema": {"type": "object", "properties": {}}}]}})
        elif method == "tools/call":
            time.sleep(3600)   # 挂起不回复 —— 旧实现会永远阻塞
    """)


def test_mcp_hang_times_out_instead_of_blocking(tmp_path):
    """服务器挂起 → 请求按配置超时返回，回合不被卡死（v0.15 前会永久阻塞）。"""
    stub = tmp_path / "hang_server.py"
    stub.write_text(_STUB_HANG, encoding="utf-8")
    client = McpClient("hang", {"command": sys.executable,
                                "args": [str(stub)], "timeout": 2}, tmp_path)
    try:
        assert client.start() is True
        t0 = time.time()
        with pytest.raises(McpError, match="timeout"):
            client.call("slow", {})
        assert time.time() - t0 < 8   # 超时兜底生效，而非 3600s
    finally:
        client.stop()


def test_mcp_negotiates_with_legacy_server(tmp_path):
    """只认旧版协议的服务器：新版被拒后自动用 2024-11-05 重协商。"""
    stub = tmp_path / "legacy_server.py"
    stub.write_text(textwrap.dedent("""
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
            rid = msg.get("id")
            if rid is None:
                continue
            if msg.get("method") == "initialize":
                want = (msg.get("params") or {}).get("protocolVersion")
                if want != "2024-11-05":
                    send({"jsonrpc": "2.0", "id": rid, "error":
                          {"code": -32602, "message": f"unsupported protocol {want}"}})
                else:
                    send({"jsonrpc": "2.0", "id": rid,
                          "result": {"protocolVersion": want}})
            elif msg.get("method") == "tools/list":
                send({"jsonrpc": "2.0", "id": rid, "result": {"tools": []}})
        """), encoding="utf-8")
    client = McpClient("legacy", {"command": sys.executable,
                                  "args": [str(stub)], "timeout": 5}, tmp_path)
    try:
        assert client.start() is True
        assert client.status == "connected"
    finally:
        client.stop()
