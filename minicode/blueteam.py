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
   **`-c` 参数必须用双引号包裹,命令内字符串用单引号**(Windows cmd
   不认单引号包裹,实测 14 条单引号自验全部 SyntaxError);
   **合并自验断言**:同类契约的多条断言用分号连进同一条命令——
   Windows 每条命令的进程启动开销很高,14 条拆开跑既慢又稀释判定;
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


def _decode_out(b: bytes) -> str:
    """命令输出解码。Windows 控制台文案多为 GBK/ANSI,直接 utf-8 解码会
    变乱码使"不是内部或外部命令"类判定失效——两种编码各解一次,取替换
    字符更少者(v0.25;实测 cmd 的缺命令提示是 GBK 且退出码为 1)。"""
    if not b:
        return ""
    best = b.decode("utf-8", errors="replace")
    try:
        alt = b.decode("gbk", errors="replace")
        if alt.count("\ufffd") < best.count("\ufffd"):
            best = alt
    except (ValueError, UnicodeDecodeError):
        pass
    return best.strip()


def verify_commands_from_output(text: str) -> List[str]:
    r"""从蓝队输出提取自验命令(格式:`- 自验: \`<命令>\``)。"""
    return [m.group(1).strip() for m in _SELF_VERIFY_RX.finditer(text)]


_TB_LAST_RX = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception))\s*:?.*$", re.M)
_NOT_STARTED_RX = re.compile(
    r"不是内部或外部命令|is not recognized as|command not found"
    r"|无法将.*识别为|(?:'|\")?[\w./\\-]+(?:'|\")?: No such file")


def _traceback_last_line(out: str) -> str:
    """从命令输出中抽取 traceback 末行(异常类型直接决定归因)。"""
    hits = _TB_LAST_RX.findall(out)
    return hits[-1].strip() if hits else ""


def attribute_failure(cmd: str, out: str) -> str:
    """FAIL 归因(v0.25 不变量 2):ValueError 与红队发现同类 → 蓝队没修好;
    TypeError/NameError/SyntaxError/AttributeError → 自验命令本身写错;
    命令无法启动(shell 报错)→ 环境。归因是提示,不是判决——供人工复核。

    已验证的高频陷阱(v0.25.1 实测,14/14 归因正确):Windows 蓝队爱写
    `python -c '...'`(单引号),cmd.exe 不解析单引号 → 输出
    "SyntaxError: unterminated string literal" 且退出码 1(非 9009)——
    必须归为"自验命令本身写错",绝不能误判"蓝队未修复"。
    提示词已明令 -c 用双引号(见 BLUETEAM_PROMPT 要求 3)。"""
    last = _traceback_last_line(out)
    if "不是内部或外部命令" in out or "is not recognized" in out \
            or "command not found" in out or "No such file" in out:
        return "环境/命令不可启动"
    if last:
        if "ValueError" in last:
            return "蓝队未修复(异常与红队发现同类)"
        if re.search(r"(TypeError|NameError|SyntaxError|AttributeError|"
                     r"KeyError|IndexError)", last):
            return "自验命令本身写错"
        return f"自验命令异常:{last[:80]}"
    return "非零退出,无 traceback——需人工读完整输出"


