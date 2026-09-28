"""v0.14：协作式中断（Esc / Web 停止按钮）、消息排队、read-before-edit、
行编辑基础设施。全部离线可复算。"""
import json
import os
import threading
import time

import pytest

from minicode.agent import Agent, Interrupted
from minicode.config import Config
from minicode.fake import FakeProvider
from minicode.lineinput import _disp_width, _completions_for
from minicode.session import Session
from minicode.tools.base import Tool, ToolContext, ToolError, ToolRegistry
from minicode.tools.fs import EditFileTool, ReadFileTool, WriteFileTool
from minicode.tools.patch import ApplyPatchTool
from minicode.ui import UI
from minicode.watcher import TurnWatcher


def make_agent(tmp_path, provider, registry=None, mode="full-access") -> Agent:
    cfg = Config(provider="fake", api_key="", model="fake", mode=mode)
    cfg.cwd = tmp_path
    session = Session()
    reg = registry or ToolRegistry([])
    return Agent(provider, session, UI(), cfg, reg)


class StopTool(Tool):
    name = "stop_tool"
    kind = "read"
    description = "sets the interrupt event then returns"

    def __init__(self, ev):
        self.ev = ev

    def describe_call(self, args):
        return "stop"

    def run(self, args, ctx):
        self.ev.set()
        return "stopped"


class EchoTool(Tool):
    name = "echo_tool"
    kind = "read"
    description = "returns its echo"

    def describe_call(self, args):
        return "echo"

    def run(self, args, ctx):
        return "echo"


# ---------- 协作式中断 ----------

def test_interrupt_during_tool_batch_keeps_session_consistent(tmp_path):
    """批次内第一个工具置位中断：回合以 Interrupted 结束，但所有已发出的
    工具调用都回填了结果——下一回合可以安全开始。"""
    ev = threading.Event()
    reg = ToolRegistry([StopTool(ev), EchoTool()])
    provider = FakeProvider([
        {"tool_calls": [{"id": "1", "name": "stop_tool", "args": "{}"},
                        {"id": "2", "name": "echo_tool", "args": "{}"}]},
    ])
    agent = make_agent(tmp_path, provider, reg)
    agent.interrupt_event = ev
    with pytest.raises(Interrupted):
        agent.run_turn("go")
    tool_results = [m for m in agent.session.messages if m.get("role") == "tool"]
    assert len(tool_results) == 2


def test_interrupt_keeps_partial_streamed_text(tmp_path):
    """流式中断：已流出的正文保留为 assistant 消息并标注中断（与
    Claude Code 行为一致），未流出的内容不出现。"""

    class MidStream(FakeProvider):
        def __init__(self, ev):
            super().__init__([])
            self.ev = ev

        def stream(self, messages, tools, system, thinking=0):
            yield {"type": "text_delta", "text": "部分回复"}
            self.ev.set()
            yield {"type": "text_delta", "text": "X"}
            yield {"type": "finish", "message": {"role": "assistant",
                                                 "content": "完整回复"},
                   "usage": {"input": 10, "output": 5}}

    ev = threading.Event()
    agent = make_agent(tmp_path, MidStream(ev))
    agent.interrupt_event = ev
    text = agent.run_turn("go")
    assert "部分回复" in text
    assert "X" not in text
    assert "（回复被用户中断）" in text


def test_interrupt_before_finish_without_text_raises(tmp_path):
    """流式中断且尚无正文：抛 Interrupted（调用方提示「已中断」）。"""

    class MidThink(FakeProvider):
        def __init__(self, ev):
            super().__init__([])
            self.ev = ev

        def stream(self, messages, tools, system, thinking=0):
            yield {"type": "reasoning_delta", "text": "thinking…"}
            self.ev.set()
            yield {"type": "text_delta", "text": "late"}
            yield {"type": "finish", "message": {"role": "assistant",
                                                 "content": "done"},
                   "usage": {"input": 1, "output": 1}}

    ev = threading.Event()
    agent = make_agent(tmp_path, MidThink(ev))
    agent.interrupt_event = ev
    with pytest.raises(Interrupted):
        agent.run_turn("go")


