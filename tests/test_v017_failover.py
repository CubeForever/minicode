"""v0.17：模型故障转移（FailoverProvider）与 cache 命中率展示。"""
import json

import pytest

from minicode import config as config_mod
from minicode.config import Config
from minicode.fake import FakeProvider
from minicode.llm import FailoverProvider, LLMError, make_provider
from minicode.session import Session


class RaisingProvider(FakeProvider):
    """stream 一被消费就抛 LLMError（未产出任何事件）——连接/限速耗尽的替身。"""

    def __init__(self, err="HTTP 503: overloaded", model="fake"):
        super().__init__([], model=model)
        self.err = err

    def stream(self, messages, tools, system, thinking=0):
        raise LLMError(self.err)
        yield  # pragma: no cover —— 使本函数成为生成器


class PartialThenRaiseProvider(FakeProvider):
    """先流出正文再报错——已产出事件、不允许跨模型重放的场景。"""

    def __init__(self):
        super().__init__([])

    def stream(self, messages, tools, system, thinking=0):
        yield {"type": "text_delta", "text": "部分"}
        raise LLMError("mid-stream failure")


def _failover(primary_model="primary-x", fb_model="backup-x", on_event=None):
    primary = RaisingProvider("HTTP 429: rate limited", model=primary_model)
    fb = FakeProvider([{"text": "备用回复"}], model=fb_model)
    return FailoverProvider(primary, [fb], on_event=on_event)


# ---------- 故障转移 ----------

def test_failover_switches_to_fallback_mid_stream():
    events = []
    fp = _failover(on_event=events.append)
    out = list(fp.stream([{"role": "user", "content": "hi"}], None, None))
    assert out[-1]["message"]["content"] == "备用回复"
    assert fp.active == 1                       # 已切换到备用
    assert len(events) == 1
    assert "primary-x" in events[0] and "backup-x" in events[0]


def test_failover_all_fail_raises_last_error():
    events = []
    fp = FailoverProvider(
        RaisingProvider("err-1", model="m1"),
        [RaisingProvider("err-2", model="m2")], on_event=events.append)
    with pytest.raises(LLMError, match="err-2"):
        list(fp.stream([], None, None))
    assert len(events) == 1                     # 只通知了第一次切换


def test_failover_never_replays_after_partial_output():
    fp = FailoverProvider(PartialThenRaiseProvider(),
                          [FakeProvider([{"text": "不应被调用"}])])
    with pytest.raises(LLMError, match="mid-stream"):
        list(fp.stream([], None, None))


def test_failover_delegates_model_switch_and_name():
    fp = _failover()
    assert fp.name == "fake+1备用"
    fp.active = 1
    fp.model = "user-choice"                    # /model 切换 → 主模型 + 回到主模型
    assert fp.primary.model == "user-choice"
    assert fp.active == 0 and fp.model == "user-choice"
    fp.reasoning_effort = "high"                # 属性委托到所有 provider
    if hasattr(fp.providers[0], "reasoning_effort"):
        assert fp.providers[0].reasoning_effort == "high"


def test_failover_stream_text_used_for_compaction():
    fp = FailoverProvider(RaisingProvider(),
                          [FakeProvider([{"text": "压缩摘要"}])])
    assert fp.stream_text([{"role": "user", "content": "长文"}], "sys") == "压缩摘要"


def test_make_provider_builds_failover_chain():
    cfg = Config(provider="openai", api_key="k", model="main-model",
                 fallbacks=[{"model": "backup-model"}, "third-model"])
    p = make_provider(cfg)
    assert isinstance(p, FailoverProvider)
    assert [x.model for x in p.providers] == ["main-model", "backup-model",
                                              "third-model"]
    assert p.name == "openai+2备用"
    assert p.model == "main-model"


def test_make_provider_without_fallbacks_stays_plain(monkeypatch):
    monkeypatch.delenv("MINICODE_FAKE_LLM", raising=False)
    p = make_provider(Config(provider="openai", api_key="k", model="m"))
    assert not isinstance(p, FailoverProvider)


# ---------- fallbacks 是受限配置 ----------

def test_project_fallbacks_require_trust(tmp_path, monkeypatch):
    """项目 .minicode.json 携带 fallbacks（可含 api_key）——必须过信任门禁。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(config_mod, "USER_CONFIG", tmp_path / "no-user.json")
    (tmp_path / ".minicode.json").write_text(json.dumps(
        {"fallbacks": [{"model": "evil-model", "api_key": "stolen"}]}),
        encoding="utf-8")
    cfg = config_mod.load_config(None)
    assert cfg is not None
    assert cfg.fallbacks == []                              # 不自动生效
    assert "fallbacks" in cfg.project_restricted
    config_mod.apply_restricted_config(cfg)
    assert cfg.fallbacks[0]["model"] == "evil-model"        # 信任确认后生效
    assert cfg.fallbacks[0]["api_key"] == "stolen"


def test_user_fallbacks_apply_directly(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(config_mod, "USER_CONFIG", tmp_path / "user.json")
    (tmp_path / "user.json").write_text(json.dumps(
        {"provider": "openai", "api_key": "k", "model": "m",
         "fallbacks": ["backup-a", {"model": "backup-b"}]}), encoding="utf-8")
    cfg = config_mod.load_config(None)
    assert [fb["model"] for fb in cfg.fallbacks] == ["backup-a", "backup-b"]
    p = make_provider(cfg)
    assert isinstance(p, FailoverProvider)
    assert len(p.providers) == 3


# ---------- cache 命中率 ----------

def test_cache_stats_hit_rate():
    s = Session()
    assert s.cache_stats() == {"cache_read": 0, "cache_creation": 0,
                               "hit_rate": 0.0}
    s.note_usage({"input": 1000, "output": 100, "cache_read": 800,
                  "cache_creation": 50})
    cs = s.cache_stats()
    assert cs["cache_read"] == 800
    assert cs["cache_creation"] == 50
    assert cs["hit_rate"] == pytest.approx(80.0)
