"""v0.23 对抗性质量闭环:只读红队 / precision 标注 / spec 驱动。

硬约束逐条落测:
- 红队只读、只出报告、默认不自动触发;
- 报告存档自带盲标三列表;
- spec 的 [auto]/[manual] 从 schema 分流,to-eval 只对存在 [auto] 的
  spec 生效(入口判定,预防而非事后过滤);
- 自检修复回合不做技能提炼;提炼失败在 debug 模式留痕。
"""
import json
import os
import sys
from pathlib import Path


from minicode.agent import Agent
from minicode.checkpoints import CheckpointManager
from minicode.config import Config
from minicode.fake import FakeProvider
from minicode.redteam import (LABELING_TABLE, build_redteam_prompt,
                              redteam_dir, save_report)
from minicode.session import Session
from minicode.specs import (build_implementation_prompt, list_specs,
                            mark_spec, parse_spec, save_spec, to_eval_task)
from minicode.tools import build_registry
from minicode.tools.shell import ShellState
from minicode.ui import UI

ROOT = Path(__file__).resolve().parent.parent


class CaptureUI(UI):
    def __init__(self):
        super().__init__()
        self.infos = []
        self.warns = []
        self.plains = []

    def info(self, m):
        self.infos.append(str(m))

    def warn(self, m):
        self.warns.append(str(m))

    def plain(self, m=""):
        if m:
            self.plains.append(str(m))

    def tool_result_note(self, text):
        pass

    def tool_diff(self, diff, max_lines=30):
        pass


def make_agent(tmp_path, provider, mode="yolo") -> Agent:
    cfg = Config(provider="fake", api_key="", model="fake", mode=mode)
    cfg.cwd = tmp_path
    agent = Agent(provider, Session(), UI(), cfg,
                  build_registry(ShellState(tmp_path, "bash")),
                  checkpoints=CheckpointManager(tmp_path / "ckpt"))
    agent.system_prompt = "test"
    return agent


# ---------- 1. 红队 ----------

def test_redteam_prompt_contains_attack_surface_and_diff():
    p = build_redteam_prompt("diff --git a/x.py", scope="只看并发")
    assert "diff --git a/x.py" in p
    for kw in ("边界条件", "并发", "注入", "回归", "无发现", "阻断|应修|可选",
               "穷举可疑点", "证伪", "已证伪的可疑点"):   # v0.23.3 发散-收敛
        assert kw in p
    assert "只看并发" in p


def test_redteam_truncated_diff_is_announced_not_silent():
    """v0.23.1:静默截断会让红队对未评审部分沉默、报告却显得完整,
    系统性虚高 precision——截断必须写进提示词并强制声明未覆盖区域。"""
    big = "\n".join(f"diff line {i} xxxxxxxxxxxxxxxxxxxxxxxx" for i in range(700))
    assert len(big) > 14000
    p = build_redteam_prompt(big)
    assert "已截断" in p and "14000" in p and f"共 {len(big)} 字符" in p
    assert "未覆盖区域" in p
    small = build_redteam_prompt("tiny diff")
    assert "已截断" not in small


def test_redteam_report_archived_with_labeling_table(tmp_path):
    p = save_report(tmp_path, "## 红队报告\n- [应修] 空指针 (a.py:3)")
    text = p.read_text(encoding="utf-8")
    assert "红队报告" in text and LABELING_TABLE in text
    assert "是否真实存在" in text and "盲标" in text