def independent_verify(text: str, cwd,
                       timeout: int = 120) -> Tuple[List[Tuple[str, str, str]], bool]:
    """独立自验门(v0.25 不变量 2+诊断增强):**不信蓝队自述**,逐条重跑
    蓝队声明的自验命令。返回 ([(命令, 状态, 诊断)], 环境是否可用)。

    状态三态:
        PASS   命令退出码 0
        FAIL   命令跑了、退出码非 0 —— 诊断含 exit/traceback 末行/输出尾部
               (≤800 字符,保留 traceback 末行),归因 蓝队未修复 vs 自验写错
        ERROR  命令没跑起来(超时/OSError/shell 报错)—— harness 或自验写法
    环境不可用(探针失败)→ 单条 ERROR + env_ok=False,调用方整体作废。
    """
    cmds = verify_commands_from_output(text)
    if not cmds:
        return [], True   # 自验缺失由调用方按协议判定,无需探针
    env_ok, why = probe_environment(cwd)
    if not env_ok:
        return [("环境探针 python -c print(1)", "ERROR",
                 f"环境不可用:{why}——本报告数据作废")], False
    results: List[Tuple[str, str, str]] = []
    for cmd in cmds:
        try:
            proc = subprocess.run(cmd, shell=True, cwd=str(cwd),
                                  capture_output=True, timeout=timeout,
                                  env=gate_env())
        except subprocess.TimeoutExpired:
            results.append((cmd, "ERROR", f"(独立复验超时 {timeout}s)"))
            continue
        except OSError as e:
            results.append((cmd, "ERROR", f"(无法启动:{e})"))
            continue
        out = _decode_out((proc.stdout or b"") + (proc.stderr or b""))
        # 命令未启动:shell 对不存在命令的退出码(Windows 9009 / POSIX 127),
        # 或 shell 文案匹配(GBK 控制台的"不是内部或外部命令")
        if proc.returncode in (127, 9009) or _NOT_STARTED_RX.search(out):
            results.append((cmd, "ERROR",
                            f"命令未启动(退出码 {proc.returncode}):{out[-200:]}"))
            continue
        if proc.returncode == 0:
            results.append((cmd, "PASS", out[-300:]))
            continue
        last = _traceback_last_line(out) or "(无 traceback)"
        diag = (f"exit={proc.returncode}; traceback 末行: {last}; "
                f"输出尾部: {out[-800:]}")
        results.append((cmd, "FAIL",
                        diag + f" | 归因: {attribute_failure(cmd, out)}"))
    return results, True


def verify_verdict(results: List[Tuple[str, str, str]]) -> str:
    """修复质量判定(v0.25.2,审查定稿——与链路可信性分离的两个维度):

        verified    全部自验 PASS——修复质量达标
        unverified  全部 FAIL 且归因均为"自验命令本身写错"——修复质量未知
                    (链路可信但自验跑不起来,典型:单引号 -c)
        failed      全部 FAIL 且至少一条归因"蓝队未修复"——修复确认无效
        mixed       部分通过/部分失败,或混有 ERROR——部分可归因
        none        无自验条目(此时 data_validity 应为 void:no_selfverify)

    data_validity(链路可信性)与 verify_verdict(修复达标)是两个正交
    维度:v0.25.1 尝试 3 曾 14 条自验全 FAIL 仍标 valid——链路可信、
    修复未经验证,统计蓝队修复率时只能用 verified 的报告。
    """
    if not results:
        return "none"
    n_pass = sum(1 for _c, st, _d in results if st == "PASS")
    blue_unfixed = sum(1 for _c, st, d in results
                       if st == "FAIL" and "蓝队未修复" in d)
    if n_pass == len(results):
        return "verified"
    if n_pass == 0:
        return "failed" if blue_unfixed else "unverified"
    return "mixed"


def render_independent_verify(results: List[Tuple[str, str, str]],
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
    for cmd, state, diag in results:
        lines.append(f"- [{state}] `{cmd}`"
                     + (f"\n  - {diag}" if state != "PASS" and diag else ""))
    n_pass = sum(1 for _c, s, _d in results if s == "PASS")
    n_fail = sum(1 for _c, s, _d in results if s == "FAIL")
    n_err = sum(1 for _c, s, _d in results if s == "ERROR")
    lines.append(f"\n独立复验:{n_pass}/{len(results)} 通过"
                 + (f"(FAIL {n_fail}, ERROR {n_err})"
                    if n_fail or n_err else "")
                 + (" —— **存在未通过项,蓝队自述与实测不符**"
                    if n_fail or n_err else ""))
    return "\n".join(lines)
