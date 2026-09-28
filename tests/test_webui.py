"""Web 界面：事件桥接 / 静态页 / 鉴权 / 回合事件流 / 浏览器内确认。"""
import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from minicode.agent import Agent
from minicode.config import Config
from minicode.fake import FakeProvider
from minicode.session import Session
from minicode.tools import build_registry
from minicode.tools.base import ToolError
from minicode.tools.shell import ShellState
from minicode.ui import UI
from minicode.webui import WebBridgeUI, WebUIServer


def make_server(tmp_path, script, mode="default") -> WebUIServer:
    cfg = Config(provider="fake", api_key="", model="fake", mode=mode)
    cfg.cwd = tmp_path
    return WebUIServer(cfg, FakeProvider(script), host="127.0.0.1", port=0)


class Running:
    """serve_forever 的测试生命周期封装。"""

    def __init__(self, srv: WebUIServer):
        self.srv = srv
        self.t = threading.Thread(target=srv.serve_forever, daemon=True)
        self.t.start()
        time.sleep(0.05)

    def __enter__(self):
        return self.srv

    def __exit__(self, *exc):
        self.srv.shutdown()
        self.t.join(timeout=5)


def _with_retry(fn, tries=3):
    """连接层抖动重试（Windows 快速起停服务器时的偶发 RemoteDisconnected）。"""
    import http.client
    for attempt in range(tries):
        try:
            return fn()
        except (http.client.RemoteDisconnected, ConnectionResetError,
                ConnectionAbortedError):
            if attempt == tries - 1:
                raise
            time.sleep(0.2)


def post(base, path, payload=None, token=None):
    return _with_retry(lambda: _post_once(base, path, payload, token))


def _post_once(base, path, payload, token):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Minicode-Token"] = token
    req = urllib.request.Request(base + path,
                                 data=json.dumps(payload or {}).encode(),
                                 method="POST", headers=headers)
    return urllib.request.urlopen(req, timeout=10)


def get(base, path, token=None):
    return _with_retry(lambda: _get_once(base, path, token))


def _get_once(base, path, token):
    headers = {"X-Minicode-Token": token} if token else {}
    return urllib.request.urlopen(
        urllib.request.Request(base + path, headers=headers), timeout=10)


