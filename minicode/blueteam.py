"""蓝队(v0.24):修复红队确认的问题——但**不是照单修复的执行器**。

第一批/第二批 precision 实测(6 任务,31 条主张,0 误报)暴露了一个边界:
红队的证伪理由可能不完整(implement-spec 中红队判 float() "仅冗余",
漏了 Decimal/Fraction 丢精度)。若蓝队只读「发现」列表,红队的错误判断
会被固化;若蓝队读到**证伪过程**,才有可能识别"同一类问题的其他实例"。

因此蓝队提示词有三条硬要求:
1. 修复前先核对红队依据是否成立——理由不完整就补全,判断错误就推翻;
2. **举一反三**:同一类问题的其他实例必须一并处理(浮点容差/元素类型
   校验这类红队漏项,靠这一条补上);
3. 修复后自验,并报告"红队遗漏的同类问题"。

A/B 对照实验(scripts/run_bluefix_ab.py)用同一份红队报告分别以
mode="findings"(仅发现列表)与 mode="full"(全量,含证伪过程)运行,
人工比对哪组补上了已知漏项——实验结论决定蓝队正式提示词读什么。
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Tuple

BLUETEAM_SYSTEM = (
    "You are the blue team. You FIX the issues the red team confirmed — "
    "but you re-reason instead of blindly following the list: verify each "
    "claim against the code, look for other instances of the same class of "
    "problem, and never bake an incomplete red-team rationale into the fix.")

BLUETEAM_PROMPT = r"""基于红队报告修复已确认的问题。你有写权限,修完必须自验。

## 工作方式(四条硬要求)
0. **契约驱动(先做)**:把 docstring / spec 的每一条承诺逐条列出,对照
   实现标注 已兑现/未兑现/无法验证——"未兑现"的与红队发现一起处理。
   红队漏的往往不是"已发现问题的同类",而是**整条没人提过的契约**
   (数值精度/容差/元素类型约束都在这一层);
1. **先核对再修**:逐条检查红队「发现」的依据是否成立。红队的证伪理由
   可能不完整——例如它可能只验证了 int/float 就断定某转换"无害",而
   Decimal/Fraction 路径并未覆盖。理由不完整就补全验证,判断错误就推翻;
2. **举一反三**:每修一个问题,检查**同一类问题的其他实例**(同类边界、
   同类缺失校验、同类类型处理),一并处理或在报告中说明为何不适用;
3. **修复后自验——自验命令必须用反引号逐条给出**(每行格式:
   `- 自验: \`<命令>\``)。这些命令会被评测脚本**独立重跑**,自述不算数;
   命令失败就继续修。**禁止占位/示意命令**——明知跑不通还写的"假自验"
   按未通过计,评测脚本先跑解释器探针,环境不可用时整份报告作废;
4. **新路径对照契约**:修复引入的**新实现路径**要重新过一遍第 0 条
   (重写可能让原本兑现的契约失效——如把直接求和改成滚动和,数值精度
   契约就从"兑现"变"未兑现")。

## 输出(修复完成后)
- 契约清单:每条承诺 → 已兑现/未兑现/无法验证(未兑现的必须已处理)
- 逐条:红队发现 → 你的核对结论(成立/部分/推翻)→ 做了什么改动 (文件:行)
- **红队遗漏的同类问题**:你在举一反三与契约清单中额外发现并处理的
  (这是本报告最重要的部分——它度量红队漏了多少)
- 自验命令清单(反引号格式,见上)