def test_stale_interrupt_does_not_kill_next_turn(tmp_path):
    """run_turn 开始时清除遗留的停止请求——上一回合的 Esc 不影响下一回合。"""
    ev = threading.Event()
    ev.set()
    provider = FakeProvider([{"text": "ok"}])
    agent = make_agent(tmp_path, provider)
    agent.interrupt_event = ev
    assert agent.run_turn("go") == "ok"


# ---------- TurnWatcher（输入解析纯逻辑，无需 tty） ----------

def test_watcher_lone_enter_commits_queue():
    ev = threading.Event()
    w = TurnWatcher(ev)
    w._handle_bytes(b"hello", lambda: False)
    assert w.drain() == []                          # 尚未回车 → 未提交
    w._handle_bytes(b"\r", lambda: False)           # 独立回车 → 提交
    assert w.drain() == ["hello"]
    w._handle_bytes(b"world\r", lambda: False)      # 行尾独立回车 → 提交
    assert w.drain() == ["world"]
    assert ev.is_set() is False


def test_watcher_esc_sets_interrupt_but_not_escape_sequences():
    ev = threading.Event()
    w = TurnWatcher(ev)
    w._handle_bytes(b"\x1b", lambda: False)        # 孤 Esc → 中断
    assert ev.is_set()
    ev2 = threading.Event()
    w2 = TurnWatcher(ev2)
    w2._handle_bytes(b"\x1b[A\x1b[B", lambda: True)  # 方向键序列 → 忽略
    assert not ev2.is_set()


def test_watcher_paste_newlines_buffered_until_lone_enter():
    ev = threading.Event()
    w = TurnWatcher(ev)
    # 粘贴：内部换行（后面还有字节）不提交；最后一次独立回车才提交一条
    w._handle_bytes("第一行\r".encode("utf-8"), lambda: True)
    w._handle_bytes("第二行\r".encode("utf-8"), lambda: False)
    queued = w.drain()
    assert len(queued) == 1
    assert queued[0] == "第一行\n第二行"


def test_watcher_backspace_and_multi_byte_utf8():
    ev = threading.Event()
    w = TurnWatcher(ev)
    w._handle_bytes("你好".encode("utf-8"), lambda: False)
    w._handle_bytes(b"\x7f", lambda: False)        # 退格删除「好」
    w._handle_bytes(b"\r", lambda: False)
    assert w.drain() == ["你"]


def test_watcher_stop_without_tty_is_noop():
    """非交互环境（管道/CI）下 start/stop 不产生线程也不改终端状态。"""
    ev = threading.Event()
    with TurnWatcher(ev) as w:
        pass
    assert w.drain() == []


# ---------- read-before-edit 硬性强制 ----------

def test_edit_without_read_is_refused(tmp_path):
    ctx = ToolContext(cwd=tmp_path, config=Config(), session=Session(), ui=UI())
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(ToolError, match="Read-before-edit"):
        EditFileTool().run({"path": "a.py", "old_string": "x = 1",
                            "new_string": "x = 2"}, ctx)
    assert (tmp_path / "a.py").read_text() == "x = 1\n"   # 未被改动
    ReadFileTool().run({"path": "a.py"}, ctx)
    EditFileTool().run({"path": "a.py", "old_string": "x = 1",
                        "new_string": "x = 2"}, ctx)
    assert (tmp_path / "a.py").read_text() == "x = 2\n"


def test_overwrite_without_read_is_refused_but_new_file_allowed(tmp_path):
    ctx = ToolContext(cwd=tmp_path, config=Config(), session=Session(), ui=UI())
    (tmp_path / "old.txt").write_text("v1", encoding="utf-8")
    with pytest.raises(ToolError, match="Read-before-edit"):
        WriteFileTool().run({"path": "old.txt", "content": "v2"}, ctx)
    WriteFileTool().run({"path": "new.txt", "content": "brand new"}, ctx)
    assert (tmp_path / "new.txt").read_text() == "brand new"


