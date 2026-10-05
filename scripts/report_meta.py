"""实验报告 frontmatter 与无头调用的共享工具(v0.25 harness 硬化)。

不变量 1:**数据有效性字段由 harness 独占写入**。报告头部必须是可解析的
frontmatter,data_validity 取值域:

    valid              数据可信,进统计
    void:timeout       蓝队/红队超时(退避重试后仍超时)
    void:env_unavailable  解释器探针失败——环境问题,不属于被评对象
    void:no_selfverify 蓝队未声明任何自验命令
    void:json_parse    无头调用没有可解析的 JSON 结果行
    void:empty_result  调用成功但模型未返回正文
    void:pre_invariant 存量报告(早于本不变量),仅作证据链,不进统计

缺失或不可解析 → 视同 void:missing(check_consistency 门禁据此拦截)。
作废不删数据:作废即标记 + 统计口径排除,存储一律保留(调参证据链)。
"""
from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

FM_RE = re.compile(r"\A---\n(.*?)\n---\n", re.S)


def frontmatter(fields: dict) -> str:
    """按给定字段序生成 frontmatter 块(值转 YAML 单行安全)。"""
    lines = ["---"]
    for k, v in fields.items():
        v = str(v).replace("\n", " ").replace("\r", " ").strip()
        lines.append(f"{k}: {v}")
    return "\n".join(lines) + "\n---\n"


def parse_frontmatter(text: str) -> dict:
    """解析报告头部 frontmatter;缺失/不可解析返回 {}(调用方视同作废)。"""
    m = FM_RE.match(text or "")
    if not m:
        return {}
    out: dict = {}
    for ln in m.group(1).splitlines():
        if ":" in ln:
            k, _, v = ln.partition(":")
            out[k.strip()] = v.strip()
    return out


def data_validity(text: str) -> str:
    """报告的数据有效性取值;缺失即作废(不变量 1 的读取侧)。"""
    return parse_frontmatter(text).get("data_validity") or "void:missing"


def run_headless(prompt: str, cwd: Path, timeout: int, env: dict,
                 max_budget: int = 600) -> Tuple[Optional[subprocess.CompletedProcess],
                                                 float, int, bool]:
    """无头调用 + 超时自动退避重试(v0.25,P1)。

    首次超时且预算未到上限 → 以 min(2×budget, max_budget) 重试**一次**;
    仍超时 → 放弃。返回 (proc|None, 耗时秒, 最终预算, 是否超时)。
    超时是 harness 资源不足,不是产出质量问题——重试优先于作废。
    """
    budget = min(int(timeout), max_budget)
    while True:
        t0 = time.time()
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "minicode", "-p", prompt,
                 "--yolo", "--no-save", "--output-format", "json"],
                cwd=str(cwd), capture_output=True, timeout=budget, env=env)
            return proc, round(time.time() - t0, 1), budget, False
        except subprocess.TimeoutExpired:
            elapsed = round(time.time() - t0, 1)
            if budget >= max_budget:
                return None, elapsed, budget, True
            budget = min(budget * 2, max_budget)   # 退避重试一次
