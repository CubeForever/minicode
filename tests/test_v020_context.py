"""v0.20 上下文经济学：分层 compact / Anthropic 递增缓存断点 / token 校准。

对应 v0.20 主题的三块改动：
1. 分层压缩——最近 N 个回合保留原文（thinking、tool_use/tool_result 配对
   无损），更早回合进 LLM 摘要 + 机械操作骨架；
2. Anthropic 对话消息递增 cache_control 断点（总量 ≤4 含 system/tools）；
3. 真实 usage 校准 chars/token 比率 + 按模型名推断上下文窗口。
"""
import json

import pytest

from minicode.agent import Agent
from minicode.config import Config, infer_context_limit
from minicode.fake import FakeProvider
from minicode.llm import _to_anthropic_messages, AnthropicProvider
from minicode.session import COMPACT_SYSTEM, Session
from minicode.tools import build_registry
from minicode.tools.shell import ShellState
from minicode.ui import UI


def make_agent(tmp_path, provider) -> Agent:
    cfg = Config(provider="fake", api_key="", model="fake", mode="yolo")
    cfg.cwd = tmp_path
    agent = Agent(provider, Session(), UI(), cfg,
                  build_registry(ShellState(tmp_path, "bash")))
    agent.system_prompt = "test"
    return agent


class StubProvider:
    """记录 stream_text 输入的桩 provider（校验喂给摘要器的转录内容）。"""
    name = "stub"

    def __init__(self, reply: str = "SUMMARY"):
        self.reply = reply
        self.prompts = []

    def stream_text(self, messages, system) -> str:
        self.prompts.append((messages[0]["content"], system))
        return self.reply


