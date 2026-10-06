"""v0.25 harness 硬化:门三态/FAIL 归因/frontmatter 不变量/退避重试。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import report_meta  # noqa: E402
from minicode.blueteam import (  # noqa: E402
    independent_verify, render_independent_verify,
    verify_commands_from_output)


def _gate(tmp_path, commands):
    text = "\n".join(f"- 自验: `{c}`" for c in commands)
    return independent_verify(text, tmp_path)


def test_three_state_pass_fail(tmp_path):
    ok_cmd = f'"{sys.executable}" -c "print(1)"'
    bad_cmd = f'"{sys.executable}" -c "print(\'boom\');raise SystemExit(3)"'
    res, env_ok = _gate(tmp_path, [ok_cmd, bad_cmd])
    assert env_ok is True
    assert res[0][1] == "PASS" and res[1][1] == "FAIL"
    rendered = render_independent_verify(res, env_ok)
    assert "[PASS]" in rendered and "[FAIL]" in rendered
    assert "自述与实测不符" in rendered


def test_error_state_is_not_fail(tmp_path, monkeypatch):
    """命令没跑起来(超时/OSError)= ERROR,与'跑了但结果不对'分离。"""
    import minicode.blueteam as bt
    import subprocess as sp

    def slow(cmd, **kw):
        raise sp.TimeoutExpired(cmd, kw.get("timeout", 1))

    # 探针单独放行,这样 subprocess.run 的每次调用都是"命令执行"本身
    monkeypatch.setattr(bt, "probe_environment",
                        lambda cwd, timeout=30: (True, ""))
    monkeypatch.setattr(bt.subprocess, "run", slow)
    res, env_ok = _gate(tmp_path, ["slow cmd"])
    assert env_ok is True and res[0][1] == "ERROR" and "超时" in res[0][2]

    def no_launch(cmd, **kw):
        raise OSError("spawn failed")

    monkeypatch.setattr(bt.subprocess, "run", no_launch)
    res2, _ = _gate(tmp_path, ["any cmd"])
    assert res2[0][1] == "ERROR" and "无法启动" in res2[0][2]
    # ERROR 不计入 FAIL,渲染区分展示
    rendered = render_independent_verify(res, True)
    assert "ERROR 1" in rendered and "FAIL 0" in rendered


def test_fail_attribution(tmp_path):
    val = f'"{sys.executable}" -c "raise ValueError(\'k 越界\')"'
    typ = f'"{sys.executable}" -c "print(未定义名字)"'
    res, env_ok = _gate(tmp_path, [val, typ])
    assert env_ok is True
    # ValueError 与红队发现同类 → 蓝队未修复
    assert res[0][1] == "FAIL" and "蓝队未修复" in res[0][2]
    assert "ValueError" in res[0][2]            # traceback 末行被抽取
    assert "exit=" in res[0][2]                 # 退出码在诊断里
    # NameError → 自验命令本身写错
    assert res[1][1] == "FAIL" and "自验命令本身写错" in res[1][2]


def test_attribution_environment(tmp_path):
    cmd = "definitely_not_a_real_command_xyz"
    res, env_ok = _gate(tmp_path, [cmd])
    # shell 找不到命令 → ERROR(命令没跑起来),退出码 9009(Windows)/127(POSIX)
    assert res[0][1] == "ERROR"
    assert "命令未启动" in res[0][2]


def test_verify_commands_extraction():
    out = ('修复完成。\n- 自验: `python -m pytest -q`\n'
           '- 自验: `python util_check.py`\n不是自验的: `ls`\n')
    assert verify_commands_from_output(out) == ["python -m pytest -q",
                                                "python util_check.py"]
    assert verify_commands_from_output("无自验") == []


# ---------- 不变量 1:frontmatter 由 harness 独占写入 ----------

def test_frontmatter_roundtrip_and_missing():
    fm = report_meta.frontmatter({"task": "t1", "mode": "findings",
                                  "data_validity": "valid",
                                  "verify_passed": 5, "verify_total": 5})
    doc = fm + "## 正文\n内容"
    parsed = report_meta.parse_frontmatter(doc)
    assert parsed["task"] == "t1" and parsed["verify_total"] == "5"
    assert report_meta.data_validity(doc) == "valid"
    # 缺失/不可解析 → 视同作废(不变量:缺失即作废)
    assert report_meta.data_validity("无 frontmatter 的报告") == "void:missing"
    assert report_meta.data_validity("---\nbroken\n---\nx") == "void:missing"


def test_data_validity_enum(tmp_path):
    cases = {
        "valid": "valid",
        "void:timeout": "void:timeout",
        "void:env_unavailable": "void:env_unavailable",
        "void:no_selfverify": "void:no_selfverify",
        "void:pre_invariant": "void:pre_invariant",
    }
    for v, _ in cases.items():
        p = tmp_path / f"{v.replace(':', '_')}.md"
        p.write_text(report_meta.frontmatter(
            {"task": "t", "data_validity": v}) + "正文", encoding="utf-8")
        assert report_meta.data_validity(p.read_text(encoding="utf-8")) == v


def test_run_headless_retries_once_on_timeout(tmp_path, monkeypatch):
    calls = {"n": 0}
    real = report_meta.subprocess.run

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise report_meta.subprocess.TimeoutExpired("cmd", kw["timeout"])
        return real([sys.executable, "-c", "print(1)"],
                    capture_output=True, timeout=30)

    monkeypatch.setattr(report_meta.subprocess, "run", flaky)
    proc, _elapsed, budget, timed_out = report_meta.run_headless(
        "x", tmp_path, timeout=5, env={})
    assert calls["n"] == 2 and timed_out is False and budget == 10  # 2× 退避


def test_run_headless_gives_up_at_cap(tmp_path, monkeypatch):
    calls = {"n": 0}

    def always_timeout(*a, **k):
        calls["n"] += 1
        raise report_meta.subprocess.TimeoutExpired("cmd", k["timeout"])

    monkeypatch.setattr(report_meta.subprocess, "run", always_timeout)
    _p, _e, budget, timed_out = report_meta.run_headless(
        "x", tmp_path, timeout=400, env={}, max_budget=600)
    assert timed_out is True
    assert budget == 600 and calls["n"] == 2   # 400 → 600(上限),只重试一次


# ---------- v0.25.2:verify_verdict(修复质量判定,与链路可信性正交) ----------

def test_verify_verdict_truth_table(tmp_path):
    ok = f'"{sys.executable}" -c "print(1)"'
    val_fail = f'"{sys.executable}" -c "raise ValueError(0)"'
    syn_fail = f'"{sys.executable}" -c "print(未定义)"'
    # 全 PASS → verified
    res, _ = _gate(tmp_path, [ok, ok])
    assert report_meta is not None
    from minicode.blueteam import verify_verdict
    assert verify_verdict(res) == "verified"
    # 全 FAIL 且归因均"自验写错" → unverified(修复质量未知)
    res2, _ = _gate(tmp_path, [syn_fail, syn_fail])
    assert verify_verdict(res2) == "unverified"
    # 全 FAIL 且有"蓝队未修复"归因 → failed(修复确认无效)
    res3, _ = _gate(tmp_path, [val_fail, val_fail])
    assert verify_verdict(res3) == "failed"
    # 混合 → mixed
    res4, _ = _gate(tmp_path, [ok, val_fail])
    assert verify_verdict(res4) == "mixed"
    # 空 → none
    assert verify_verdict([]) == "none"


# ---------- v0.25.3:data_audit 按日期分桶 + verdict 口径 ----------

def test_data_audit_summarize_buckets_and_verdict():
    from data_audit import summarize
    now = __import__("time").time()
    rows = [
        # 近 7 天蓝队 verified
        {"dir": "eval/bluefix", "validity": "valid", "verify_verdict": "verified",
         "mtime": now, "kind": "bluefix-ab"},
        # 近 7 天蓝队 valid 但缺 verdict → verdict_missing 计 1
        {"dir": "eval/bluefix", "validity": "valid", "verify_verdict": "",
         "mtime": now - 3 * 86400, "kind": "bluefix-ab"},
        # 红队报告无 verdict → 不计入 verdict_missing
        {"dir": "eval/redteam", "validity": "valid", "verify_verdict": "",
         "mtime": now, "kind": "redteam-fresh"},
        # 30 天前的蓝队报告 → older 桶
        {"dir": "eval/bluefix", "validity": "valid", "verify_verdict": "mixed",
         "mtime": now - 30 * 86400, "kind": "bluefix-ab"},
        # 存量 → pre_invariant 桶(无论多新)
        {"dir": "eval/bluefix", "validity": "void:pre_invariant",
         "verify_verdict": "", "mtime": now, "kind": "legacy"},
    ]
    s = summarize(rows)
    assert s["by_date"]["recent_7d"] == {"total": 3, "valid": 3, "valid_rate": 100.0}
    assert s["by_date"]["older"]["total"] == 1
    assert s["by_date"]["pre_invariant"]["total"] == 1
    assert s["by_verdict"] == {"verified": 1, "mixed": 1}
    assert s["verdict_missing"] == 1   # 红队报告不计入
