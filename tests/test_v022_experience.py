"""v0.22 经验引擎 + 渐进披露 + 度量修复。

覆盖审查修正后的三项：
1. Brain 召回：配额均衡渲染（裸截断丢尾部）+ brain_search 检索工具
2. MCP 渐进披露：MCP_INLINE_LIMIT 门控 + mcp_search 动态注册
3. Skills 自生成：触发门 / 提炼落盘 / 命中遥测 / 过期归档
外加两个前置修复的回归测试：P0-2 提示词重建保 skills、-p 补 _after_turn。
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from minicode.agent import Agent
from minicode.config import Config
from minicode.fake import FakeProvider
from minicode.mcp import McpSearchTool
from minicode.prompts import assemble_system_prompt
from minicode.session import Session
from minicode.tools import build_registry
from minicode.tools.base import Tool, ToolRegistry
from minicode.tools.memory import (render_brain,
                                   search_brain_text)
from minicode.tools.shell import ShellState
from minicode.tools.skills import (AUTO_PREFIX, _hits, archive_stale_autoskills,
                                   load_skill, should_autoskill,
                                   skills_catalog, write_autoskill)
from minicode.ui import UI

ROOT = Path(__file__).resolve().parent.parent


def make_agent(tmp_path, provider) -> Agent:
    cfg = Config(provider="fake", api_key="", model="fake", mode="yolo")
    cfg.cwd = tmp_path
    agent = Agent(provider, Session(), UI(), cfg,
                  build_registry(ShellState(tmp_path, "bash")))
    agent.system_prompt = "test"
    return agent


def _write_brain(tmp_path, sections: dict):
    d = tmp_path / ".minicode"
    d.mkdir(exist_ok=True)
    lines = ["# Project Brain", ""]
    for name, items in sections.items():
        lines.append(f"## {name}")
        lines += [f"- {it}" for it in items]
        lines.append("")
    (d / "BRAIN.md").write_text("\n".join(lines), encoding="utf-8")


# ---------- 1. Brain 召回 ----------

def test_render_brain_balanced_not_tail_truncated(tmp_path):
    facts = [f"fact-{i:03d} " + "x" * 60 for i in range(40)]
    _write_brain(tmp_path, {"Facts": facts,
                            "Gotchas": ["gotcha-A 部署超时要加 --timeout",
                                        "gotcha-B 中文编码用 PYTHONUTF8"],
                            "Decisions": ["decision-1 选了零依赖"]})
    out = render_brain(tmp_path, cap=2500)
    assert "## Gotchas" in out and "gotcha-A" in out      # 旧版裸截断会整段丢掉
    assert "## Decisions" in out and "decision-1" in out
    assert "brain_search" in out                          # 省略提示指向检索工具


def test_brain_search_cjk_and_english(tmp_path):
    _write_brain(tmp_path, {"Facts": ["deploy uses scripts/release.yml",
                                      "测试命令是 pytest -q"],
                            "Gotchas": ["部署 超时 需要加大 timeout"]})
    r = search_brain_text(tmp_path, "deploy release")
    assert "release.yml" in r
    r2 = search_brain_text(tmp_path, "测试命令")
    assert "pytest -q" in r2
    r3 = search_brain_text(tmp_path, "超时")
    assert "部署 超时" in r3
    assert "没有匹配" in search_brain_text(tmp_path, "zzz-not-there")
    assert "brain 为空" in search_brain_text(tmp_path / "empty", "x")


def test_brain_search_tool_registered(tmp_path):
    reg = build_registry(ShellState(tmp_path, "bash"))
    assert "brain_search" in reg.tools
    assert len(reg.tools) == 22


# ---------- 2. MCP 渐进披露 ----------

def _stub_tool(name, desc):
    t = Tool()
    t.name = name
    t.description = desc
    t.kind = "mcp"
    t.input_schema = {"type": "object", "properties": {}}
    return t


def test_mcp_search_loads_matching_tools():
    reg = ToolRegistry([])
    pool = [_stub_tool(f"mcp__jira__tool{i}", f"jira issue tool {i}")
            for i in range(10)]
    pool += [_stub_tool(f"mcp__wiki__page{i}", f"wiki page search {i}")
             for i in range(10)]
    search = McpSearchTool(pool, reg)
    out = search.run({"query": "jira issue"}, None)
    assert "mcp__jira__tool0" in out
    loaded = [n for n in reg.tools if n.startswith("mcp__")]
    assert 0 < len(loaded) <= 8


def test_mcp_search_skips_loaded_and_exhausts():
    reg = ToolRegistry([])
    pool = [_stub_tool(f"mcp__s__t{i}", f"tool {i}") for i in range(5)]
    search = McpSearchTool(pool, reg)
    search.run({"query": "tool"}, None)
    first_loaded = list(reg.tools)
    search.run({"query": "tool"}, None)
    for name in first_loaded:
        assert name in reg.tools          # 不重复注册
    # 全部加载后 → 无更多
    while any(t.name not in reg.tools for t in pool):
        search.run({"query": "tool"}, None)
    assert "没有更多" in search.run({"query": "tool"}, None)


def test_registry_register_duplicate_rejected():
    reg = ToolRegistry([_stub_tool("a", "x")])
    try:
        reg.register(_stub_tool("a", "y"))
        raise AssertionError("should reject duplicate")
    except ValueError:
        pass


# ---------- 3. Skills 自生成 ----------

def test_should_autoskill_truth_table():
    assert not should_autoskill(1, 0, True)      # 单文件：不值得沉淀
    assert not should_autoskill(3, 0, None)      # 多文件一把过、无自检：不沉淀
    assert should_autoskill(2, 0, True)          # 自检通过 ✔
    assert should_autoskill(2, 3, None)          # 多次试错后成功 ✔
    assert not should_autoskill(2, 1, False)     # 失败回合不沉淀


SKILL_MD = """---
name: release-check
description: 发布前跑此技能核对版本号与 CHANGELOG
---
1. 核对四处版本号一致：__init__ / pyproject / CHANGELOG / README
2. 跑一致性检查脚本，任何漂移立即修复
3. 全量测试通过后再打 tag
"""


def test_write_autoskill_and_dedup(tmp_path):
    path, created = write_autoskill(tmp_path, SKILL_MD)
    assert created and path is not None
    assert path.name == "auto-release-check"              # auto- 前缀单层目录
    assert (path / "SKILL.md").exists()
    assert "release-check" in skills_catalog(tmp_path)    # 既有发现层直接可见
    _p2, created2 = write_autoskill(tmp_path, SKILL_MD)
    assert not created2                                   # 重名不覆盖


def test_write_autoskill_rejects_garbage(tmp_path):
    assert write_autoskill(tmp_path, "")[1] is False
    assert write_autoskill(tmp_path, "too short")[1] is False
    assert write_autoskill(tmp_path, "---\nname: x\n---\n" + "y" * 200)[1] is True


def test_skill_hit_telemetry(tmp_path):
    d = tmp_path / ".minicode" / "skills" / "my-skill"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
    assert not _hits(tmp_path)
    load_skill(tmp_path, "my-skill")
    load_skill(tmp_path, "my-skill")
    h = _hits(tmp_path)["my-skill"]
    assert h["hits"] == 2 and h.get("last_used")


def test_stale_autoskill_hidden_then_archived(tmp_path):
    d = tmp_path / ".minicode" / "skills" / (AUTO_PREFIX + "old")
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
    old = time.time() - 31 * 86400
    os.utime(d, (old, old))                                  # 目录 mtime 即判龄依据
    assert "release-check" not in skills_catalog(tmp_path)   # 过期：清单不列出
    moved = archive_stale_autoskills(tmp_path)
    assert moved == 1
    assert not d.exists()
    assert (tmp_path / ".minicode" / "skills" / "archive" / (AUTO_PREFIX + "old")
            / "SKILL.md").exists()
    assert archive_stale_autoskills(tmp_path) == 0           # 幂等


# ---------- 4. 前置修复回归 ----------

def test_rebuild_prompt_keeps_skills_section(tmp_path, monkeypatch):
    """P0-2 回归：重建系统提示后 skills 段不得丢失。"""
    d = tmp_path / ".minicode" / "skills" / "proj-skill"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
    cfg = Config(provider="fake", api_key="", model="fake", mode="yolo")
    cfg.cwd = tmp_path
    agent = make_agent(tmp_path, FakeProvider([]))
    agent.config = cfg
    agent._prompt_cwd = tmp_path
    agent._custom_agents = {}
    from minicode.cli import _rebuild_prompt
    _rebuild_prompt(agent)
    assert "release-check" in agent.system_prompt   # frontmatter name 进索引
    assert "release-check" in assemble_system_prompt(cfg, tmp_path, {})


def test_p_mode_runs_after_turn(tmp_path):
    """-p 生命周期回归：turn_end hook 此前在 -p 路径不触发。

    hook 放用户级配置（HOME/USERPROFILE 重定向到 tmp）——项目级 hooks
    属受限字段，非交互 -p 下被信任门禁自动跳过（这是 v0.19 的安全设计）。
    """
    marker = tmp_path / "turn_end_marker.txt"
    hook = (f'"{sys.executable}" -c "open({str(marker)!r}, '
            f'\'w\').write(\'1\')"')
    (tmp_path / ".minicode.json").write_text(json.dumps({
        "hooks": {"turn_end": hook},
    }), encoding="utf-8")
    fake = tmp_path / "fake.json"
    fake.write_text(json.dumps([{"text": "done"}]), encoding="utf-8")
    r = subprocess.run(
        [sys.executable, "-m", "minicode", "-p", "hi", "--yolo", "--no-save",
         "--output-format", "json"],
        cwd=str(tmp_path), capture_output=True, timeout=120,
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONPATH": str(ROOT),
             "MINICODE_FAKE_LLM": str(fake),
             "HOME": str(tmp_path), "USERPROFILE": str(tmp_path)})
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    data = json.loads(r.stdout.decode("utf-8").strip().splitlines()[-1])
    assert data["usage"]["input"] > 0
    assert marker.exists(), "turn_end hook 未随 _after_turn 执行"