def test_patch_update_without_read_is_refused(tmp_path):
    ctx = ToolContext(cwd=tmp_path, config=Config(), session=Session(), ui=UI())
    (tmp_path / "a.py").write_text("old value\n", encoding="utf-8")
    patch = ("*** Begin Patch\n*** Update File: a.py\n-old value\n+new value\n"
             "*** End Patch")
    with pytest.raises(ToolError, match="Read-before-edit"):
        ApplyPatchTool().run({"patch": patch}, ctx)
    ReadFileTool().run({"path": "a.py"}, ctx)
    ApplyPatchTool().run({"patch": patch}, ctx)
    assert (tmp_path / "a.py").read_text() == "new value\n"


# ---------- 行编辑基础设施 ----------

def test_disp_width_cjk_counts_double():
    assert _disp_width("abc") == 3
    assert _disp_width("你好") == 4
    assert _disp_width("a好b") == 4


def test_completion_slash_commands():
    names = ["/help", "/clear", "/cost"]
    assert _completions_for("/c", names) == ["/clear", "/cost"]
    assert _completions_for("/help", names) == ["/help"]


@pytest.mark.skipif(os.name != "nt", reason="Windows 原生编辑器")
def test_winedit_multiline_row_accounting():
    from minicode.winedit import _Editor
    ed = _Editor("❯ ", [])
    ed.buf = "line1\nline2"
    ed.pos = len(ed.buf)
    rows = ed._display_rows()
    assert rows == 2
    ed.buf = "x" * 300
    ed.pos = len(ed.buf)
    assert ed._display_rows() >= 2   # 长行按终端宽度折行计数


# ---------- Web：停止按钮链路 ----------

def test_webui_stop_interrupts_turn_and_resolves_pending(tmp_path):
    """回合阻塞在浏览器确认卡时点停止：确认按「拒绝」落定、回合以中断
    结束、写操作不落盘。"""
    import urllib.error
    import urllib.request
    from minicode.webui import WebUIServer

    def make_server(tmp_path_, script, mode="default"):
        cfg = Config(provider="fake", api_key="", model="fake", mode=mode)
        cfg.cwd = tmp_path_
        return WebUIServer(cfg, FakeProvider(script), host="127.0.0.1", port=0)

    class Running:
        def __init__(self, srv):
            self.srv = srv
            self.t = threading.Thread(target=srv.serve_forever, daemon=True)
            self.t.start()
            time.sleep(0.05)

        def __enter__(self):
            return self.srv

        def __exit__(self, *exc):
            self.srv.shutdown()
            self.t.join(timeout=5)

    def post(base, path, payload=None, token=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Minicode-Token"] = token
        data = json.dumps(payload if payload is not None else {}).encode()
        r = urllib.request.Request(base + path, data=data, method="POST",
                                   headers=headers)
        return urllib.request.urlopen(r, timeout=10)

    def get(base, path, token=None):
        headers = {"X-Minicode-Token": token} if token else {}
        r = urllib.request.Request(base + path, headers=headers)
        return urllib.request.urlopen(r, timeout=10)

    script = [
        {"tool_calls": [{"id": "t1", "name": "write_file",
                         "args": json.dumps({"path": "x.txt", "content": "hi"})}]},
        {"text": "done"},
    ]
    srv = make_server(tmp_path, script, mode="default")
    with Running(srv):
        base = f"http://127.0.0.1:{srv.port}"
        post(base, "/api/turn", {"prompt": "write"}, token=srv.token)
        deadline = time.time() + 10
        while time.time() < deadline:
            st = json.load(get(base, "/api/status", token=srv.token))
            if st["busy"]:
                break
            time.sleep(0.05)
        assert st["busy"]
        # 无回合时停止 → 409
        with pytest.raises(urllib.error.HTTPError) as ei:
            get(base, "/app.js")
        assert ei.value.code == 401
        # （busy 中）停止当前回合
        r = post(base, "/api/stop", token=srv.token)
        assert json.load(r)["ok"] is True
        deadline = time.time() + 10
        while time.time() < deadline:
            st = json.load(get(base, "/api/status", token=srv.token))
            if not st["busy"]:
                break
            time.sleep(0.05)
        assert not st["busy"]
        assert not (tmp_path / "x.txt").exists()   # 确认被拒绝，未落盘