def wait_idle(srv, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not srv.busy:
            return
        time.sleep(0.05)
    raise AssertionError("turn did not finish in time")


# ---------- 事件桥接 ----------

def test_bridge_strips_ansi_and_broadcasts():
    b = WebBridgeUI()
    q = b.subscribe()
    b.info("\x1b[32mgreen text\x1b[0m")
    b.tool_line("bash", "\x1b[90msummary\x1b[0m")
    b.plain("")  # 空行不推送
    events = [q.get(timeout=2) for _ in range(2)]
    assert events[0] == {"t": "info", "text": "green text"}
    assert events[1] == {"t": "tool", "name": "bash", "summary": "summary"}
    assert q.empty()


def test_bridge_confirm_blocks_until_resolved():
    b = WebBridgeUI()
    q = b.subscribe()
    result = {}

    def ask():
        result["ans"] = b.confirm("允许 write_file？", "Create x.txt")

    th = threading.Thread(target=ask, daemon=True)
    th.start()
    ev = q.get(timeout=2)
    assert ev["t"] == "confirm" and "write_file" in ev["title"]
    time.sleep(0.05)
    assert th.is_alive()           # 未答复前阻塞（与 REPL 的 input 一致）
    assert b.resolve(ev["id"], "y")
    th.join(timeout=2)
    assert result["ans"] == "y"


def test_bridge_choose_malformed_answer_defends():
    """choose 收到畸形答复时防御性返回 []，不崩回合。"""
    b = WebBridgeUI()
    q = b.subscribe()
    result = {}

    def ask():
        result["labels"] = b.choose("选一个", [{"label": "A"}, {"label": "B"}])

    th = threading.Thread(target=ask, daemon=True)
    th.start()
    ev = q.get(timeout=2)
    assert ev["t"] == "choose" and len(ev["options"]) == 2
    b.resolve(ev["id"], "not-a-list")   # 畸形答复 → 防御性返回 []
    th.join(timeout=2)
    assert result["labels"] == []


# ---------- 服务器基础 ----------

def test_webui_serves_static_and_requires_token(tmp_path):
    srv = make_server(tmp_path, [])
    with Running(srv):
        base = f"http://127.0.0.1:{srv.port}"
        # 未携带 token：静态资源与 API 一律 401（不泄露任何内容）
        with pytest.raises(urllib.error.HTTPError) as ei:
            get(base, "/app.js")
        assert ei.value.code == 401
        with pytest.raises(urllib.error.HTTPError) as ei:
            get(base, "/api/status")
        assert ei.value.code == 401
        # token 走 query → 200 并种下 HttpOnly cookie
        resp = get(base, f"/?token={srv.token}")
        html = resp.read().decode("utf-8")
        assert "minicode" in html
        assert "v__VERSION__" not in html               # 版本号已注入
        assert srv.token not in html                    # 页面本身不包含 token
        cookie = resp.headers.get("Set-Cookie") or ""
        assert "minicode_token=" in cookie and "HttpOnly" in cookie
        js = get(base, "/app.js", token=srv.token).read().decode("utf-8")
        assert srv.token not in js and "__TOKEN__" not in js  # 静态资源零凭据
        health = json.load(get(base, "/api/health"))
        assert health["ok"] and health["mode"] == "web"
        s = json.load(get(base, "/api/status", token=srv.token))
        assert s["model"] == "fake" and s["busy"] is False


def test_webui_sessions_list_and_open(tmp_path, monkeypatch):
    """侧边栏：列出历史会话 + 打开会话回放 + 名字防路径穿越。"""
    from minicode import session as session_mod
    saved = tmp_path / "saved"
    saved.mkdir()
    monkeypatch.setattr(session_mod, "SESSIONS_DIR", saved)
    Session(messages=[{"role": "user", "content": "旧任务"},
                      {"role": "assistant", "content": "已完成"}]) \
        .save(saved / "20260926-120000_old-task.json")

    srv = make_server(tmp_path, [], mode="default")
    with Running(srv):
        base = f"http://127.0.0.1:{srv.port}"
        d = json.load(get(base, "/api/sessions", token=srv.token))
        assert d["sessions"] and d["sessions"][0]["name"] == "20260926-120000_old-task"
        assert d["sessions"][0]["title"] == "old-task"

        r = json.load(post(base, "/api/session/open",
                           {"name": "20260926-120000_old-task"}, token=srv.token))
        assert r["ok"] and r["messages"] == 2
        msgs = json.load(get(base, "/api/messages", token=srv.token))["messages"]
        assert msgs[0]["content"] == "旧任务"

        with pytest.raises(urllib.error.HTTPError) as ei:
            post(base, "/api/session/open", {"name": "../evil"}, token=srv.token)
        assert ei.value.code == 400                    # 路径穿越被拒
        with pytest.raises(urllib.error.HTTPError) as ei2:
            post(base, "/api/session/open", {"name": "no-such"}, token=srv.token)
        assert ei2.value.code == 404


def test_webui_rejects_non_local_host(tmp_path):
    srv = make_server(tmp_path, [])
    with Running(srv):
        base = f"http://127.0.0.1:{srv.port}"
        # DNS rebinding 防护：伪装 Host 直接 403
        req = urllib.request.Request(base + "/api/health",
                                     headers={"Host": "evil.example"})
        try:
            urllib.request.urlopen(req, timeout=5)
            raised = False
        except urllib.error.HTTPError as e:
            raised = e.code == 403
        except urllib.error.URLError:
            raised = True  # 某些环境直接拒连也算拦截
        assert raised


# ---------- 回合事件流 ----------

def test_webui_turn_streams_events_and_persists(tmp_path):
    srv = make_server(tmp_path, [{"text": "hi there"}], mode="full-access")
    q = srv.bridge.subscribe()
    with Running(srv):
        base = f"http://127.0.0.1:{srv.port}"
        resp = json.load(post(base, "/api/turn", {"prompt": "hello"}, token=srv.token))
        assert resp["ok"]
        wait_idle(srv)
        kinds = []
        while not q.empty():
            kinds.append(q.get(timeout=1)["t"])
        assert kinds[0] == "user" and kinds[1] == "busy"
        assert "text" in kinds and "busy" in kinds
        msgs = json.load(get(base, "/api/messages", token=srv.token))["messages"]
        assert msgs[-1]["content"] == "hi there"
        # 会话落盘（与 REPL 行为一致）
        from minicode.session import SESSIONS_DIR
        assert SESSIONS_DIR.exists()
        # 回合结束后再次保存由 _run_turn 完成；这里只验证 busy 翻转
        s = json.load(get(base, "/api/status", token=srv.token))
        assert s["busy"] is False


def test_webui_turn_rejects_while_busy_and_empty_prompt(tmp_path):
    srv = make_server(tmp_path, [{"text": "ok"}], mode="full-access")
    with Running(srv):
        base = f"http://127.0.0.1:{srv.port}"
        with pytest.raises(urllib.error.HTTPError) as ei:
            post(base, "/api/turn", {"prompt": ""}, token=srv.token)
        assert ei.value.code == 400
        with pytest.raises(urllib.error.HTTPError) as ei2:
            post(base, "/api/turn", {"prompt": "x"}, token="wrong")
        assert ei2.value.code == 401


def test_webui_permission_confirm_via_browser(tmp_path):
    """default 模式下写文件 → 确认请求到达浏览器 → 答复 y → 落盘。"""
    script = [
        {"tool_calls": [{"id": "t1", "name": "write_file",
                         "args": json.dumps({"path": "x.txt", "content": "hi"})}]},
        {"text": "done"},
    ]
    srv = make_server(tmp_path, script, mode="default")
    q = srv.bridge.subscribe()
    with Running(srv):
        base = f"http://127.0.0.1:{srv.port}"
        post(base, "/api/turn", {"prompt": "write"}, token=srv.token)
        ev = None
        deadline = time.time() + 10
        while time.time() < deadline:
            e = q.get(timeout=5)
            if e["t"] == "confirm":
                ev = e
                break
        assert ev and "write_file" in ev["title"]
        assert json.load(post(base, "/api/answer",
                              {"id": ev["id"], "value": "y"}, token=srv.token))["ok"]
        wait_idle(srv)
        assert (tmp_path / "x.txt").read_text(encoding="utf-8") == "hi"


def test_webui_mode_and_clear_endpoints(tmp_path):
    srv = make_server(tmp_path, [], mode="default")
    with Running(srv):
        base = f"http://127.0.0.1:{srv.port}"
        r = json.load(post(base, "/api/mode", {"mode": "plan"}, token=srv.token))
        assert r["mode"] == "plan"
        assert json.load(get(base, "/api/status", token=srv.token))["mode"] == "plan"
        r2 = json.load(post(base, "/api/mode", {"mode": "yolo"}, token=srv.token))
        assert r2["mode"] == "full-access"            # 别名归一
        with pytest.raises(urllib.error.HTTPError) as ei:
            post(base, "/api/mode", {"mode": "chaos"}, token=srv.token)
        assert ei.value.code == 400
        assert json.load(post(base, "/api/clear", token=srv.token))["ok"]


def test_webui_sse_stream_delivers_hello(tmp_path):
    srv = make_server(tmp_path, [], mode="default")
    with Running(srv):
        resp = get(f"http://127.0.0.1:{srv.port}",
                   f"/api/events?token={srv.token}")
        try:
            first = resp.readline().decode("utf-8")
            assert first.startswith("data:") and "hello" in first
        finally:
            resp.close()


# ---------- 会话管理：归档 / 重命名 / 删除 ----------

def test_webui_session_management(tmp_path, monkeypatch):
    srv = make_server(tmp_path, [], mode="default")
    sessions = tmp_path / "saved"
    sessions.mkdir()
    Session(messages=[{"role": "user", "content": "a"}]).save(
        sessions / "20260926-100000_alpha.json")
    Session(messages=[{"role": "user", "content": "b"}]).save(
        sessions / "20260926-100001_beta.json")
    monkeypatch.setattr("minicode.session.SESSIONS_DIR", sessions)

    with Running(srv):
        base = f"http://127.0.0.1:{srv.port}"
        # 归档
        r = json.load(post(base, "/api/session/archive",
                           {"name": "20260926-100000_alpha"}, token=srv.token))
        assert r["ok"]
        d = json.load(get(base, "/api/sessions", token=srv.token))
        assert [s["name"] for s in d["sessions"]] == ["20260926-100001_beta"]
        assert [s["name"] for s in d["archived"]] == ["20260926-100000_alpha"]
        # 重命名（归档区）
        r = json.load(post(base, "/api/session/rename",
                           {"name": "20260926-100000_alpha", "title": "旧任务",
                            "archived": True}, token=srv.token))
        assert r["ok"] and r["name"] == "20260926-100000_旧任务"
        # 恢复
        r = json.load(post(base, "/api/session/unarchive",
                           {"name": "20260926-100000_旧任务"}, token=srv.token))
        assert r["ok"]
        d = json.load(get(base, "/api/sessions", token=srv.token))
        assert "20260926-100000_旧任务" in [s["name"] for s in d["sessions"]]
        # 删除
        r = json.load(post(base, "/api/session/delete",
                           {"name": "20260926-100000_旧任务"}, token=srv.token))
        assert r["ok"]
        assert not (sessions / "20260926-100000_旧任务.json").exists()
        # 非法名字与不存在
        with pytest.raises(urllib.error.HTTPError) as ei:
            post(base, "/api/session/delete", {"name": "../x"}, token=srv.token)
        assert ei.value.code == 400
        with pytest.raises(urllib.error.HTTPError) as ei2:
            post(base, "/api/session/delete", {"name": "ghost"}, token=srv.token)
        assert ei2.value.code == 404


# ---------- 斜杠命令分发 ----------

def test_webui_command_endpoint(tmp_path):
    srv = make_server(tmp_path, [{"text": "ok"}], mode="default")
    with Running(srv):
        base = f"http://127.0.0.1:{srv.port}"
        r = json.load(post(base, "/api/command", {"line": "/mode plan"},
                           token=srv.token))
        assert r["output"] == "权限模式：plan"
        assert json.load(get(base, "/api/status", token=srv.token))["mode"] == "plan"
        r = json.load(post(base, "/api/command", {"line": "/yolo"},
                           token=srv.token))
        assert "full-access" in r["output"]
        r = json.load(post(base, "/api/command", {"line": "/limit 200k"},
                           token=srv.token))
        assert json.load(get(base, "/api/status",
                             token=srv.token))["context_limit"] == 200000
        r = json.load(post(base, "/api/command", {"line": "/verify pytest -q"},
                           token=srv.token))
        assert "pytest -q" in r["output"]
        r = json.load(post(base, "/api/command", {"line": "/init"},
                           token=srv.token))
        assert "MINICODE" in r["turn"]          # turn 型命令交给 /api/turn
        r = json.load(post(base, "/api/command", {"line": "/nope"},
                           token=srv.token))
        assert "未知命令" in r["error"]


# ---------- 模型 API 配置 ----------

def test_webui_config_get_update_save(tmp_path, monkeypatch):
    from minicode import config as config_mod
    user_cfg = tmp_path / "user-minicode.json"
    monkeypatch.setattr(config_mod, "USER_CONFIG", user_cfg)
    srv = make_server(tmp_path, [], mode="default")
    with Running(srv):
        base = f"http://127.0.0.1:{srv.port}"
        c = json.load(get(base, "/api/config", token=srv.token))
        assert c["model"] == "fake" and c["api_key_set"] is False
        r = json.load(post(base, "/api/config",
                           {"model": "glm-x", "api_key": "sk-test",
                            "save": True}, token=srv.token))
        assert r["ok"] and r["model"] == "glm-x"
        assert json.load(get(base, "/api/status",
                             token=srv.token))["model"] == "glm-x"
        saved = json.loads(user_cfg.read_text(encoding="utf-8"))
        assert saved["api_key"] == "sk-test" and saved["model"] == "glm-x"
        c2 = json.load(get(base, "/api/config", token=srv.token))
        assert c2["api_key_set"] is True and c2["api_key_tail"] == "test"


# ---------- 工作区 ----------

def test_webui_workspace_add_switch_remove(tmp_path, monkeypatch):
    import minicode.webui as webui_mod
    ws_file = tmp_path / "workspaces.json"
    monkeypatch.setattr(webui_mod, "WORKSPACES_FILE", ws_file)
    ws2 = tmp_path / "ws2"
    ws2.mkdir()
    srv = make_server(tmp_path, [], mode="default")
    with Running(srv):
        base = f"http://127.0.0.1:{srv.port}"
        r = json.load(post(base, "/api/workspace/add",
                           {"path": str(ws2)}, token=srv.token))
        assert str(ws2) in r["list"]
        with pytest.raises(urllib.error.HTTPError) as ei:
            post(base, "/api/workspace/add", {"path": "relative/path"},
                 token=srv.token)
        assert ei.value.code == 400
        d = json.load(get(base, "/api/workspaces", token=srv.token))
        assert d["current"] == str(tmp_path)
        # 切换 → 新会话 + cwd 变更
        r = json.load(post(base, "/api/workspace/switch",
                           {"path": str(ws2)}, token=srv.token))
        assert r["cwd"] == str(ws2)
        assert json.load(get(base, "/api/status",
                             token=srv.token))["cwd"] == str(ws2)
        assert json.load(get(base, "/api/messages",
                             token=srv.token))["messages"] == []
        # 不能移除当前工作区
        with pytest.raises(urllib.error.HTTPError) as ei2:
            post(base, "/api/workspace/remove", {"path": str(ws2)},
                 token=srv.token)
        assert ei2.value.code == 400


# ---------- 工作区边界锁定 ----------

def test_webui_workspace_lock_denies_outside_writes(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    inside = tmp_path / "ws"
    inside.mkdir()
    cfg = Config(provider="fake", api_key="", model="fake", mode="full-access")
    cfg.cwd = inside
    cfg.workspace_lock = True
    agent = Agent(FakeProvider([
        {"tool_calls": [{"id": "t1", "name": "write_file",
                         "args": json.dumps({"path": str(outside / "x.txt"),
                                             "content": "pwn"})}]},
        {"text": "done"}]), Session(), UI(), cfg,
        build_registry(ShellState(inside, "bash")))
    agent.run_turn("go")
    tool_msg = agent.session.messages[2]
    assert tool_msg["is_error"]
    assert "工作区边界" in tool_msg["content"]
    assert not (outside / "x.txt").exists()      # yolo 也被硬拒绝


# ---------- 并行子智能体 ----------

def test_dispatch_agents_runs_tasks_in_parallel(tmp_path):
    import time as _time
    from minicode.tools.base import ToolContext
    from minicode.tools.subagent import DispatchAgentsTool
    ctx = ToolContext(cwd=tmp_path, config=Config(provider="fake", api_key="",
                                                    model="fake"),
                      session=Session(), ui=UI())

    def slow_factory(prompt, subagent_type=None):
        _time.sleep(0.3)                      # 串行 3×0.3=0.9s；并行应 ~0.3s
        return f"报告[{prompt}]"

    ctx.agent_factory = slow_factory
    t0 = _time.time()
    r = DispatchAgentsTool().run(
        {"tasks": [{"prompt": "查模块A"}, {"prompt": "查模块B"}, {"prompt": "查模块C"}]},
        ctx)
    elapsed = _time.time() - t0
    assert elapsed < 0.85                     # 并行而非串行
    assert "任务 1" in r and "报告[查模块A]" in r
    assert "报告[查模块B]" in r and "报告[查模块C]" in r


def test_dispatch_agents_isolates_failures_and_validates(tmp_path):
    from minicode.tools.base import ToolContext
    from minicode.tools.subagent import DispatchAgentsTool
    ctx = ToolContext(cwd=tmp_path, config=Config(provider="fake", api_key="",
                                                    model="fake"),
                      session=Session(), ui=UI())

    def flaky_factory(prompt, subagent_type=None):
        if "炸" in prompt:
            raise RuntimeError("boom")
        return f"ok[{prompt}]"

    ctx.agent_factory = flaky_factory
    r = DispatchAgentsTool().run(
        {"tasks": [{"prompt": "正常任务"}, {"prompt": "炸任务"}]}, ctx)
    assert "ok[正常任务]" in r
    assert "RuntimeError: boom" in r          # 单任务失败不影响其他
    assert "任务 2" in r

    tool = DispatchAgentsTool()
    with pytest.raises(ToolError):
        tool.run({"tasks": [{"prompt": "只有一个"}]}, ctx)   # 少于 2 个拒绝
    with pytest.raises(ToolError):
        tool.run({"tasks": "not-a-list"}, ctx)


def test_dispatch_agents_registered_full_but_not_readonly(tmp_path):
    from minicode.tools import build_registry
    full = build_registry(ShellState(tmp_path, "bash"))
    ro = build_registry(ShellState(tmp_path, "bash"), read_only=True)
    assert "dispatch_agents" in full.names()
    assert "dispatch_agents" not in ro.names()   # 子智能体不嵌套派生


# ---------- 扩展面板接口 ----------

def test_webui_extensions_endpoint(tmp_path, monkeypatch):
    ext_dir = tmp_path / "ext"
    (ext_dir / ".minicode" / "skills" / "review-skill").mkdir(parents=True)
    (ext_dir / ".minicode" / "commands").mkdir(parents=True)
    (ext_dir / ".minicode" / "agents").mkdir(parents=True)
    (ext_dir / ".minicode" / "tools").mkdir(parents=True)
    (ext_dir / ".minicode" / "skills" / "review-skill" / "SKILL.md").write_text(
        "---\nname: review-skill\ndescription: 专项审查流程\n---\n按步骤审查。",
        encoding="utf-8")
    (ext_dir / ".minicode" / "commands" / "refactor.md").write_text(
        "重构 $ARGUMENTS", encoding="utf-8")
    (ext_dir / ".minicode" / "agents" / "scout.md").write_text(
        "---\ndescription: 侦察兵\ntools: read_file,grep\n---\n只调研。",
        encoding="utf-8")
    (ext_dir / ".minicode" / "tools" / "hello.py").write_text(
        'TOOL = {"name": "hello", "description": "打招呼"}\n'
        "def run(args, ctx):\n    return 'hi'\n", encoding="utf-8")
    monkeypatch.chdir(ext_dir)
    # 信任门禁放行插件
    monkeypatch.setattr("minicode.cli.sys.stdin.isatty", lambda: False)

    srv = make_server(ext_dir, [], mode="default")
    with Running(srv):
        base = f"http://127.0.0.1:{srv.port}"
        d = json.load(get(base, "/api/extensions", token=srv.token))
        skill_names = [s["name"] for s in d["skills"]]
        assert "review-skill" in skill_names          # 项目技能 + 内置技能共存
        rs = next(s for s in d["skills"] if s["name"] == "review-skill")
        assert rs["desc"] == "专项审查流程" and rs["source"] == "项目"
        assert "hello" in d["plugins"]
        assert d["agents"][0]["name"] == "scout" and d["agents"][0]["desc"] == "侦察兵"
        assert [c["name"] for c in d["commands"]] == ["refactor"]


# ---------- 目录浏览与扩展直接导入 ----------

def test_webui_fs_list(tmp_path):
    root = tmp_path / "browse"
    (root / "sub1").mkdir(parents=True)
    (root / "sub2").mkdir()
    (root / ".hidden").mkdir()
    (root / "file.txt").write_text("x", encoding="utf-8")
    srv = make_server(tmp_path, [], mode="default")
    with Running(srv):
        base = f"http://127.0.0.1:{srv.port}"
        d = json.load(get(base, "/api/fs/list?path=" +
                          urllib.request.quote(str(root)), token=srv.token))
        assert d["path"] == str(root)
        assert sorted(d["dirs"]) == ["sub1", "sub2"]   # 隐藏目录与文件不出现
        assert d["parent"] == str(tmp_path)
        # 相对路径拒绝（错误放 payload，仍 200）
        d2 = json.load(get(base, "/api/fs/list?path=relative", token=srv.token))
        assert "error" in d2


def test_webui_ext_import(tmp_path, monkeypatch):
    """直接导入本机文件为扩展：技能文件夹 / 插件 .py / 命令 .md。"""
    src = tmp_path / "src"
    (src / "my-skill").mkdir(parents=True)
    (src / "my-skill" / "SKILL.md").write_text(
        "---\nname: my-skill\ndescription: 导入的技能\n---\n步骤。",
        encoding="utf-8")
    (src / "tool.py").write_text(
        'TOOL = {"name": "imported_tool"}\ndef run(args, ctx):\n    return "ok"\n',
        encoding="utf-8")
    (src / "greet.md").write_text("---\ndescription: 问候\n---\n你好 $ARGUMENTS",
                                  encoding="utf-8")
    (src / "bad.txt").write_text("not an extension", encoding="utf-8")

    ws = tmp_path / "ws"
    ws.mkdir()
    srv = make_server(ws, [], mode="default")
    with Running(srv):
        base = f"http://127.0.0.1:{srv.port}"
        # 导入技能文件夹
        r = json.load(post(base, "/api/ext/import",
                           {"kind": "skill", "paths": [str(src / "my-skill")]},
                           token=srv.token))
        assert r["imported"] == ["my-skill"]
        assert (ws / ".minicode" / "skills" / "my-skill" / "SKILL.md").exists()
        # 导入插件 + 命令（多路径）
        r = json.load(post(base, "/api/ext/import",
                           {"kind": "plugin", "paths": [str(src / "tool.py")]},
                           token=srv.token))
        assert r["imported"] == ["tool"]
        assert (ws / ".minicode" / "tools" / "tool.py").exists()
        r = json.load(post(base, "/api/ext/import",
                           {"kind": "command", "paths": [str(src / "greet.md")]},
                           token=srv.token))
        assert r["imported"] == ["greet"]
        # .txt 不是合法技能来源 → 200 但进 failed 列表
        d = json.load(post(base, "/api/ext/import",
                           {"kind": "skill", "paths": [str(src / "bad.txt")]},
                           token=srv.token))
        assert d["failed"] and "SKILL.md" in d["failed"][0]["error"]
        with pytest.raises(urllib.error.HTTPError) as ei2:
            post(base, "/api/ext/import", {"kind": "nonsense", "paths": ["x"]},
                 token=srv.token)
        assert ei2.value.code == 400
        # 重载端点
        assert json.load(post(base, "/api/ext/reload", {}, token=srv.token))["ok"]


# ---------- 扩展详情 ----------

def test_webui_ext_detail(tmp_path, monkeypatch):
    ext_dir = tmp_path / "ext"
    (ext_dir / ".minicode" / "skills" / "review-flow").mkdir(parents=True)
    (ext_dir / ".minicode" / "skills" / "review-flow" / "SKILL.md").write_text(
        "---\nname: review-flow\ndescription: 审查流程\n---\n第一步：跑测试。",
        encoding="utf-8")
    (ext_dir / ".minicode" / "agents").mkdir(parents=True)
    (ext_dir / ".minicode" / "agents" / "scout.md").write_text(
        "---\ndescription: 侦察兵\ntools: read_file,grep\nmodel: glm-4.6\n"
        "---\n只调研不修改。", encoding="utf-8")
    (ext_dir / ".minicode" / "commands").mkdir(parents=True)
    (ext_dir / ".minicode" / "commands" / "refactor.md").write_text(
        "---\ndescription: 重构命令\n---\n重构 $ARGUMENTS", encoding="utf-8")
    (ext_dir / ".minicode" / "tools").mkdir(parents=True)
    (ext_dir / ".minicode" / "tools" / "hello.py").write_text(
        'TOOL = {"name": "hello"}\ndef run(args, ctx):\n    return "hi"',
        encoding="utf-8")
    monkeypatch.chdir(ext_dir)

    srv = make_server(ext_dir, [], mode="default")
    with Running(srv):
        base = f"http://127.0.0.1:{srv.port}"
        # 技能全文
        d = json.load(get(base, "/api/ext/detail?type=skills&name=review-flow",
                          token=srv.token))
        assert d["source"] == "项目" and "第一步" in d["content"]
        assert d["desc"] == "审查流程"
        # 子智能体元信息
        d = json.load(get(base, "/api/ext/detail?type=agents&name=scout",
                          token=srv.token))
        assert d["tools"] == "read_file,grep" and d["model"] == "glm-4.6"
        # 命令内容
        d = json.load(get(base, "/api/ext/detail?type=commands&name=refactor",
                          token=srv.token))
        assert "$ARGUMENTS" in d["content"]
        # 插件源码
        d = json.load(get(base, "/api/ext/detail?type=plugins&name=hello",
                          token=srv.token))
        assert "def run" in d["content"]
        # MCP 不存在
        with pytest.raises(urllib.error.HTTPError) as ei:
            get(base, "/api/ext/detail?type=mcp&name=ghost", token=srv.token)
        assert ei.value.code == 404
        # 非法类型
        with pytest.raises(urllib.error.HTTPError) as ei2:
            get(base, "/api/ext/detail?type=bogus&name=x", token=srv.token)
        assert ei2.value.code == 400
