"""测试全局夹具：把会话落盘重定向到临时目录。

此前 save_auto 会写进真实的 ~/.minicode/sessions/（每个用例留下一份会话
文件），跑一次全量测试就在用户目录积累几十份垃圾会话。autouse 夹具统一
隔离，单个用例仍可自行覆盖 SESSIONS_DIR。
"""
import pytest

from minicode import session as session_mod


@pytest.fixture(autouse=True)
def _isolated_sessions(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    monkeypatch.setattr(session_mod, "SESSIONS_DIR", sessions)
    return sessions