## 红队报告({mode_label})
{input}"""


def blueteam_dir(cwd) -> Path:
    return Path(cwd) / ".minicode" / "bluefix"


def extract_sections(report_md: str) -> dict:
    """把红队报告按 ### 小节切分。返回 {节名: 正文};红队报告标题之后、
    人工标注表之前的内容参与切分(标注表是给人的,不给蓝队)。"""
    text = report_md.split("## 人工标注")[0]
    if "## 红队报告" in text:
        text = text.split("## 红队报告", 1)[1]
        text = text.split("\n", 1)[1] if "\n" in text else ""
    sections: dict = {}
    cur = None
    for ln in text.splitlines():
        if ln.startswith("### "):
            cur = ln[4:].strip()
            sections.setdefault(cur, [])
        elif cur is not None and ln.startswith("## "):
            cur = None
        elif cur is not None:
            sections[cur].append(ln)
    return {k: "\n".join(v).strip() for k, v in sections.items() if v}


def blue_input(report_md: str, mode: str) -> str:
    """蓝队的输入。mode='findings' → 仅「发现」(对照组 A);
    mode='full' → 全量报告含证伪过程(实验组 B)。"""
    if mode == "findings":
        sections = extract_sections(report_md)
        findings = next((v for k, v in sections.items() if k.startswith("发现")), "")
        return findings or "(红队报告无发现节)"
    return report_md.split("## 人工标注")[0].strip()


def build_blueteam_prompt(redteam_input: str, mode: str) -> str:
    label = ("对照组 A:仅发现列表" if mode == "findings"
             else "实验组 B:全量报告(含已证伪可疑点与存疑)")
    return BLUETEAM_PROMPT.format(mode_label=label, input=redteam_input)


def save_fix_report(cwd, text: str) -> Path:
    p = blueteam_dir(cwd) / f"{time.strftime('%Y%m%d-%H%M%S')}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    return p


def findings_summary(report_md: str) -> str:
    """控制台摘要:发现条数与定级,给 /bluefix 前置确认用。"""
    sections = extract_sections(report_md)
    findings = next((v for k, v in sections.items() if k.startswith("发现")), "")
    grades = re.findall(r"\[(阻断|应修|可选)\]", findings)
    if not grades:
        return "无确认发现"
    counts: Dict[str, int] = {}
    for g in grades:
        counts[g] = counts.get(g, 0) + 1
    return "、".join(f"{k} {v} 条" for k, v in counts.items())


_SELF_VERIFY_RX = re.compile(r"^\s*-\s*自验[:：]\s*`([^`]+)`", re.M)


def gate_env() -> dict:
    """独立自验门专用 env:解释器目录前置进 PATH(v0.24.2,P0 修复)。

    首轮实战 6 条自验全 FAIL 即因门没带这套 env——蓝队声明的
    `python ...` 在 Windows shell 里不可解析,而求解/红队的 env
    (_base_env)有前置。门与它们必须同语义。
    """
    import sys as _sys
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    exe_dir = str(Path(_sys.executable).parent)
    env["PATH"] = exe_dir + os.pathsep + env.get("PATH", "")
    return env


def probe_environment(cwd, timeout: int = 30) -> Tuple[bool, str]:
    """解释器探针:`python` 在自验门 env 里可达吗?

    不可达 → **环境不可用**,整批自验数据作废——而不是把 N 条命令
    全标 FAIL 混进 precision 数据(v0.24.2,审查①:门响亮失败但报告
    标"通过"的同构缺陷)。区分"环境问题(harness 的锅)"与
    "命令失败(蓝队的锅)"是数据可信性的第一道分界。
    """
    try:
        proc = subprocess.run('python -c "print(1)"', shell=True,
                              cwd=str(cwd), capture_output=True,
                              timeout=timeout, env=gate_env())
        if proc.returncode == 0:
            return True, ""
        out = ((proc.stdout or b"") + (proc.stderr or b""))\
            .decode("utf-8", "replace").strip()
        return False, out[:200] or f"退出码 {proc.returncode}"
    except subprocess.TimeoutExpired:
        return False, f"探针超时 {timeout}s"
    except OSError as e:
        return False, str(e)


def verify_commands_from_output(text: str) -> List[str]:
    r"""从蓝队输出提取自验命令(格式:`- 自验: \`<命令>\``)。"""
    return [m.group(1).strip() for m in _SELF_VERIFY_RX.finditer(text)]


def independent_verify(text: str, cwd,
                       timeout: int = 120) -> Tuple[List[Tuple[str, bool, str]], bool]:
    """独立自验门(v0.24.1):**不信蓝队自述**,把它声明的自验命令逐条
    重跑。返回 ([(命令, 是否通过, 输出尾部)], 环境是否可用)。

    v0.24.2:先探针后执行——`python` 不可达时返回环境不可用
    (env_ok=False),调用方必须把本报告整体标记作废,而不是把
    环境性 FAIL 混进数据。env 统一走 gate_env()(P0 修复:门此前
    没带解释器前置,Windows 下 6 条自验全 FAIL 而报告标"通过")。
    """
    cmds = verify_commands_from_output(text)
    if not cmds:
        return [], True   # 自验缺失由调用方按协议判定,无需探针
    env_ok, why = probe_environment(cwd)
    if not env_ok:
        return [("环境探针 python -c print(1)", False,
                 f"环境不可用:{why}——本报告数据作废")], False
    results: List[Tuple[str, bool, str]] = []
    for cmd in cmds:
        try:
            proc = subprocess.run(cmd, shell=True, cwd=str(cwd),
                                  capture_output=True, timeout=timeout,
                                  env=gate_env())
            out = ((proc.stdout or b"") + (proc.stderr or b""))\
                .decode("utf-8", "replace").strip()
            results.append((cmd, proc.returncode == 0, out[-300:]))
        except subprocess.TimeoutExpired:
            results.append((cmd, False, f"(独立复验超时 {timeout}s)"))
        except OSError as e:
            results.append((cmd, False, f"(无法执行:{e})"))
    return results, True


def render_independent_verify(results: List[Tuple[str, bool, str]],
                              env_ok: bool = True) -> str:
    if not env_ok:
        why = results[0][2] if results else ""
        return ("## 独立自验门 —— **环境不可用,本报告数据作废**\n\n"
                f"解释器探针失败:{why}\n\n"
                "命令级 FAIL 未逐条展开:环境问题属于 harness,不属于蓝队;"
                "修复环境后重跑,本报告不进数据集。\n")
    if not results:
        return ("## 独立自验门\n\n蓝队未声明任何自验命令——"
                "按 v0.24.1 协议视为**自验缺失**,数据不可信。\n")
    lines = ["## 独立自验门(评测脚本重跑,非蓝队自述)"]
    for cmd, ok, out in results:
        lines.append(f"- [{'PASS' if ok else 'FAIL'}] `{cmd}`"
                     + (f" — {out}" if not ok and out else ""))
    failed = sum(1 for _c, ok, _o in results if not ok)
    lines.append(f"\n独立复验:{len(results) - failed}/{len(results)} 通过"
                 + (" —— **存在未通过项,蓝队自述与实测不符**" if failed else ""))
    return "\n".join(lines)
