"""一致性检查：文档声明 vs 代码事实。

每次推送（CI）与每次版本更新后都必须运行，任何一项漂移即退出码 1：

    python scripts/check_consistency.py

核对项：
  1. 版本号四处一致（__init__ / pyproject / CHANGELOG 最新条目 / README 当前状态）
  2. CHANGELOG 版本头无重复、按时间倒序
  3. 测试数：pytest 收集数 == README 两处声明
  4. 内置工具数：build_registry 实数 == README / CONTRIBUTING 声明
  5. 终端斜杠命令：_command 分发器实集 ∪ BUILTIN == README 清单；且 /help 有条目
  6. Web 斜杠命令：webui 分发器实数 == README「N 条」；app.js COMMANDS 与分发器一致
  7. README 架构图覆盖 minicode/ 全部顶层模块
  8. CONTRIBUTING 行数声明与实际相差 ≤ 300 行
  9. .gitignore 覆盖 dist/ build/ egg-info/ .minicode.json
"""
from __future__ import annotations

import io
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FAILURES: list = []


def check(name: str, ok: bool, detail: str = ""):
    mark = "✓" if ok else "✗"
    print(f"  [{mark}] {name}" + (f" —— {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def read(path: str) -> str:
    return io.open(ROOT / path, encoding="utf-8").read()


# ---------- 1/2. 版本号与 CHANGELOG ----------
version = re.search(r'__version__ = "([^"]+)"',
                    read("minicode/__init__.py")).group(1)
pyproject_v = re.search(r'^version = "([^"]+)"', read("pyproject.toml"), re.M).group(1)
check("版本号 __init__ == pyproject", version == pyproject_v, f"{version} vs {pyproject_v}")

changelog = read("CHANGELOG.md")
headers = re.findall(r"^## (\d+\.\d+\.\d+)", changelog, re.M)
dups = {h for h in headers if headers.count(h) > 1}
check("CHANGELOG 版本头无重复", not dups, f"重复: {sorted(dups)}")
check("CHANGELOG 最新条目 == 当前版本", headers[0] == version,
      f"{headers[0]} vs {version}")
dates = re.findall(r"^## \d+\.\d+\.\d+ \(([\d-]+)\)", changelog, re.M)
check("CHANGELOG 按时间倒序", dates == sorted(dates, reverse=True), str(dates[:4]))

readme = read("README.md")
status_line = next((ln for ln in readme.splitlines() if "当前状态" in ln), "")
check("README 当前状态含当前版本", f"v{version} ·" in status_line, status_line[:80])

# ---------- 3. 测试数 ----------
res = subprocess.run([sys.executable, "-m", "pytest", "tests", "--collect-only", "-q"],
                     capture_output=True, text=True, cwd=str(ROOT), timeout=300)
m = re.search(r"(\d+) tests collected", res.stdout)
test_count = int(m.group(1)) if m else -1
check("pytest 收集成功", test_count > 0, res.stdout[-200:] + res.stderr[-200:])
check("README 测试数（当前状态）", f"{test_count} 项自动化测试" in readme,
      f"实际 {test_count}")
check("README 测试数（测试小节）", f"# {test_count} 项" in readme, f"实际 {test_count}")

# ---------- 4. 工具数 ----------
sys.path.insert(0, str(ROOT))
from minicode.tools import build_registry              # noqa: E402
from minicode.tools.shell import ShellState            # noqa: E402
tool_count = len(build_registry(ShellState(ROOT, "bash")).tools)
m = re.search(r"(\d+) 个内置工具", readme)
check("README 工具数", m and int(m.group(1)) == tool_count, f"实际 {tool_count}")
contrib = read("CONTRIBUTING.md")
m = re.search(r"共 (\d+) 个", contrib)
check("CONTRIBUTING 工具数", m and int(m.group(1)) == tool_count, f"实际 {tool_count}")

# ---------- 5. 终端斜杠命令 ----------
cli_src = read("minicode/cli.py")
chain_txt = cli_src[cli_src.index("def _command"):cli_src.index("def _run_turn_queued")]
chain = {c.lstrip("/") for c in
         re.findall(r'name == "(/[\w-]+)"', chain_txt)}
chain |= {c.lstrip("/") for c in
          re.findall(r'name in \("(/[\w-]+)"', chain_txt)}       # /exit 等
builtin = set(re.findall(r'^\s{4}"(\w[\w-]*)": \(', cli_src, re.M))  # BUILTIN_COMMANDS
readme_slash = re.search(r"^`(/help)` .*`(/exit)`$", readme, re.M)
readme_list = {c.lstrip("/") for c in
               re.findall(r"`(/[\w-]+)`", readme_slash.group(0))} if readme_slash else set()
check("README 斜杠清单 == 终端命令实集", readme_list == chain | builtin,
      f"README 多: {sorted(readme_list - (chain | builtin))} / "
      f"README 缺: {sorted((chain | builtin) - readme_list)}")
help_txt = cli_src[cli_src.index("HELP_SECTIONS = ["):cli_src.index("def _disp_width")]
help_names = set(re.findall(r'\("/([\w-]+)', help_txt))
missing_help = (chain | builtin) - help_names - {"review"}
check("终端 /help 覆盖全部命令", not missing_help, f"缺条目: {sorted(missing_help)}")

# ---------- 6. Web 斜杠命令 ----------
webui_src = read("minicode/webui.py")
disp_txt = webui_src[webui_src.index("def _dispatch_command"):
                     webui_src.index("def _native_pick")]
web_cmds = set(re.findall(r'name == "([\w-]+)"', disp_txt))
web_cmds |= set(re.findall(r'name in \(([^)]*)\)', disp_txt) and
                {n.strip().strip('"') for tup in
                 re.findall(r'name in \(([^)]*)\)', disp_txt)
                 for n in tup.split(",")} - {"nt"})
m = re.search(r"/命令`（(\d+) 条）", readme)
check("README Web 命令数", m and int(m.group(1)) == len(web_cmds),
      f"实际 {len(web_cmds)}")
appjs = read("minicode/web/app.js")
js_cmds = set(re.findall(r'"(/[\w-]+)"',
              appjs[appjs.index("const COMMANDS = ["):appjs.index("];",
                                                          appjs.index("const COMMANDS = ["))]))
js_cmds = {c.lstrip("/") for c in js_cmds}
check("app.js COMMANDS == Web 分发器", js_cmds == web_cmds,
      f"app.js 多: {sorted(js_cmds - web_cmds)} / app.js 缺: {sorted(web_cmds - js_cmds)}")

# ---------- 7. README 架构图覆盖 ----------
tree = re.search(r"```text\n(.*?)```", readme, re.S).group(1)
missing_mods = [p.name for p in (ROOT / "minicode").glob("*.py")
                if p.name not in tree and not p.name.startswith("__")]
check("README 架构图覆盖全部顶层模块", not missing_mods, f"缺: {missing_mods}")

# ---------- 8. CONTRIBUTING 行数声明 ----------
core_lines = sum(1 for p in (ROOT / "minicode").rglob("*.py") for _ in p.open())
m = re.search(r"核心约 (\d+) 行", contrib)
check("CONTRIBUTING 行数声明（±300）",
      m and abs(int(m.group(1)) - core_lines) <= 300,
      f"实际 {core_lines}")
m = re.search(r"核心约 (\d+) 行", readme)
check("README 行数声明（±300）", m and abs(int(m.group(1)) - core_lines) <= 300,
      f"实际 {core_lines}")

# ---------- 9. .gitignore ----------
ignore = read(".gitignore")
for key in ("dist/", "build/", "egg-info/", "minicode.json"):
    check(f".gitignore 覆盖 {key}", key in ignore)

# ---------- 结果 ----------
print()
if FAILURES:
    print(f"一致性检查：{len(FAILURES)} 项漂移")
    sys.exit(1)
print(f"一致性检查：全部通过（版本 {version} · 测试 {test_count} · 工具 {tool_count} · "
      f"终端命令 {len(chain | builtin)} · Web 命令 {len(web_cmds)}）")
