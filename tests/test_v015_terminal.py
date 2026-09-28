"""v0.15：终端 Markdown 渲染、持久 shell 环境、cache token、思考关键词
落地、子智能体非交互、测试隔离。"""
import base64
import json
import shutil

import pytest

from minicode import session as session_mod
from minicode.llm import AnthropicProvider, OpenAIProvider
from minicode.session import Session
from minicode.tools.shell import BashTool, ShellState
from minicode.ui import SubUI, StreamRenderer, UI, md_inline
from minicode.config import Config


def render(text: str) -> str:
    out = []
    r = StreamRenderer(out.append)
    r.feed(text)
    r.flush()
    return "".join(out)


# ---------- 终端 Markdown 渲染 ----------

def test_renderer_verbatim_in_plain_mode():
    """无色模式下无 markdown 特征的文本逐字保留（回归既有测试语义）。"""
    assert render("```py\nx = 1\n```\ndone") == "```py\nx = 1\n```\ndone"


def test_renderer_headings_lists_quotes():
    out = render("# 标题一\n## 标题二\n- 项目甲\n- [ ] 待办\n- [x] 完成\n> 引用文\n---\n")
    assert "# 标题一" not in out and "标题一" in out          # 标记被结构化
    assert "• 项目甲" in out
    assert "✓ 完成" in out and "○ 待办" in out
    assert "│ 引用文" in out
    assert "──" in out


def test_renderer_inline_stripped_without_color():
    out = render("这是 **加粗** 与 `代码` 与 [链接](https://x.dev) 文本\n")
    assert "**" not in out and "`" not in out
    assert "加粗" in out and "代码" in out
    assert "链接 (https://x.dev)" in out


def test_renderer_table_aligned():
    out = render("| 名称 | 数量 |\n| --- | --- |\n| 苹果 | 3 |\n| 香蕉 | 12 |\n之后\n")
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert "名称" in lines[0] and "数量" in lines[0]
    # 数据行按列对齐：分隔线与行宽一致
    sep = next(ln for ln in lines if "┼" in ln)
    assert "─" in sep
    assert any("苹果" in ln for ln in lines[2:])
    assert "之后" in lines[-1]


def test_renderer_pipe_prose_not_table():
    out = render("a | b 不是表格\n下一行\n")
    assert "a | b 不是表格" in out


def test_renderer_table_flushed_at_stream_end():
    out = render("| a | b |\n| --- | --- |\n| 1 | 2 |")
    assert "a" in out and "┼" in out


def test_renderer_fenced_code_never_markdownified():
    out = render("```\n# 不是标题\n**不是粗体**\n```\n")
    assert "# 不是标题" in out and "**不是粗体**" in out


def test_md_inline_with_color(monkeypatch):
    from minicode import ui as ui_mod
    monkeypatch.setattr(ui_mod, "USE_COLOR", True)
    s = md_inline("纯文本")
    assert s == "纯文本"
    assert "\x1b[1m加粗\x1b[0m" in md_inline("**加粗**")
    assert "\x1b[36m" in md_inline("`code`")


# ---------- 持久 shell 环境 ----------

def test_shellstate_parse_env_excludes_noise():
    dump = "HOME=/home/u\x00PWD=/tmp\x00SHLVL=2\x00MY_VAR=hello world\x00"
    env = ShellState._parse_env(dump)
    assert env == {"HOME": "/home/u", "MY_VAR": "hello world"}


def test_shellstate_env_diff_and_prefix():
    st = ShellState.__new__(ShellState)   # 跳过 __init__（不抓真实基线）
    st.shell = "bash"
    st.cwd = __import__("pathlib").Path(".")
    st.background = {}
    st._bg_counter = 0
    st._env_baseline = {"A": "1", "B": "2", "GONE": "x"}
    st._env_overrides = {}
    st._env_unsets = set()
    raw = base64.b64encode("A=9\x00NEW=yes\x00B=2\x00".encode()).decode()
    st.update_env(raw)
    assert st._env_overrides == {"A": "9", "NEW": "yes"}
    assert st._env_unsets == {"GONE"}
    prefix = st.env_export_prefix()
    assert "unset GONE;" in prefix
    # 值经 base64 传输：脚本内只出现安全字符，解码后还原原值
    seg_a = next(p for p in prefix.split("; ") if p.startswith("export A="))
    enc = seg_a.split("'")[1]
    assert base64.b64decode(enc).decode() == "9"
    assert seg_a.startswith("export A=$(printf %s '") \
        and seg_a.endswith("' | base64 -d)")


def test_shellstate_ignores_empty_dump():
    st = ShellState.__new__(ShellState)
    st.shell = "bash"
    st.cwd = __import__("pathlib").Path(".")
    st.background = {}
    st._bg_counter = 0
    st._env_baseline = {"A": "1"}
    st._env_overrides = {}
    st._env_unsets = set()
    st.update_env("")          # env -0 失败 → 不可信，不把全部变量当 unset
    assert st._env_overrides == {} and st._env_unsets == set()


