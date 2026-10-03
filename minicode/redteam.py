"""只读红队评审(v0.23):1 个攻击性只读子代理,对本会话改动出报告。

与审查共识一致的硬约束:
- **只读、只出报告**——不做修复循环;是否加蓝队由 precision 标注数据
  (eval/redteam/ 的人工标注)决定,v0.24 再议;
- **默认不自动触发**——仅 /redteam 显式调用,precision 未知前不进任何
  自动链路;
- 报告存档 .minicode/redteam/<ts>.md,内附人工盲标三列表(先自查再对照,
  "否"的条目=提示词误报率的直接度量)。

子代理经 dispatch factory 运行:只读 registry + 独立 15 次迭代上限,
且 sub.run_turn() 不经过 _run_turn——天然不触发自检门/经验引擎
(v0.22.2/v0.22.3 已实证)。
"""
from __future__ import annotations

import time
from pathlib import Path

REDTEAM_SYSTEM = (
    "You are an adversarial code reviewer (red team). You only REPORT — you "
    "never modify anything. You care about REAL, provable problems, and you "
    "know that false reports destroy your credibility.")

REDTEAM_PROMPT = """对下面这批代码改动做对抗性评审。只读调查,禁止修改任何文件。

## 工作方式:先发散,再收敛
第一遍**穷举可疑点**——对照攻击面清单自由发散,不设门槛,宁多勿漏;
第二遍对每个可疑点**逐条证伪**(找反例/追调用点/验触发条件)。
只有证伪失败的才进「发现」;证伪成功的写进「已证伪的可疑点」并给出
理由——这一节是工作质量的证据,不许省略。不要跳过发散直接下结论。

## 攻击面清单(逐项过)
1. 边界条件:空输入/零/负数/超长/Unicode/文件末尾
2. 错误路径:异常被吞?失败状态残留半成品?
3. 回归:改动是否破坏既有调用点/既有测试的隐含假设
4. 资源与并发:文件句柄/临时目录泄漏;竞态;可重入性
5. 注入面:拼接进 shell/SQL/路径/正则的外部输入
6. 平台差异:Windows/POSIX 行为不一致(路径分隔符、编码、信号)

## 输出格式(严格遵守)
## 红队报告
### 发现
- [阻断|应修|可选] 问题一句话 (文件:行) — 依据与触发条件
### 已证伪的可疑点
- [证伪] 可疑点 — 证伪理由
### 存疑(不确定是否真实)
- [存疑] …
### 明确排查过且无问题的领域
- …
没有发现就写"无发现"——编造问题比遗漏问题更不可接受。每条发现必须给出
可在代码中指认的依据;给不出的不要写。

## 待评审改动(diff)
{diff}
{scope}"""


def build_redteam_prompt(diff_text: str, scope: str = "") -> str:
    scope_line = f"\n## 用户圈定的评审范围\n{scope}\n" if scope else ""
    # 截断透明化(v0.23.1,审查采纳):静默截断会让红队对"没看到的部分"
    # 沉默,报告却显得完整——系统性低估发现率,虚高 v0.24 要用的 precision。
    limit = 14000
    shown = diff_text[:limit]
    trunc_note = ""
    if len(diff_text) > limit:
        trunc_note = (f"\n> ⚠ 本次 diff 已截断:仅展示前 {limit} 字符"
                      f"(共 {len(diff_text)} 字符)。未展示部分你**没有评审过**,"
                      "不要默认它们没有问题——必须在报告末尾单列「未覆盖区域」"
                      "一节说明此限制,供标注者把该任务的 precision 单独定性。\n")
    return REDTEAM_PROMPT.format(diff=shown, scope=scope_line) + trunc_note


def redteam_dir(cwd) -> Path:
    return Path(cwd) / ".minicode" / "redteam"


def save_report(cwd, report: str) -> Path:
    """报告存档,内附人工盲标三列表(空表,由人填写)。"""
    p = redteam_dir(cwd) / f"{time.strftime('%Y%m%d-%H%M%S')}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    body = (report or "(红队无输出)").strip() + "\n\n" + LABELING_TABLE
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(body)
    return p


LABELING_TABLE = """## 人工标注(precision)

> 盲标流程:先自己读改动代码、列出自己看到的问题,**然后**再对照本报告,
> 逐条判定。否则会被报告锚定,把"我也觉得对"误算成命中。

| 红队提出的问题 | 是否真实存在(是/否/部分) | 严重程度(阻断/应修/可选) |
|---|---|---|
|  |  |  |

"否"的条目是这份数据里最有价值的部分——它们直接度量红队提示词的
误报率,决定 v0.24 蓝队循环是否值得默认开启、攻击性强度要不要调。

**「存疑」条目单独统计**(不算进误报,也不算进命中):它们映射的是
攻击面清单里"模型不确定"的风险类型。积累 5-6 个任务后,反复出现存疑
的攻击面若在 eval 环境里结构性不可测(如本批的编码环境/字节码缓存),
应从提示词移出,转由 /review 或人工覆盖。
"""