def test_redteam_command_end_to_end(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    agent = make_agent(tmp_path, FakeProvider([{"text": "ok"}]))
    agent.checkpoints.snapshot(tmp_path / "a.py", "edit_file")
    ui = CaptureUI()
    agent.ui = ui
    monkeypatch.setattr(agent.ctx, "agent_factory",
                        lambda prompt, subagent_type=None:
                        "## 红队报告\n- [应修] 未判空 (a.py:1)")
    from minicode import cli as cli_mod
    cli_mod._command("/redteam", agent, ui, tmp_path)
    assert any("红队报告" in p for p in ui.plains)
    assert any("盲标" in i for i in ui.infos)
    reports = list(redteam_dir(tmp_path).glob("*.md"))
    assert reports and "未判空" in reports[0].read_text(encoding="utf-8")


def test_redteam_needs_changes(tmp_path):
    agent = make_agent(tmp_path, FakeProvider([]))
    agent.ctx.agent_factory = lambda prompt, subagent_type=None: ""
    ui = CaptureUI()
    agent.ui = ui
    from minicode import cli as cli_mod
    cli_mod._command("/redteam", agent, ui, tmp_path)
    assert any("没有文件改动" in w for w in ui.warns)


# ---------- 2. spec ----------

SPEC_MD = """---
id: user-auth
title: 用户登录
status: draft
---
# 需求
支持用户名+密码登录。

# 验收标准
- [auto] `{python} -c "print('t1')"`
- [manual] 移动端登录页可用
- 未标注的这条按 manual 处理
"""


def test_parse_spec_tag_classification(tmp_path):
    p = tmp_path / "user-auth.md"
    p.write_text(SPEC_MD, encoding="utf-8")
    meta, req, criteria = parse_spec(p)
    assert meta["id"] == "user-auth" and "密码登录" in req
    tags = [t for t, _ in criteria]
    assert tags == ["auto", "manual", "manual"]     # 无标签保守归 manual


def test_to_eval_requires_auto(tmp_path):
    save_spec(tmp_path, "no-auto", "无自动项", "# 需求\nx\n# 验收标准\n- [manual] 人工看")
    tp, _cp, msg = to_eval_task(tmp_path, "no-auto")
    assert tp is None and "没有 [auto]" in msg       # 入口判定:预防而非事后过滤

    save_spec(tmp_path, "with-auto", "有自动项",
              "# 需求\n输出 hello\n# 验收标准\n- [auto] `{python} -c \"print('hi')\"`"
              "\n- [manual] 界面友好")
    tp, cp, msg = to_eval_task(tmp_path, "with-auto")
    assert tp is not None and tp.exists() and cp.exists()
    task = json.loads(tp.read_text(encoding="utf-8"))
    assert task["id"] == "spec-with-auto" and "界面友好" in task["instruction"]
    checker = cp.read_text(encoding="utf-8")
    assert "print" in checker and "hi" in checker
    assert list_specs(tmp_path)


def test_spec_mark_done(tmp_path):
    save_spec(tmp_path, "s1", "标题", "# 需求\nx\n# 验收标准\n- [manual] y")
    mark_spec(tmp_path, "s1", "done")
    rows = [(sid, st) for sid, _t, st, _a, _m in list_specs(tmp_path)]
    assert ("s1", "done") in rows


def test_spec_run_auto_verify(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    crit = ("- [auto] `{python} -c \"import os;"
            "assert os.path.exists('hello.txt')\"`")
    save_spec(tmp_path, "hello", "输出 hello",
              "# 需求\n创建 hello.txt 内容 ok\n# 验收标准\n" + crit)
    script = [{"tool_calls": [{"id": "t1", "name": "write_file",
                               "args": json.dumps({"path": "hello.txt",
                                                   "content": "ok"})}]},
              {"text": "done"}]
    agent = make_agent(tmp_path, FakeProvider(script))
    ui = CaptureUI()
    agent.ui = ui
    from minicode import cli as cli_mod
    cli_mod._command("/spec run hello", agent, ui, tmp_path)
    assert (tmp_path / "hello.txt").exists()
    assert any("全部 [auto] 验收通过" in i for i in ui.infos)


def test_implementation_prompt_sections():
    p = build_implementation_prompt("s", "标题", "做 x",
                                    [("auto", "跑测试"), ("manual", "好看")])
    assert "做 x" in p and "机器可判定项" in p and "人工判定项" in p


# ---------- 3. 提炼失败 debug 留痕 ----------

def test_autoskill_failure_logged_in_debug(tmp_path, monkeypatch):
    import minicode.cli as cli_mod
    agent = make_agent(tmp_path, FakeProvider([]))

    class _StubProvider:   # 绕过"演示模型不沉淀"守卫,直测 debug 留痕
        name = "stub"
    agent.provider = _StubProvider()
    agent.config.debug = True
    agent.last_verify_ok = True
    agent.session.add({"role": "user", "content": "hi"})
    monkeypatch.setattr(cli_mod, "maybe_generate_skill",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    ui = CaptureUI()
    agent.ui = ui
    cli_mod._maybe_autoskill(agent, 3, 0)
    assert any("[autoskill]" in w and "boom" in w for w in ui.warns)


def test_redteam_report_carries_diff_before_findings(tmp_path):
    """v0.23.2 盲标协议回归:diff 必须先于红队报告落盘(沙箱即焚,
    没有这一节盲标无法执行);截断时红队实收长度必须写明。"""
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import run_redteam
    finally:
        sys.path.remove(str(ROOT / "scripts"))
    text = run_redteam.assemble_report("t1", True,
                                       "diff --git a/app.py\n+ok",
                                       "## 红队报告\n- [应修] 未判空",
                                       tmp_path)
    i_diff = text.index("## 待评审改动")
    i_report = text.index("## 红队报告\n")
    assert i_diff < i_report                       # 自查材料在前
    assert "标注协议" in text and str(tmp_path) in text
    assert "app.py" in text.split("## 红队报告")[0]
    big = "x" * 15000
    t2 = run_redteam.assemble_report("t2", True, big, "r", tmp_path)
    assert "红队实际收到前 14000 / 15000" in t2    # 截断透明(报告侧)


# ---------- v0.23.4:红队调用失败响亮化 / harness 卫生 / env 一致 ----------

def _load_run_redteam():
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import run_redteam
    finally:
        sys.path.pop(0)
        sys.path.insert(0, str(ROOT / "scripts"))   # 后续断言仍可复用
    return run_redteam


def test_extract_report_success():
    rr = _load_run_redteam()
    line = json.dumps({"result": "## 红队报告\n- [应修] x", "usage": {}})
    report, fail = rr._extract_report('noise before\n' + line + '\n', "")
    assert fail == "" and "未判空" not in report and "应修" in report


def test_extract_report_failure_modes_are_loud():
    rr = _load_run_redteam()
    # 无 JSON 行 → 失败类别 + stdout/stderr 尾部落盘(此前空报告+空 stderr 静默)
    report, fail = rr._extract_report("some error text\n", "Traceback …")
    assert fail == "no-json"
    assert "stdout 尾部" in report and "some error text" in report
    assert "Traceback" in report
    # JSON 但 result 为空 → 显式 empty-result,不是静默成功
    line = json.dumps({"result": "", "usage": {}})
    report2, fail2 = rr._extract_report(line, "")
    assert fail2 == "empty-result" and "红队无输出" in report2


def test_diff_vs_setup_skips_harness_artifacts(tmp_path):
    rr = _load_run_redteam()
    task = {"setup": {"files": {"app.py": "print(1)\n"}}}
    (tmp_path / "app.py").write_text("print(2)\n", encoding="utf-8")
    cache = tmp_path / ".pytest_cache" / "v" / "cache"
    cache.mkdir(parents=True)
    (cache / "lastfailed").write_text("x", encoding="utf-8")
    brain = tmp_path / ".minicode"
    brain.mkdir()
    (brain / "BRAIN.md").write_text("泄露本机路径的内容", encoding="utf-8")
    (tmp_path / "real_new.py").write_text("print(3)\n", encoding="utf-8")
    diff = rr._diff_vs_setup(task, tmp_path)
    assert "app.py" in diff and "real_new.py" in diff   # 真实解法保留
    assert "pytest_cache" not in diff and "BRAIN.md" not in diff  # 副产物滤除


def test_base_env_prepends_interpreter_dir():
    rr = _load_run_redteam()
    env = rr._base_env()
    exe_dir = str(Path(sys.executable).parent)
    assert env["PATH"].startswith(exe_dir + os.pathsep)
    assert env["PYTHONPATH"] == str(ROOT)
    assert env["PYTHONUTF8"] == "1"


# ---------- v0.24:蓝队 A/B 对照 ----------

REPORT_MD = """# 红队 precision 标注 — t1

## 红队报告
### 发现
- [应修] 字面量 --upper 被吞 (echo.py:4)
### 已证伪的可疑点
- [证伪] 空输入崩溃 — 实测打印空行不崩
### 存疑(不确定是否真实)
- [存疑] 编码问题

## 人工标注(precision)
"""


def test_extract_sections_and_modes():
    from minicode.blueteam import blue_input, extract_sections
    secs = extract_sections(REPORT_MD)
    assert any(k.startswith("发现") for k in secs)
    assert any("证伪" in k for k in secs)
    a = blue_input(REPORT_MD, "findings")
    b = blue_input(REPORT_MD, "full")
    assert "被吞" in a and "证伪" not in a          # 对照组 A:看不到证伪过程
    assert "被吞" in b and "证伪" in b              # 实验组 B:全量
    assert "人工标注" not in b                      # 标注表不给蓝队


def test_blueteam_prompt_hard_requirements():
    from minicode.blueteam import build_blueteam_prompt
    p = build_blueteam_prompt("INPUT", "full")
    for kw in ("先核对再修", "举一反三", "同一类问题的其他实例", "红队遗漏",
               "实验组 B"):
        assert kw in p
    pa = build_blueteam_prompt("INPUT", "findings")
    assert "对照组 A" in pa


def test_findings_summary_grades():
    from minicode.blueteam import findings_summary
    md = "## 红队报告\n### 发现\n- [应修] a\n- [可选] b\n- [可选] c\n"
    assert findings_summary(md) == "应修 1 条、可选 2 条"
    assert findings_summary("## 红队报告\n### 发现\n(无)") == "无确认发现"


def test_bluefix_command_wiring(tmp_path, monkeypatch):
    """写权限子代理路径:registry 必须含写工具(蓝队与只读红队的本质区别)。"""
    monkeypatch.chdir(tmp_path)
    agent = make_agent(tmp_path, FakeProvider([]))
    captured = {}

    class FakeSubAgent:
        def __init__(self, provider, session, ui, cfg, registry, **kw):
            captured["registry"] = registry
            captured["session"] = session

        def run_turn(self, prompt):
            captured["prompt"] = prompt
            captured["session"].add({"role": "assistant",
                                     "content": "FIXED"})

    monkeypatch.setattr("minicode.cli.Agent", FakeSubAgent)
    (tmp_path / ".minicode" / "redteam").mkdir(parents=True)
    (tmp_path / ".minicode" / "redteam" / "r1.md").write_text(
        REPORT_MD.replace("t1", "echo"), encoding="utf-8")
    ui = CaptureUI()
    agent.ui = ui
    from minicode import cli as cli_mod
    cli_mod._command("/bluefix full", agent, ui, tmp_path)
    assert "write_file" in captured["registry"].tools   # 写权限
    assert "findings" not in captured["prompt"][:200]   # full 模式
    from minicode.blueteam import blueteam_dir
    reports = list(blueteam_dir(tmp_path).glob("*.md"))
    assert reports and "FIXED" in reports[0].read_text(encoding="utf-8")


def test_bluefix_without_reports_warns(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    agent = make_agent(tmp_path, FakeProvider([]))
    ui = CaptureUI()
    agent.ui = ui
    from minicode import cli as cli_mod
    cli_mod._command("/bluefix", agent, ui, tmp_path)
    assert any("没有红队报告" in w for w in ui.warns)


# ---------- v0.24.1:契约驱动 + 独立自验门 ----------

def test_blueteam_prompt_contract_driven():
    from minicode.blueteam import build_blueteam_prompt
    p = build_blueteam_prompt("INPUT", "findings")
    for kw in ("契约驱动", "已兑现/未兑现/无法验证", "自验命令必须用反引号",
               "独立重跑"):
        assert kw in p


def test_verify_commands_extraction():
    from minicode.blueteam import verify_commands_from_output
    out = ('修复完成。\n- 自验: `python -m pytest -q`\n'
           '- 自验: `python util_check.py`\n不是自验的: `ls`\n')
    assert verify_commands_from_output(out) == ["python -m pytest -q",
                                                "python util_check.py"]
    assert verify_commands_from_output("无自验") == []


def test_independent_verify_runs_commands(tmp_path):
    from minicode.blueteam import independent_verify, render_independent_verify
    ok_cmd = f'"{sys.executable}" -c "print(\'fine\')"'
    bad_cmd = f'"{sys.executable}" -c "print(\'boom\');raise SystemExit(3)"'
    res, env_ok = independent_verify(f"- 自验: `{ok_cmd}`\n- 自验: `{bad_cmd}`",
                                     tmp_path)
    assert env_ok is True                       # 探针通过(命令级 FAIL 才算数)
    assert len(res) == 2
    assert res[0][1] == "PASS" and res[1][1] == "FAIL"   # v0.25 三态
    rendered = render_independent_verify(res, env_ok)
    assert "[PASS]" in rendered and "[FAIL]" in rendered
    assert "自述与实测不符" in rendered
    # 蓝队未声明自验命令 → 按协议视为自验缺失(数据不可信)
    assert "自验缺失" in render_independent_verify([], True)


def test_independent_verify_env_unusable_invalidates_report(tmp_path, monkeypatch):
    """v0.24.2 P0 回归:python 不可达 → 环境不可用、报告作废——
    而不是把 N 条环境性 FAIL 混进数据(首轮实战 6 条全 FAIL 即此)。"""
    import minicode.blueteam as bt
    monkeypatch.setattr(bt, "probe_environment",
                        lambda cwd, timeout=30: (False, "'python' 不是内部或外部命令"))
    res, env_ok = bt.independent_verify(
        "- 自验: `python -c \"print(1)\"`\n- 自验: `python -c \"print(2)\"`",
        tmp_path)
    assert env_ok is False
    assert len(res) == 1 and res[0][0].startswith("环境探针")   # 不逐条跑命令
    rendered = bt.render_independent_verify(res, env_ok)
    assert "环境不可用" in rendered and "数据作废" in rendered
    assert "FAIL]" not in rendered                              # 不混进命令级数据


def test_bluefix_default_mode_is_findings(tmp_path, monkeypatch):
    """A/B 实验结论落地:输入模式定稿 A(只读发现列表)。"""
    monkeypatch.chdir(tmp_path)
    agent = make_agent(tmp_path, FakeProvider([]))

    class FakeSubAgent:
        def __init__(self, provider, session, ui, cfg, registry, **kw):
            self.session = session

        def run_turn(self, prompt):
            assert "对照组 A" in prompt      # 默认 findings
            assert "已证伪的可疑点" not in prompt
            self.session.add({"role": "assistant", "content": "done"})

    monkeypatch.setattr("minicode.cli.Agent", FakeSubAgent)
    (tmp_path / ".minicode" / "redteam").mkdir(parents=True)
    (tmp_path / ".minicode" / "redteam" / "r1.md").write_text(
        REPORT_MD.replace("t1", "echo"), encoding="utf-8")
    ui = CaptureUI()
    agent.ui = ui
    from minicode import cli as cli_mod
    cli_mod._command("/bluefix", agent, ui, tmp_path)   # 不带模式参数