@pytest.mark.skipif(not shutil.which("bash"), reason="需要 bash")
def test_bash_env_persists_across_calls(tmp_path):
    """export → 下一条命令仍然可见（此前 export 不跨调用存活）。"""
    from minicode.tools.base import ToolContext
    st = ShellState(tmp_path, "bash")
    tool = BashTool(st)
    ctx = ToolContext(cwd=tmp_path, config=Config(), session=Session(), ui=UI())
    tool.run({"command": "export MINICODE_V015=persisted"}, ctx)
    out = tool.run({"command": "echo value=$MINICODE_V015"}, ctx)
    assert "value=persisted" in out


# ---------- cache token 与思考关键词 ----------

class FakeResp:
    def __init__(self, chunks):
        self.chunks = chunks

    def __iter__(self):
        return iter(self.chunks)

    def close(self):
        pass


def test_openai_usage_parses_cached_tokens():
    chunks = [
        b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n',
        b'data: {"choices":[],"usage":{"prompt_tokens":100,"completion_tokens":5,'
        b'"prompt_tokens_details":{"cached_tokens":64}}}\n\n',
        b'data: [DONE]\n\n',
    ]
    p = OpenAIProvider("m", "k")
    finish = list(p._iter_stream(FakeResp(chunks)))[-1]
    assert finish["usage"] == {"input": 100, "output": 5, "cache_read": 64}


def test_anthropic_usage_parses_cache_fields():
    chunks = [
        ("data: " + json.dumps({"type": "message_start",
                                "message": {"usage": {"input_tokens": 20,
                                                      "cache_read_input_tokens": 130,
                                                      "cache_creation_input_tokens": 50}}})
         + "\n\n").encode(),
        ("data: " + json.dumps({"type": "content_block_start", "index": 0,
                                "content_block": {"type": "text"}}) + "\n\n").encode(),
        ("data: " + json.dumps({"type": "content_block_delta", "index": 0,
                                "delta": {"type": "text_delta", "text": "ok"}})
         + "\n\n").encode(),
        ("data: " + json.dumps({"type": "message_delta",
                                "delta": {"stop_reason": "end_turn"},
                                "usage": {"output_tokens": 4}}) + "\n\n").encode(),
    ]
    p = AnthropicProvider("m", "k")
    finish = list(p._iter_stream(FakeResp(chunks)))[-1]
    # cache 计入上下文占用（input 总量），同时单列供费用展示
    assert finish["usage"]["input"] == 200
    assert finish["usage"]["cache_read"] == 130
    assert finish["usage"]["cache_creation"] == 50


def test_session_usage_accumulates_cache():
    s = Session()
    s.note_usage({"input": 100, "output": 10, "cache_read": 80})
    s.note_usage({"input": 50, "output": 5, "cache_read": 40,
                  "cache_creation": 20})
    assert s.total_usage["cache_read"] == 120
    assert s.total_usage["cache_creation"] == 20
    assert s.total_usage["input"] == 150


def test_thinking_keyword_maps_to_effort():
    p = OpenAIProvider("m", "k")
    body = p._build_body([{"role": "user", "content": "ultrathink"}], None, None,
                         thinking=31999)
    assert body["reasoning_effort"] == "high"
    body = p._build_body([{"role": "user", "content": "think"}], None, None,
                         thinking=4000)
    assert body["reasoning_effort"] == "medium"
    # 用户显式设置优先：effort=low 且关键词预算不高 → 保持 low
    p2 = OpenAIProvider("m", "k", reasoning_effort="low")
    body = p2._build_body([{"role": "user", "content": "think"}], None, None,
                          thinking=4000)
    assert body["reasoning_effort"] == "low"
    # 关键词强于显式档位 → 升档
    body = p2._build_body([{"role": "user", "content": "ultrathink"}], None, None,
                          thinking=31999)
    assert body["reasoning_effort"] == "high"


def test_anthropic_non_stream_events():
    data = {"content": [
        {"type": "thinking", "thinking": "考虑中", "signature": "sig"},
        {"type": "text", "text": "答案"},
        {"type": "tool_use", "id": "t1", "name": "bash",
         "input": {"command": "ls"}},
    ], "stop_reason": "tool_use",
        "usage": {"input_tokens": 10, "output_tokens": 3,
                  "cache_read_input_tokens": 7}}
    p = AnthropicProvider("m", "k")
    events = list(p._events_from_json(data))
    kinds = [e["type"] for e in events]
    assert kinds == ["reasoning_delta", "text_delta", "tool_call",
                     "tool_call_delta", "finish"]
    finish = events[-1]
    assert finish["usage"]["input"] == 17      # cache_read 并入上下文占用
    assert finish["message"]["tool_calls"][0]["args"] == '{"command": "ls"}'


# ---------- 子智能体非交互 ----------

def test_subui_never_prompts():
    parent = UI()
    sub = SubUI(parent)
    assert sub.confirm("允许写入？") == "n"
    assert sub.choose("选择", [{"label": "A"}]) == []


# ---------- 测试隔离（conftest） ----------

def test_session_save_goes_to_tmp_dir(tmp_path):
    """conftest autouse 夹具把 SESSIONS_DIR 指到临时目录——本测试若污染
    真实 ~/.minicode/sessions 即失败。"""
    s = Session()
    s.add({"role": "user", "content": "隔离验证"})
    s.save_auto("隔离验证")
    files = list(session_mod.SESSIONS_DIR.glob("*.json"))
    assert files, "会话应写入被重定向的临时目录"
    assert "minicode" not in str(files[0].parent.parent).lower() or \
        "pytest" in str(files[0]).lower()