def _turn(i: int, with_tools: bool = True) -> list:
    msgs = [{"role": "user", "content": f"u{i}"}]
    if with_tools:
        msgs.append({"role": "assistant", "content": None,
                     "tool_calls": [{"id": f"c{i}", "name": "bash",
                                     "args": json.dumps({"command": f"echo {i}"})}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "name": "bash",
                     "content": f"out {i}", "is_error": False})
    else:
        msgs.append({"role": "assistant", "content": f"a{i}"})
    return msgs


# ---------- 1. 分层压缩 ----------

def test_layered_compact_keeps_recent_turns_verbatim():
    s = Session()
    for i in range(6):
        for m in _turn(i):
            s.add(m)
    stub = StubProvider("OLD SUMMARY")
    stats = s.compact(stub, keep_recent_turns=4)
    assert stats["before"] == 18
    assert stats["dropped_turns"] == 2 and stats["kept_turns"] == 4
    assert stats["after"] == 1 + 12

    pre = s.messages[0]
    assert pre.get("compact_preamble")
    assert "OLD SUMMARY" in pre["content"]
    # 操作骨架：被丢弃回合的工具调用仍可回溯
    assert "bash(command=echo 0)" in pre["content"] and "-> out 0" in pre["content"]

    kept = s.messages[1:]
    assert kept[0]["content"] == "u2" and kept[-1]["content"] == "out 5"
    # 配对完整性：保留部分里每个 tool_use 后紧跟对应 tool 结果
    for i, m in enumerate(kept):
        for tc in m.get("tool_calls") or []:
            nxt = kept[i + 1]
            assert nxt["role"] == "tool" and nxt["tool_call_id"] == tc["id"]
    # 只有一次 LLM 摘要调用，系统提示正确
    assert len(stub.prompts) == 1
    assert stub.prompts[0][1] == COMPACT_SYSTEM
    assert "u0" in stub.prompts[0][0] and "u2" not in stub.prompts[0][0]


def test_compact_without_old_turns_skips_llm():
    provider = FakeProvider([{"text": "SHOULD NOT BE USED"}])
    s = Session()
    for m in _turn(0):
        s.add(m)
    s.add({"role": "assistant", "content": "done"})
    stats = s.compact(provider)
    assert provider.i == 0          # 没有更早回合 → 不消费脚本（不调 LLM）
    assert stats["summary_chars"] == 0
    assert not any(m.get("compact_preamble") for m in s.messages)
    assert s.messages[0]["content"] == "u0"


def test_compact_small_history_with_carry_over():
    provider = FakeProvider([])
    s = Session()
    for m in _turn(0):
        s.add(m)
    s.compact(provider, carry_over="## 任务清单\n- x")
    assert provider.i == 0
    pre = s.messages[0]
    assert pre.get("compact_preamble") and "任务清单" in pre["content"]
    assert s.messages[1]["content"] == "u0"


def test_recompact_replaces_old_preamble():
    stub = StubProvider("SUM1")
    s = Session()
    for i in range(7):
        for m in _turn(i, with_tools=False):
            s.add(m)
    s.compact(stub, keep_recent_turns=4)
    assert s.messages[0].get("compact_preamble")

    stub2 = StubProvider("SUM2")
    for m in _turn(7, with_tools=False):
        s.add(m)
    s.compact(stub2, keep_recent_turns=4)
    # 旧 preamble 不重复；旧摘要内容并入再次总结的转录
    assert sum(1 for m in s.messages if m.get("compact_preamble")) == 1
    assert "SUM2" in s.messages[0]["content"]
    assert "SUM1" in stub2.prompts[0][0]


# ---------- 2. Anthropic 递增缓存断点 ----------

def _anthropic_turns(n: int) -> list:
    msgs = []
    for i in range(n):
        msgs.append({"role": "user", "content": f"u{i}"})
        msgs.append({"role": "assistant", "content": None,
                     "tool_calls": [{"id": f"c{i}", "name": "bash", "args": "{}"}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "name": "bash",
                     "content": "ok", "is_error": False})
    return msgs


def _marked(out: list) -> list:
    marks = []
    for i, m in enumerate(out):
        for b in m["content"]:
            if b.get("cache_control"):
                marks.append((i, b.get("type")))
    return marks


def test_cache_breakpoints_on_two_recent_turn_ends():
    out = _to_anthropic_messages(_anthropic_turns(4), cache_turn_breaks=True)
    # 4 个回合 → out 结构 12 条；断点钉在最近两个已完成回合（回合 1/2）
    # 的末尾 tool_result 上，即当前提示词 u3 之前的最后两条消息
    assert _marked(out) == [(5, "tool_result"), (8, "tool_result")]


def test_cache_breakpoints_stable_within_turn():
    base = _anthropic_turns(3)
    out1 = _to_anthropic_messages(base, cache_turn_breaks=True)
    grown = base + [{"role": "assistant", "content": None,
                     "tool_calls": [{"id": "cx", "name": "bash", "args": "{}"}]},
                    {"role": "tool", "tool_call_id": "cx", "name": "bash",
                     "content": "ok2", "is_error": False}]
    out2 = _to_anthropic_messages(grown, cache_turn_breaks=True)
    # 回合内消息增长，断点仍钉在同一内容上 → 后续调用前缀持续命中
    assert _marked(out1) == _marked(out2) == [(2, "tool_result"), (5, "tool_result")]
    assert out1[2]["content"][0]["tool_use_id"] == "c0"


def test_cache_breakpoints_total_le4():
    p = AnthropicProvider("claude-x", "key")
    tools = [{"name": "t", "description": "d",
              "input_schema": {"type": "object", "properties": {}}}]
    body = p._build_body(_anthropic_turns(4), tools, "system prompt")
    total = 0
    total += sum(1 for b in body["system"] if b.get("cache_control"))
    total += sum(1 for t in body["tools"] if t.get("cache_control"))
    for m in body["messages"]:
        total += sum(1 for b in m["content"] if b.get("cache_control"))
    assert total == 4   # system + tools + 两个回合边界


def test_cache_breakpoints_without_tools_or_system():
    out = _to_anthropic_messages(_anthropic_turns(3), cache_turn_breaks=False)
    assert _marked(out) == []
    body = AnthropicProvider("claude-x", "key")._build_body(
        _anthropic_turns(3), None, None)
    total = sum(1 for m in body["messages"]
                for b in m["content"] if b.get("cache_control"))
    assert total == 2   # 只有对话断点（压缩调用路径 tools/system 皆空）


# ---------- 3. token 校准与窗口推断 ----------

def test_calibrate_usage_converges():
    s = Session()
    assert s.approx_tokens() == 0
    s.add({"role": "user", "content": "x" * 3000})
    assert s.approx_tokens() == 1000        # 默认比率 3.0
    s.calibrate_usage(3000, {"input": 2000})   # 真实比率 1.5（如中文会话）
    assert s.chars_per_token == pytest.approx(2.4)   # 3.0*0.6 + 1.5*0.4
    assert 1240 <= s.approx_tokens() <= 1260
    s.calibrate_usage(0, {"input": 100})     # 无效输入跳过
    s.calibrate_usage(3000, None)
    s.calibrate_usage(3000, {"input": 0})
    assert s.chars_per_token == pytest.approx(2.4)
    s.calibrate_usage(3000, {"input": 1})    # 异常比率夹到 8.0
    assert s.chars_per_token == pytest.approx(4.64)   # 2.4*0.6 + 8.0*0.4


def test_chars_per_token_persists(tmp_path):
    s = Session()
    s.calibrate_usage(3000, {"input": 2000})
    p = tmp_path / "s.json"
    s.save(p)
    assert Session.load(p).chars_per_token == s.chars_per_token


def test_turns_remaining():
    s = Session()
    assert s.turns_remaining(100_000) is None       # 无数据
    s.note_usage({"input": 10_000, "output": 100})
    s.end_turn()
    assert s.turns_remaining(100_000) is None       # 只有一个回合，无增长
    s.note_usage({"input": 12_000, "output": 100})
    s.end_turn()
    assert abs(s.turns_remaining(100_000) - 39.0) < 0.1   # (90k-12k)/2k
    s.note_usage({"input": 95_000, "output": 100})
    s.end_turn()
    assert s.turns_remaining(100_000) == 0.0
    assert s.turns_remaining(0) is None


def test_infer_context_limit():
    assert infer_context_limit("glm-4.6", 1_000_000) == 200_000
    assert infer_context_limit("GLM-5-Air", 1) == 200_000
    assert infer_context_limit("glm-4-flash", 99) == 128_000
    assert infer_context_limit("deepseek-chat", 99) == 128_000
    assert infer_context_limit("kimi-k2", 99) == 256_000
    assert infer_context_limit("qwen3-max", 99) == 262_144
    assert infer_context_limit("qwen2.5-72b", 99) == 131_072
    assert infer_context_limit("claude-sonnet-4-5", 99) == 200_000
    assert infer_context_limit("gpt-4.1", 99) == 1_000_000
    assert infer_context_limit("totally-unknown-model", 777) == 777


def test_load_config_infers_context_limit(tmp_path, monkeypatch):
    from minicode import config as config_mod
    monkeypatch.setattr(config_mod, "USER_CONFIG", tmp_path / "user.json")
    monkeypatch.setattr(config_mod, "PROJECT_CONFIG", tmp_path / "proj.json")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.setenv("MINICODE_FAKE_LLM", "demo")
    (tmp_path / "user.json").write_text(json.dumps({
        "provider": "openai", "api_key": "k", "model": "glm-4.6"}))
    cfg = config_mod.load_config(type("A", (), {"profile": None, "model": None,
                                                "yolo": False,
                                                "append_system_prompt": ""})())
    assert cfg.context_limit == 200_000      # 按模型推断，而非默认 1M
    # 显式配置优先
    (tmp_path / "user.json").write_text(json.dumps({
        "provider": "openai", "api_key": "k", "model": "glm-4.6",
        "context_limit": 555_000}))
    cfg = config_mod.load_config(type("A", (), {"profile": None, "model": None,
                                                "yolo": False,
                                                "append_system_prompt": ""})())
    assert cfg.context_limit == 555_000


def test_agent_calibrates_during_turn(tmp_path):
    """一回合内每次模型调用都用真实 usage 校准估算比率。"""
    script = [
        {"tool_calls": [{"id": "t1", "name": "bash",
                         "args": json.dumps({"command": "echo hi"})}]},
        {"text": "done"},
    ]
    agent = make_agent(tmp_path, FakeProvider(script))
    agent.run_turn("run it")
    assert agent.session.chars_per_token != 3.0   # 已被 usage 校准
