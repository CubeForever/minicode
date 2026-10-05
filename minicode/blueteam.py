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

import re
import time
from pathlib import Path
from typing import Dict

BLUETEAM_SYSTEM = (
    "You are the blue team. You FIX the issues the red team confirmed — "
    "but you re-reason instead of blindly following the list: verify each "
    "claim against the code, look for other instances of the same class of "
    "problem, and never bake an incomplete red-team rationale into the fix.")

BLUETEAM_PROMPT = """基于红队报告修复已确认的问题。你有写权限,修完必须自验。

## 工作方式(三条硬要求)
1. **先核对再修**:逐条检查红队「发现」的依据是否成立。红队的证伪理由
   可能不完整——例如它可能只验证了 int/float 就断定某转换"无害",而
   Decimal/Fraction 路径并未覆盖。理由不完整就补全验证,判断错误就推翻;
2. **举一反三**:每修一个问题,检查**同一类问题的其他实例**(同类边界、
   同类缺失校验、同类类型处理),一并处理或在报告中说明为何不适用;
3. **修复后自验**:运行与修复相关的测试/命令,失败就继续修。

## 输出(修复完成后)
- 逐条:红队发现 → 你的核对结论(成立/部分/推翻)→ 做了什么改动 (文件:行)
- **红队遗漏的同类问题**:你在举一反三中额外发现并处理的(这是本报告
  最重要的部分——它度量红队漏了多少)
- 自验结果

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
