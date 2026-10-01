"""v0.21 编辑可靠性三件套：弹性匹配 / 自动编辑 diff / lint 快速回路。"""
import json
import sys

import pytest

from minicode.agent import Agent
from minicode.config import Config
from minicode.fake import FakeProvider
from minicode.lint import detect_lint_command, render_command, run_lint
from minicode.session import Session
from minicode.tools import build_registry
from minicode.tools.base import ToolContext
from minicode.tools.fs import EditFileTool, _flexible_replace
from minicode.tools.shell import ShellState
from minicode.ui import UI


def make_agent(tmp_path, provider, mode="yolo", lint_command="") -> Agent:
    cfg = Config(provider="fake", api_key="", model="fake", mode=mode)
    cfg.cwd = tmp_path
    cfg.lint_command = lint_command
    agent = Agent(provider, Session(), UI(), cfg,
                  build_registry(ShellState(tmp_path, "bash")))
    agent.system_prompt = "test"
    return agent


def edit(tmp_path, file_content, **args):
    """直接调用 EditFileTool：预置 read-before-edit 的 mtime 记录。"""
    p = tmp_path / "a.py"
    p.write_text(file_content, encoding="utf-8")
    s = Session()
    s.file_mtimes[str(p)] = p.stat().st_mtime
    ctx = ToolContext(cwd=tmp_path, config=None, session=s, ui=None)
    r = EditFileTool().run({"path": "a.py", **args}, ctx)
    return r, p.read_text(encoding="utf-8")


class CaptureUI(UI):
    def __init__(self):
        super().__init__()
        self.diffs = []
        self.notes = []

    def tool_diff(self, diff, max_lines=30):
        self.diffs.append(diff)

    def tool_result_note(self, text):
        self.notes.append(str(text))


# ---------- 4. 弹性匹配 ----------

def test_flexible_replace_passes():
    text = "def calc(a, b):\n    return a + b\n"
    # 尾随空格
    r = _flexible_replace(text.replace("b):", "b):  "), "def calc(a, b):", "def calc(a, c):",
                          False, None)
    assert r[0] == "ok" and "calc(a, c)" in r[1]
    # 缩进漂移
    r = _flexible_replace("if x:\n        return a + b\n", "if x:\n    return a + b",
                          "if x:\n    return a - b", False, None)
    assert r[0] == "ok" and "a - b" in r[1]


def test_edit_fuzzy_match_applies_and_reports(tmp_path):
    r, updated = edit(tmp_path, "def calc(a, b):\n    return a + b\n",
                      old_string="def calc(x, b):\n    return a + b",
                      new_string="def calc(a, b):\n    return a * b")
    assert "fuzzy" in r and "def calc(a, b):" in updated and "a * b" in updated


def test_edit_not_found_reports_nearest(tmp_path):
    src = "def calc(a, b):\n    return a + b\n\ndef main():\n    pass\n"
    with pytest.raises(Exception) as ei:
        edit(tmp_path, src, old_string="def totally_absent():\n    return 42",
             new_string="x")
    assert "Closest region" in str(ei.value) and "%" in str(ei.value)


def test_edit_ambiguous_lists_lines(tmp_path):
    with pytest.raises(Exception) as ei:
        edit(tmp_path, "foo( )\nfoo( )\n", old_string="foo()", new_string="bar()")
    assert "lines 1, 2" in str(ei.value)


def test_edit_line_hint_disambiguates_exact(tmp_path):
    src = "keep = 1\nvalue = 1\n"
    r, updated = edit(tmp_path, src, old_string=" = 1", new_string=" = 2",
                      line=1)   # 提示第 1 行 → 命中 keep = 1 而非 value = 1
    assert "disambiguated by line" in r and updated.startswith("keep = 2")


def test_edit_line_hint_picks_fuzzy_window(tmp_path):
    r, updated = edit(tmp_path, "a1 = 1\na2 = 2\n", old_string="a3 = 2",
                      new_string="a2 = 9", line=2)
    assert "fuzzy" in r and "a2 = 9" in updated and "a1 = 1" in updated


# ---------- 5. 自动编辑 diff ----------

