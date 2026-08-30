import pytest
from pathlib import Path

from minicode.config import Config
from minicode.session import Session
from minicode.tools.base import ToolContext, ToolError
from minicode.tools.fs import (EditFileTool, GlobTool, GrepTool, ListDirTool,
                               ReadFileTool, WriteFileTool)
from minicode.ui import UI


@pytest.fixture
def ctx(tmp_path):
    cfg = Config(provider="fake", api_key="", model="fake")
    cfg.cwd = tmp_path
    return ToolContext(cwd=tmp_path, config=cfg, session=Session(), ui=UI(),
                       agent_factory=None)


def test_write_read_roundtrip(ctx):
    r = WriteFileTool().run({"path": "a/b.txt", "content": "line1\nline2\n"}, ctx)
    assert "created" in r
    out = ReadFileTool().run({"path": "a/b.txt"}, ctx)
    assert "line1" in out
    assert "2\tline2" in out
    assert "showing lines" not in out


def test_read_offset_and_pagination(ctx):
    WriteFileTool().run({"path": "f.txt",
                         "content": "\n".join(f"l{i}" for i in range(1, 51))}, ctx)
    out = ReadFileTool().run({"path": "f.txt", "offset": 45}, ctx)
    assert "45\tl45" in out
    assert "showing lines 45-50 of 50" in out


def test_read_missing_and_binary(ctx):
    with pytest.raises(ToolError):
        ReadFileTool().run({"path": "nope.txt"}, ctx)
    (ctx.cwd / "bin.dat").write_bytes(b"ab\x00cd")
    with pytest.raises(ToolError):
        ReadFileTool().run({"path": "bin.dat"}, ctx)


def test_edit_unique_and_replace_all(ctx):
    WriteFileTool().run({"path": "e.py", "content": "x = 1\ny = 1\n"}, ctx)
    with pytest.raises(ToolError):  # two matches, not unique
        EditFileTool().run({"path": "e.py", "old_string": "1", "new_string": "2"}, ctx)
    EditFileTool().run({"path": "e.py", "old_string": "y = 1", "new_string": "y = 2"}, ctx)
    assert (ctx.cwd / "e.py").read_text() == "x = 1\ny = 2\n"
    r = EditFileTool().run({"path": "e.py", "old_string": "=",
                            "new_string": "==", "replace_all": True}, ctx)
    assert "2 occurrence" in r
    assert (ctx.cwd / "e.py").read_text() == "x == 1\ny == 2\n"


def test_edit_not_found(ctx):
    WriteFileTool().run({"path": "e2.py", "content": "hello\n"}, ctx)
    with pytest.raises(ToolError):
        EditFileTool().run({"path": "e2.py", "old_string": "zzz", "new_string": "y"}, ctx)


def test_grep_and_glob(ctx):
    WriteFileTool().run({"path": "src/app.py", "content": "def main():\n    pass\n"}, ctx)
    WriteFileTool().run({"path": "src/util.py", "content": "def helper_main():\n    pass\n"}, ctx)
    WriteFileTool().run({"path": "node_modules/x.js", "content": "main();\n"}, ctx)
    g = GrepTool().run({"pattern": "main", "path": "src"}, ctx)
    assert "app.py:1" in g and "util.py:1" in g  # paths relative to search root
    g2 = GrepTool().run({"pattern": "main"}, ctx)
    assert "src/app.py:1" in g2 and "src/util.py" in g2
    assert "node_modules" not in g2  # ignored directory pruned (both paths)
    gl = GlobTool().run({"pattern": "**/*.py"}, ctx)
    assert "src/app.py" in gl and "src/util.py" in gl



def test_glob_brace_expansion(ctx):
    WriteFileTool().run({"path": "a.py", "content": "x"}, ctx)
    WriteFileTool().run({"path": "b.md", "content": "y"}, ctx)
    WriteFileTool().run({"path": "c.txt", "content": "z"}, ctx)
    out = GlobTool().run({"pattern": "*.{py,md}"}, ctx)
    assert "a.py" in out and "b.md" in out
    assert "c.txt" not in out


def test_list_dir(ctx):
    WriteFileTool().run({"path": "pkg/mod.py", "content": "x\n"}, ctx)
    out = ListDirTool().run({"path": ".", "depth": 2}, ctx)
    assert "pkg/" in out and "mod.py" in out


def test_bash_echo_and_cwd(ctx):
    from minicode.tools.shell import BashTool, ShellState, detect_shell
    st = ShellState(Path(ctx.cwd), detect_shell(None))
    tool = BashTool(st)
    r = tool.run({"command": "echo hello"}, ctx)
    assert "hello" in r
    assert "__MCC_PWD__" not in r
    cd_cmd = {"bash": "mkdir -p sub && cd sub",
              "powershell": "New-Item -ItemType Directory -Force sub | Out-Null; Set-Location sub",
              "cmd": "mkdir sub 2>nul & cd sub"}[st.shell]
    tool.run({"command": cd_cmd}, ctx)
    pwd_cmd = {"bash": "pwd", "powershell": "(Get-Location).Path", "cmd": "cd"}[st.shell]
    r2 = tool.run({"command": pwd_cmd}, ctx)
    assert "sub" in r2


def test_bash_exit_code(ctx):
    from minicode.tools.shell import BashTool, ShellState, detect_shell
    tool = BashTool(ShellState(Path(ctx.cwd), detect_shell(None)))
    r = tool.run({"command": "exit 3"}, ctx)
    assert "Exit code: 3" in r


def test_bash_timeout(ctx):
    from minicode.tools.shell import BashTool, ShellState, detect_shell
    st = ShellState(Path(ctx.cwd), detect_shell(None))
    if st.shell != "bash":
        pytest.skip("bash-specific test")
    r = BashTool(st).run({"command": "sleep 5", "timeout": 1}, ctx)
    assert "timed out" in r
