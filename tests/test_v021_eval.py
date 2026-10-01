"""v0.21 eval 回路：runner 端到端（离线，脚本模型）+ 任务装载。"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def eval_env(tmp_path, monkeypatch):
    """隔离 eval/tasks + eval/checks 到临时目录的夹具。"""
    tasks = tmp_path / "tasks"
    checks = tmp_path / "checks"
    tasks.mkdir()
    checks.mkdir()
    return tasks, checks


def _fake_llm(tmp_path, monkeypatch):
    """脚本模型：一次 write_file 创建 hello.txt 后结束。"""
    fake = tmp_path / "fake.json"
    fake.write_text(json.dumps([
        {"tool_calls": [{"id": "c1", "name": "write_file",
                         "args": json.dumps({"path": "hello.txt",
                                             "content": "ok"})}]},
        {"text": "done"}]), encoding="utf-8")
    monkeypatch.setenv("MINICODE_FAKE_LLM", str(fake))


def test_run_eval_end_to_end(tmp_path, eval_env, monkeypatch):
    tasks, checks = eval_env
    _fake_llm(tmp_path, monkeypatch)
    (tasks / "demo-create.json").write_text(json.dumps({
        "id": "demo-create", "category": "demo",
        "instruction": "创建 hello.txt，内容为 ok",
        "setup": {"files": {}},
        "check": {"command": "{python} \"{check}\" \"{sandbox}\""},
    }, ensure_ascii=False), encoding="utf-8")
    (checks / "demo-create.py").write_text(
        "import sys\nfrom pathlib import Path\n"
        "sb = Path(sys.argv[1])\n"
        "assert (sb / 'hello.txt').read_text(encoding='utf-8').strip() == 'ok'\n"
        "print('OK')\n", encoding="utf-8")

    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "run_eval.py"),
         "--tasks", str(tasks), "--out", str(tmp_path / "out")],
        capture_output=True, text=True, timeout=180,
        env={**os.environ, "PYTHONUTF8": "1"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "✓" in r.stdout and "1/1" in r.stdout

    report = json.loads(sorted((tmp_path / "out").glob("eval-*.json"))[0]
                        .read_text(encoding="utf-8"))
    assert report["results"][0]["ok"] is True
    assert report["pass_rate"] == 100.0


def test_run_eval_failure_recorded(tmp_path, eval_env, monkeypatch):
    tasks, checks = eval_env
    _fake_llm(tmp_path, monkeypatch)
    (tasks / "demo-miss.json").write_text(json.dumps({
        "id": "demo-miss", "category": "demo",
        "instruction": "创建 hello.txt（脚本模型只会写 ok，校验期待别的值 → 必败）",
        "setup": {"files": {}},
        "check": {"command": "{python} \"{check}\" \"{sandbox}\""},
    }, ensure_ascii=False), encoding="utf-8")
    (checks / "demo-miss.py").write_text(
        "import sys\nfrom pathlib import Path\n"
        "sb = Path(sys.argv[1])\n"
        "assert (sb / 'hello.txt').read_text(encoding='utf-8').strip() == 'never-written'\n"
        "print('OK')\n", encoding="utf-8")

    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "run_eval.py"),
         "--tasks", str(tasks), "--out", str(tmp_path / "out")],
        capture_output=True, text=True, timeout=180,
        env={**os.environ, "PYTHONUTF8": "1"})
    assert r.returncode == 0          # 任务失败不炸 runner（eval 是度量非门禁）
    assert "✗" in r.stdout and "0/1" in r.stdout
    report = json.loads(sorted((tmp_path / "out").glob("eval-*.json"))[0]
                        .read_text(encoding="utf-8"))
    assert report["results"][0]["ok"] is False
    assert "check failed" in report["results"][0]["error"]


def test_load_tasks_skips_broken(tmp_path):
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import run_eval
    finally:
        sys.path.remove(str(ROOT / "scripts"))
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    (tasks / "bad.json").write_text("{broken", encoding="utf-8")
    (tasks / "good.json").write_text(json.dumps({"id": "good"}),
                                     encoding="utf-8")
    loaded = run_eval.load_tasks(tasks)
    assert [t["id"] for t in loaded] == ["good"]