def _write_script():
    return [
        {"tool_calls": [{"id": "t1", "name": "read_file",
                         "args": json.dumps({"path": "a.py"})}]},
        {"tool_calls": [{"id": "t2", "name": "write_file",
                         "args": json.dumps({"path": "a.py",
                                             "content": "value = 2\n"})}]},
        {"text": "done"},
    ]


def test_auto_edit_renders_diff(tmp_path):
    (tmp_path / "a.py").write_text("value = 1\n", encoding="utf-8")
    ui = CaptureUI()
    agent = make_agent(tmp_path, FakeProvider(_write_script()),
                       mode="accept-edits")
    agent.ui = ui
    agent.run_turn("改值")
    assert any("value = 1" in d and "value = 2" in d and "+" in d for d in ui.diffs)


def test_full_access_renders_diff_default_does_not(tmp_path):
    class AllowUI(CaptureUI):
        def confirm(self, title, preview=None):
            return "y"

    (tmp_path / "a.py").write_text("value = 1\n", encoding="utf-8")
    agent = make_agent(tmp_path, FakeProvider(_write_script()), mode="full-access")
    ui1 = AllowUI()
    agent.ui = ui1
    agent.run_turn("改值")
    assert ui1.diffs

    agent2 = make_agent(tmp_path, FakeProvider(_write_script()), mode="default")
    ui2 = AllowUI()
    agent2.ui = ui2
    (tmp_path / "a.py").write_text("value = 1\n", encoding="utf-8")
    agent2.run_turn("改值")
    assert ui2.diffs == []     # default 模式由确认对话框预览，不重复渲染


# ---------- 6. lint 快速回路 ----------

PY = f'"{sys.executable}"'


def test_lint_failure_feeds_back(tmp_path):
    (tmp_path / "a.py").write_text("value = 1\n", encoding="utf-8")
    bad = f'{PY} -c "print(\'LINT-FAIL\'); raise SystemExit(1)"'
    ui = CaptureUI()
    agent = make_agent(tmp_path, FakeProvider(_write_script()),
                       mode="accept-edits", lint_command=bad)
    agent.ui = ui
    agent.run_turn("改值")
    assert any("[lint]" in n and "LINT-FAIL" in n for n in ui.notes)


def test_lint_pass_no_feedback(tmp_path):
    (tmp_path / "a.py").write_text("value = 1\n", encoding="utf-8")
    ok = f'{PY} -c "raise SystemExit(0)"'
    ui = CaptureUI()
    agent = make_agent(tmp_path, FakeProvider(_write_script()),
                       mode="accept-edits", lint_command=ok)
    agent.ui = ui
    agent.run_turn("改值")
    assert not any("[lint]" in n for n in ui.notes)


def test_render_command_files_quoted(tmp_path):
    cmd = render_command("ruff check {files}", ["sub dir/a.py", "b.py"], tmp_path)
    assert '"sub dir/a.py"' in cmd and "b.py" in cmd
    assert render_command("ruff check {files}", [], tmp_path).endswith(".")
    assert render_command("make lint", ["a.py"], tmp_path) == "make lint"


def test_run_lint_exit_codes(tmp_path):
    assert run_lint(tmp_path, f'{PY} -c "raise SystemExit(0)"') == ""
    assert "LINT-ERR" in run_lint(tmp_path, f'{PY} -c "print(\'LINT-ERR\');'
                                            f' raise SystemExit(1)"')
    assert "退出码" in run_lint(tmp_path, f'{PY} -c "raise SystemExit(3)"')


def test_detect_lint_command(tmp_path, monkeypatch):
    import minicode.lint as lint_mod
    monkeypatch.setattr(lint_mod.shutil, "which", lambda name: "/usr/bin/x")
    (tmp_path / "ruff.toml").write_text("[lint]\n")
    assert detect_lint_command(tmp_path) == "ruff check {files}"
    (tmp_path / "ruff.toml").unlink()
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\nline-length = 100\n")
    assert detect_lint_command(tmp_path) == "ruff check {files}"
    (tmp_path / "pyproject.toml").unlink()
    assert detect_lint_command(tmp_path) == ""
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / ".eslintrc.json").write_text("{}")
    assert detect_lint_command(tmp_path) == "npx --no-install eslint {files}"
