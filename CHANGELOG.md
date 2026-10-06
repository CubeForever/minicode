# Changelog

## 0.25.2 (2026-10-06)

语义漏洞修复 + 蓝队提示词合并自验(审查定稿的三小项,进 v0.26 前收口):

**P0:verify_verdict——"链路可信"与"修复达标"拆为正交两维**
- v0.25.1 尝试 3 的 14 条自验全 FAIL,报告却标 data_validity=valid——
  valid 混着"环境与链路可信"与"修复质量达标"两个含义,读报告的人会
  误以为修复可信,统计修复成功率时也会被 shell 引号类的 0% 污染
- 新增 `verify_verdict` 判定(独立自验门结果 → 修复质量):
  verified(全 PASS)/ unverified(全 FAIL 且归因均"自验写错",
  修复质量未知)/ failed(全 FAIL 且有"蓝队未修复"归因,修复确认
  无效)/ mixed(部分通过或混有 ERROR)/ none(无自验条目)
- frontmatter 新增 verify_verdict 字段;data_audit 新增
  "修复质量判定(valid 报告)"一维——统计蓝队修复率只能用 verified

**P1:归因案例入注释**(blueteam.py attribute_failure docstring):
Windows 蓝队高频陷阱 `python -c '...'` 单引号 → cmd.exe 输出
"SyntaxError: unterminated string literal" 且退出码 1(非 9009),
14/14 归因正确(v0.25.1 实测)——下一个改提示词的人会在代码里看到

**P1:蓝队提示词合并自验断言**:同类契约的多条断言用分号连进同一条
命令——Windows 每条命令的进程启动开销很高,14 条拆开跑既慢又稀释判定;
(纯提示词层优化,不动机制)

测试 409 项(+1:verify_verdict 真值表)。

## 0.25.1 (2026-10-06)

v0.25 三不变量的真数据验证跑 + 两个实战发现当日修复：

**验证跑（implement-spec findings,3 次尝试,全部正确标记）**
- 尝试 1:蓝队 600s×2 退避后仍超时 → void:timeout ✔(作废语义首次实战)
- 尝试 2:蓝队 503s 完成,最终正文仅"我先"(生成截断) →
  void:no_selfverify ✔(0 条声明命令,数据自动隔离)
- 尝试 3:✓ 通过,data_validity=valid——**valid 值域首次落地**
- frontmatter 写入路径三真跑三正确;run_headless 退避重试实战触发
  (600s 上限内重试一次)

**归因首战命中**:尝试 3 的 14 条自验全部 FAIL,门 14/14 归因
"自验命令本身写错"(SyntaxError: unterminated string literal),
零误判为"蓝队未修复"——exit 码/traceback 末行/归因三件套在真数据上
完成判别,正是 v0.25 不变量 2 的设计目标。

**实战发现当日修复:单引号 -c 命令**
- 蓝队在 Windows 写 `python -c '...'`(单引号),cmd.exe 不解析,
  14/14 SyntaxError——蓝队写自验命令的高频踩坑
- 蓝队提示词明令:`-c` 参数必须用双引号包裹,命令内字符串用单引号
- 超时调优数据入档:300s full 超时 / 360s 双超时 / 600s 双过 /
  findings 600s 1/3 成功——蓝队负载(契约驱动+自验矩阵)需要长预算,
  v0.26 前置观察项

**工具层强制(heredoc 事故的根治)**:check_consistency 新增第 11 项——
全仓 Python 文件(minicode/tests/scripts)必须可被 ast 解析,任何
shell-heredoc 转义污染(字符串字面量被真实换行打断)在推送阶段即 CI
失败,不再依赖"记得用 Edit/Write"的文档约定。一致性检查 23→24 项。

测试 408 项(提示词断言加强)。

## 0.25.0 (2026-10-05)

harness 硬化专版（三个不变量 + 诊断增强 + 退避重试；**不加新功能**）：

连续六轮的静默失败历史——sys.path 缺根 → 二进制 null → diff 不落盘 →
stderr 空 → diff desync → 门无 env——共同特征是"失败被静默吞掉、数据被
当成有效"。本版把"数据可信"从流程约定升级为 CI 可强制的不变量。

**不变量 1:数据有效性由 harness 独占写入（structured frontmatter）**
- 报告头改为可解析 frontmatter:`data_validity` 取值域
  {valid, void:timeout, void:env_unavailable, void:no_selfverify,
  void:json_parse, void:empty_result, void:pre_invariant}
- **缺失即作废**:无 frontmatter 或不可解析 → `void:missing`
- `scripts/report_meta.py`:frontmatter 写入/解析/取值 的共享工具
- **CI 门禁**:check_consistency 新增第 10 项——扫描 eval/redteam 与
  eval/bluefix,任一报告缺失/非法 data_validity 即 CI 失败
- 存量 17 份报告回填 `void:pre_invariant`(仅作证据链,不进统计)——
  作废不删数据,保留调参/排障证据

**不变量 2:FAIL 必须携带可归因诊断（门从二态升三态）**
- PASS / FAIL / **ERROR** 三态:ERROR = 命令没跑起来（超时/OSError/
  命令未启动),与"跑了但结果不对"彻底分离
- FAIL 诊断三件套:exit 码 + **traceback 末行抽取** + 输出尾部(≤800)
- **自动归因**:ValueError 与红队发现同类 → "蓝队未修复";
  TypeError/NameError/SyntaxError → "自验命令本身写错";
  命令未启动(退出码 127/9009 或 shell 文案) → 环境/自验写法
- 输出解码双回落(utf-8 → gbk):Windows 控制台"不是内部或外部命令"
  为 GBK,此前会变乱码使判定失效

**诊断增强:超时自动退避重试 + 耗时入头（P1）**
- `report_meta.run_headless`:超时以 2× 预算自动重试一次(上限 600s)——
  超时是 harness 资源不足,不是产出质量问题,重试优先于作废
- 每个无头调用记录耗时与最终预算,写入 frontmatter
  (solve_seconds / redteam_seconds / bluefix_seconds / timeout_budget)

**可见性:scripts/data_audit.py（P2,价值被审查低估的一项）**
- 输出 有效率 / 作废率(按原因分类),`--strict` 在缺 frontmatter 时
  非零退出,`--json` 供机器消费
- 连续六轮的根因都是"没有可见的失败率指标"——本脚本让整类问题从
  "靠人发现"变成"自动可见"

测试 408 项(+9:三态/归因/ERROR/不变量枚举/退避重试/上限放弃)。

## 0.24.2 (2026-10-05)

独立自验门实战收口 + 数据可信性纪律（第三轮标注的三个问题当日处理）：

**P0:门全 FAIL 而报告标"通过"——同构于 v0.23.4 空报告,根因是门没带 env**
- 首轮实战:蓝队 6 条自验全部 FAIL（'python' 不是内部或外部命令）,而
  报告头部"修复后校验:通过"——独立自验门的 env 没有解释器前置
- 修复:`gate_env()`(解释器前置,与 _base_env 同语义)+ **前置探针**:
  `python -c "print(1)"` 不可达 → **环境不可用,整份报告作废**——区分
  "环境问题(harness 的锅)"与"命令失败(蓝队的锅)",环境性 FAIL 不混进
  数据;/bluefix 与 run_bluefix_ab 同步接入,失败标 ✗ 并注明数据作废

**P1:蓝队能声明明知无效的占位自验**
- 提示词明令:禁止占位/示意命令,评测脚本逐条重跑,占位按未通过计;
- 契约驱动补一层:修复引入的**新实现路径**要重新对照契约(滚动和之于
  数值精度——重写可能让原本兑现的契约失效)

**P1:spec 隐含契约显式化(红队盲区集中在契约的隐含部分)**
- /spec new 起草提示要求显式写出 元素类型/数值精度与容差/边界语义/
  平台差异——给红队可测的目标,比让它自己猜可靠

**数据可信性纪律固化**(eval/README):探针前置/自验缺失=数据不可信/
失败必须响亮——v0.25 前不再加新机制,先让每一环的数据可信。

测试 399 项(+1:环境不可用作废回归)。

## 0.24.1 (2026-10-05)

A/B 对照实验结果落地 + 独立自验门（审查判定"最重要"的一条）：

**实验结论:蓝队输入模式定稿 A(只读发现列表)**——人工判定已知三漏项,
A 覆盖面 ≥ B(A"举一反三"补 7 类 k 类型实例,B 仅 3 类),成本更低,且
B 被证伪过程带偏:把"红队 diff 含 float() 但文件没有"归为红队瑕疵,
未意识到评审对象与实际文件不同步,最终代码仍保留了这一矛盾。/bluefix
默认模式改为 findings。

**三个实验发现全部修复/落地**:
1. **评审对象绑定**:run_bluefix_ab.py 红队对每轮求解现场重跑——旧报告
   评审的是另一次求解的产物,对象不同步是 B 事实错误的根源;
2. **独立自验门(最重要)**:A/B 两组蓝队都声称做了自验(A"验证矩阵"、
   B"24 passed")而项目里根本没有测试文件——声明式自验不可信。蓝队
   自验命令必须以 `- 自验: \`<命令>\`` 格式声明,评测脚本独立重跑,
   PASS/FAIL 写进报告;未声明 = 自验缺失,数据不可信;
3. **契约驱动提示词(蓝队第 0 条硬要求)**:三个漏项(Decimal 精度/浮点
   容差/元素类型)都不是"已发现问题的同类",而是整条没人提过的契约——
   根因驱动的举一反三够不到。蓝队先把 docstring/spec 每条承诺列出,
   逐条标注 已兑现/未兑现/无法验证,未兑现的与红队发现一起处理。

**新指标:红队盲区率 = 红队漏项总数 ÷ 红队发现总数**(eval/README 入档)。
precision 说明红队不乱说,盲区率说明红队说不全——后者才指导提示词演进:
结构性盲区从攻击面清单移到契约驱动检查。

测试 398 项(+4)。

## 0.24.0 (2026-10-05)

蓝队循环 + A/B 对照实验（v0.24 主题：**红队发现 → 蓝队再推理修复 → 校验通过率**）：

**precision 数据支撑的 go 决策（人工盲标,6 任务）**：31 条红队主张 0 误报、
4 条确定发现（1 应修 + 3 可选,零阻断零凑数升级）、28 条证伪理由全部成立、
发现密度中难度为低难度的 3 倍——红队质量经两批任务验证,蓝队最怕的
"修不存在的问题"风险已实测排除。

**蓝队（minicode/blueteam.py 新模块 + /bluefix [findings|full]）**
- **写权限子代理**（红队只读,蓝队必须能改）:独立 ShellState/Session/
  Checkpoint,sub.run_turn() 直跑——不触发自检门/经验引擎（v0.22.2/0.22.3
  实证的边界）,max_iterations=25
- 提示词三条硬要求（审查定稿）:**先核对再修**（红队证伪理由可能不完整,
  如 float() "仅冗余"漏了 Decimal 丢精度）/ **举一反三**（同类问题的其他
  实例一并处理,浮点容差、元素类型校验这类漏项靠这条补上）/ **修复后自验**
- 强制输出「红队遗漏的同类问题」——它度量红队漏了多少,决定红队提示词
  的下一步校准
- 输入模式即实验变量:findings（仅发现列表）/ full（全量含证伪过程）

**A/B 对照实验（scripts/run_bluefix_ab.py）**
- 同一份红队报告、两种蓝队输入各跑一次:重建沙箱 → 求解 → 蓝队 → 复跑
  eval 校验 → 修复前/后 diff 与蓝队输出落盘 eval/bluefix/
- 判据（人工）:对照已知三漏项（float()/Decimal 精度、浮点容差、元素类型
  校验）,比较 A/B 各补上哪些——B 明显补上 → 蓝队正式提示词必须读全量
- 默认 pilot=feature-implement-spec（已知漏项所在任务）,--task 可换

**流程纪律延续**:蓝队调用失败同样响亮（失败分类+stdout/stderr 尾部落盘,
复用 v0.23.4 的 _extract_report 语义）。

测试 394 项(+5:章节提取/A-B 输入隔离/提示词硬要求/写权限子代理接线/
无报告告警)。

## 0.23.4 (2026-10-03)

红队 harness 三处修复（第二批标注发现的全部问题,当日收口）：

**① 红队调用失败响亮化（P0：implement-spec 报告为空的根因类）**
- 旧解析 `splitlines()[-1]` + 宽 except：stdout 无 JSON 行时静默产出"(红队调用失败: )"空报告,批次仍标记成功——流程静默失败比崩溃更危险（与 v0.23.2 盲标协议同类）
- `_extract_report` 从后向前扫 JSON 结果行,失败分类 no-json / empty-result,失败时 stdout 与 stderr 尾部**全文落盘**进报告;控制台标 ✗ 并注明"报告仅作排查用"
- `TimeoutExpired` 单独分支（旧代码未接,红队超时会崩整批）,报告写明超时与调参建议

**② eval harness 卫生**：`_diff_vs_setup` 跳过 `.pytest_cache/` 与 `.minicode/`（BRAIN.md 污染评审对象、消耗红队存疑额——第二批两个任务中招）

**③ env 一致性**：求解与红队统一走 `_base_env()`,并把解释器目录前置进 PATH——沙箱内 agent 的 bash 中 python 由此可解析,消掉"agent 自述无解释器可用"一类失实环境记录（implement-spec 的 BRAIN.md 曾写"No Python interpreter",同批红队却实测 4 passed）

测试 389 项（+4:提取三态/失败响亮/副产物滤除/PATH 前置）。

## 0.23.3 (2026-10-03)

红队第一批标注基线入档 + 发散-收敛提示词（v0.24 方向调整的落地）：

**第一批标注结果（人工盲标,3 个简单任务）**：误报率 0%——1 条确定发现
（cli-flag 字面量 `--upper` 被静默吞,红队比人工自查更具体:补了位置无关性
与触发用例）、2 条存疑(1 部分成立 / 1 不成立,且均为红队自标存疑)。
"全无否"信号命中,提示词偏保守;但其中两个任务天然无可发现缺陷,
"无发现"是正确行为——任务集过简,不足以判断提示词上限。

**据此 v0.24 顺序调整(审查定稿)**:
- **先扩任务集,不做蓝队**:新增 `--batch2`(中等难度:bugfix-json-syntax
  解析逻辑 / feature-write-tests 造测试 / feature-implement-spec 多步
  实现)——这是红队能发挥的场域;
- **提示词加发散-收敛工作方式**(改"要求"而非攻击面清单):先穷举可疑点,
  再逐条证伪;证伪失败的进「发现」,证伪成功的必须写入「已证伪的可疑点」
  一节(工作质量的证据,不许省略)——针对"没有发现就写无发现"威慑力
  导致的直接收敛;
- **蓝队循环等 precision 在中等难度站稳再上**。

**「存疑」单独统计**(标注表与 eval/README 同步入档):存疑不计误报也不计
命中,映射攻击面清单里"模型不确定"的风险类型;积累 5-6 个任务后,eval
环境结构性不可测的攻击面(本批:编码环境/字节码缓存)应从提示词移出,
转 /review 或人工覆盖。

测试 385 项(红队提示词断言加强,不加数)。

## 0.23.2 (2026-10-03)

盲标协议可执行性修复（审查发现：沙箱即焚 + diff 未落盘 → 标注必然被红队锚定，整批数据无效）：

- `run_redteam.py` 报告重排为协议顺序：**元信息 → 标注协议 → 待评审改动(diff,自查材料) → 红队报告 → 空白三列表**——diff 此前只进红队 prompt,报告文件里只有一句"已随报告交给红队",人工自查的中间态根本不存在
- 截断透明在报告侧同样生效：diff 超 14000 字符时报告写明"红队实际收到前 N / M 字符"
- 新增 `--keep-sandbox` 开关（默认仍评审后清理），沙箱路径写入报告元信息
- 清理第一轮两份旧报告（191726/192049,二进制过滤生效前的运行产物）；标注以修复后的三份为准
- 测试 385 项（+1:diff 先于报告落盘/协议与沙箱路径在文/截断透明）

## 0.23.1 (2026-10-03)

红队 diff 截断透明化（审查采纳——影响 v0.24 precision 数据可信度的唯一实质项）：

- 旧版 `diff_text[:14000]` **静默截断**：大改动时红队只看到前 14000 字符，对剩余部分沉默，报告却显得完整——系统性低估发现率，虚高 precision
- 修复：截断发生时在提示词中显式声明（展示字符数/总字符数），并**强制红队**在报告末尾单列「未覆盖区域」一节——标注者据此把该任务的 precision 单独定性，不并入总体数据（eval/README 已写入该规则）
- 同步沉淀小样本解读信号（eval/README）：3 任务全无"否"→ 提示词过保守，调激进后换类复跑；"否">40% → 过度报警，收紧后复跑；介于其间维持现状累计标注

测试 384 项（+1：截断声明/小 diff 无声明/未覆盖区域指令）。

## 0.23.0 (2026-10-03)

对抗性质量闭环（v0.23 主题：**不只是写对，而是用红队证明、用 spec 约定**）：

**只读红队评审（/redteam，对标 Codex /review 的对抗性强化）**
- 新模块 redteam.py：1 个攻击性只读子代理对本会话改动（checkpoint diff）出报告——六类攻击面（边界/错误路径/回归/资源并发/注入/平台差异）逐项过，严重度三档（阻断/应修/可选），**存疑单独列出**，"没有发现就写无发现——编造问题比遗漏更不可接受"写进提示词
- 硬约束（与审查共识一致）：只读、只出报告不做修复；**默认不自动触发**（precision 未知前不进任何自动链路）；报告存档 `.minicode/redteam/` 并自带人工盲标三列表
- 子代理经 factory 的 `sub.run_turn()` 运行——不经过 `_run_turn`，天然不触发自检门/经验引擎（v0.22.2/v0.22.3 已实证的边界）
- precision 标注工作流：`scripts/run_redteam.py` 对 eval 任务"求解 → 红队 → 报告+空白标注表"；盲标流程（先自查再对照，防锚定偏差）与三列表格式入档 eval/README；**"否"的条目 = 提示词误报率的直接度量**，是 v0.24 蓝队循环的决策依据

**spec 驱动模式（/spec，对标 GitHub Spec Kit 的终端化）**
- 新模块 specs.py：spec 存档 `.minicode/specs/<id>.md`，验收标准**从 schema 第一天分流**——`[auto]` 机器可判定 / `[manual]` 人工过，无标签保守归 manual
- `/spec new <描述>` 起草（模型按结构化提示写入）、`/spec show|done` 管理、`/spec run <id>` 实施——完成后**自动逐条运行含命令的 [auto] 校验**，全过提示归档
- `/spec to-eval <id>` 一键转化为 eval 回归任务 + 校验脚本（校验脚本逐条运行 [auto] 命令，支持 {python}/{sandbox} 占位符）；**只对存在 [auto] 的 spec 生效**——入口判定，"转化按钮亮不亮"在调用前就知道（预防而非转化后过滤）；无命令的 [auto] 与 [manual] 并入任务 instruction

**提炼失败 debug 留痕（审查建议随版落地）**
- `_maybe_autoskill` 的静默 except 在 debug 模式（或 MINICODE_DEBUG）下输出 `[autoskill] 提炼失败` 告警——附加功能依旧绝不影响主回合，但"为什么我的技能没生成"不再无从排查

**终端命令 41 → 43**（+`/redteam`、`/spec`）；测试 383 项（+10：红队提示词/存档/命令端到端、spec 分类/转化门槛/实施自验、debug 留痕）。

## 0.22.3 (2026-10-03)

自检修复回合不再做技能提炼（审查 v0.22.2 复盘的非阻塞建议，两行成本即日收口）：

- `_maybe_autoskill` 此前在 `_run_turn` 中不受 `_in_verify` 保护：自检修复回合若碰巧改了 3+ 个文件且有试错，`should_autoskill` 会放行一次提炼——而素材是"自检失败的挣扎过程"，可能沉淀出负面技能（如"如何绕过自检"）
- 修复：`_maybe_autoskill` 增加 `in_verify` 参数，修复回合直接跳过（早于 should_autoskill 判定）；正常回合提炼门照常
- 测试：in_verify=True 时条件全满足也必须跳过；正常路径调用链未被一刀切（373 项，+1）

## 0.22.2 (2026-10-03)

自检门防嵌套（审查 v0.24 前置关切的实证提前化——它不是 v0.24 的新风险，而是当下就存在的活 bug）：

- **无界递归修复**：`_self_verify` 的修复回合走 `_run_turn`，而 `_run_turn` 结尾又会调用 `_self_verify`——只要每轮修复还在改文件且自检持续失败，"自检→修复→又自检→再修复"就会无界嵌套（每层再展开 2 轮修复）。修复：`_run_turn` 增加 `_in_verify` 标记，自检的修复回合不再触发自检门——门自身在每轮修复后本来就复验，嵌套语义重复且危险；嵌套深度恒为 1
- **子代理侧确认安全**：dispatch_agent 的 factory 直接跑 `sub.run_turn()`，不经过 `_run_turn`——红队/蓝队子代理天然不会触发自检门，v0.23/v0.24 的多智能体方案在此点上无前置障碍
- 回归测试：监视 `_self_verify` 调用序列，修复回合重入即失败；外层触发恰一次
- 观察沉淀为代码注释（防丢失）：① auto 技能"同名更优提炼落不了盘"的升级信号（hits≥N 且 30 天未落盘 → auto-<slug>-v2，依据 /skills 面板数据再定，勿提前建机制）；② `.hits.json` 按项目 cwd 隔离——worktree 并行子代理落地前必须先定 hits 归属（按 git 仓库根归一或按仓库身份全局聚合），否则重度使用技能会在其他 worktree 视角被判零命中而误归档

测试 372 项（+1）。

## 0.22.1 (2026-10-03)

审查修复（v0.22 复盘的三处收口）：

**auto 技能键名错位修复（审查③引出的真 bug，比"误归档"更严重）**
- 实证发现两个连锁缺陷：frontmatter `name: <slug>` 与目录 `auto-<slug>` 错位 → ① catalog 列得出但 skill 工具加载返回 None（报"empty"）；② `.hits.json` 遥测键与归档判定键（目录名）不一致 → 命中永远查不到，30 天后重度使用的自生成技能也会被误归档
- 修复：frontmatter name 直接写 `auto-<slug>`，catalog 键 / load_skill 目录定位 / 遥测键 / 归档判定键四处同源；附键名对齐与"命中后过期不误归档"回归测试
- 归档边界确认 + 测试固化：`archive_stale_autoskills` 只 glob 项目级 `.minicode/skills/auto-*`，用户级 `~/.minicode/skills` 永不被某个项目误归档（此前仅静态确认，现在有测试）

**brain_search 来源标注（审查②）**
- 检索返回加首行 `[brain_search] 以下条目来自 BRAIN.md 的历史沉淀，可能过时或有误，采信前先验证`——提示注入面的防护提示此前只存在于 system 注入路径（prompts.py），检索路径同源同险

**指标口径固定（审查①）**
- eval/README.md 新增"指标口径"：基线 13/13 处于饱和区，版本间对比以 **token 降幅 + 耗时降幅**为主指标、**成功率不低于基线**为护栏指标（基线数字入档）

测试 371 项（+2）。

## 0.22.0 (2026-10-03)

经验引擎 + 渐进披露 + 度量修复（v0.22 主题：**越用越强，且能证明**）：

**度量基础设施修复（前置，审查发现的 P0）**
- `-p` 模式此前跳过整个回合收尾：token 统计 / microcompaction / turn_end hook / 失败复盘全部不执行（eval 基线 in_tok 恒为 0 的根因）——已补 `_after_turn`
- run_eval.py 改用 `--output-format json` 从结构化 usage 提取 token，替换与实际输出不匹配的 stdout 正则
- **P0-2**：系统提示唯一组装点 `assemble_system_prompt`（prompts.py）——修复 4 处重建丢 skills 段（cli `/plans done`、webui `/brain clear`、`/add-dir`、`/output-style`），自生成技能以它为飞轮入口
- 跑出 13 任务完整基线（eval/baselines，成功率/时长/token 全量可用）

**Brain 召回修复 + 检索工具（22 号内置工具 brain_search）**
- 旧版 brain 注入是裸字符截断（`text[:4000]`）：Facts 一节超长时 Gotchas/Decisions 整段消失；新版按 section 配额均衡渲染，结尾注明省略条数
- 新增 `brain_search` 工具：按关键词检索全部脑条目（零依赖分词，英文按词、中文按 bigram）——system 注入保持有界以维持 prompt 缓存，召回率交给检索（渐进披露思路应用于记忆）

**MCP 渐进披露（对标 Anthropic Tool Search）**
- MCP 工具+资源总数 >15（MCP_INLINE_LIMIT）时不再把全部 schema 塞进上下文：只注册 `mcp_search` 元工具，模型按关键词检索、命中即动态注册进 registry（ToolRegistry 新增 `register()`），下一轮即可直接调用；会话内加载一次永久可用，重复检索自动跳过
- 大 MCP 服务器的 schema token 开销从 O(全部工具) 降为 O(用到)；小服务器零变化
- 顺手清理死常量 MAX_PROMPTS_PER_SERVER（v0.19 遗留，从未使用）

**Skills 自生成（经验引擎，对标 Agent Skills 生态的空白点）**
- 触发门 `should_autoskill`（纯函数）：跨 ≥2 文件的成功回合 +（自检通过 或 工具试错 ≥2 次）才提炼——单文件顺手改/一把过不沉淀
- 信号层补齐：`agent.turn_errors`（回合内工具错误计数）、`agent.last_verify_ok`（自检门外露）、每回合改动文件数（checkpoint 差分）
- 提炼走 `provider.stream_text`，输出经 frontmatter 校验落盘 `.minicode/skills/auto-<name>/SKILL.md`——单层前缀命名完全复用既有发现层（审查修正：两层目录会被 glob 丢弃）；重名不覆盖；无效输出拒绝；假模型/异常一律静默跳过
- 命中遥测：load_skill 落笔 `.hits.json`（次数+最近使用）；/skills 面板显示命中次数并触发归档
- 防膨胀：自生成技能 30 天零命中 → archive_stale_autoskills 移入 archive/（单层 glob 天然不可见，可手工捞回）

**测试**：新增 13 项（配额均衡/中英检索/动态注册/触发门真值表/提炼落盘去重/遥测/过期归档/P0-2 回归/-p 生命周期回归），全量 369 项。

## 0.21.0 (2026-10-01)

编辑可靠性三件套 + eval 回路（v0.21 主题：**让编辑一次成功，并用度量验证收益**）：

**edit_file 多通道弹性匹配（对标 Aider）**
- 旧版 old_string 逐字精确匹配，空白/缩进稍有偏差即失败，只能靠模型重读重试——这是编辑类工具的最高频失败点
- 新版分级降级：精确匹配 → 尾随空格不敏感 → 全空白不敏感（缩进漂移自动对齐）→ difflib 模糊窗口（≥0.90 相似度且明确最优才应用，结果注明 `[fuzzy match 96%, line 1]`）
- 新增可选 `line` 参数（1-based，来自 read_file 输出）：多处命中时消歧、模糊匹配时锚定窗口
- 完全失败时报出**最接近候选区域及相似度**（如 "line 3 (50%)"），模型一次修正而不是盲试

**自动模式编辑 diff 呈现（对标 Claude Code"自动 ≠ 不可见"）**
- accept-edits / full-access 下写工具落盘后渲染紧凑 unified diff（单文件 ≤30 行，自动模式此前只有一行摘要）
- 终端着色（+绿 −红 @@青）；Web 端新增 diff 事件，浏览器内复用 /diff 的着色视图；子代理与 -p JSON 输出保持安静

**编辑后 lint 快速回路（Aider 招牌）**
- 写工具落盘后立即对刚修改的文件跑项目 linter，报错作为 [lint] 反馈追加进同一工具结果——模型当场自修，而不是等到测试阶段
- 配置 `"lint_command": "ruff check {files}"` 显式指定（{files} 占位符自动引用相对路径）；未配置时自动探测 ruff（ruff.toml / [tool.ruff]）与 eslint（package.json + 配置文件），二进制不存在则静默跳过
- 新增 /lint 命令：查看/设置 lint 命令、手动对本会话改动文件跑 lint、off 关闭
- lint_command 列入受限字段：他人仓库的 .minicode.json 不能静默注入命令，须经信任门禁

**eval 回路（真实任务成功率度量）**
- 新增 eval/（13 个任务：修 bug / 加特性 / 重构 / 写测试，初始文件 ≤ 40 行、校验确定）+ scripts/run_eval.py（零依赖无头 runner）+ .github/workflows/eval.yml（每夜自动跑，结果 artifact，任务失败不弄红 CI）
- 每个任务独立临时沙箱：setup 文件 → `python -m minicode -p <指令> --yolo --no-save` → 可执行校验判定；校验脚本在沙箱外防作弊
- 统计成功率 / 时长 / token（解析回合用量行），落盘 JSON 报告；本地 `python scripts/run_eval.py --filter json` 即可复跑；离线自检支持 MINICODE_FAKE_LLM 脚本模型（tests/test_v021_eval.py 端到端验证）

**测试**：新增 16 项（弹性匹配各通道/模糊应用与候选报告/line 消歧/diff 渲染各模式/lint 反馈与探测与渲染/eval runner 端到端），全量 356 项。

## 0.20.0 (2026-10-01)

上下文经济学（v0.20 主题：**分层压缩 + 递增缓存断点 + token 校准**）：

**分层上下文压缩（对标 Claude Code compaction）**
- 旧版把整段历史替换为单条 user 消息，tool_use/tool_result 配对与 thinking 全部丢失——推理模型（GLM/DeepSeek/Claude thinking）压缩后可能因结构断裂报错或质量骤降
- 新版三层保留：最近 4 个回合**逐字保留**（thinking、工具调用配对无损），更早回合交 LLM 出对话摘要，同时机械提取早期工具调用骨架（工具名+参数摘要+结果首行）放进开头 preamble，模型仍可回溯"早期做过什么"
- 只在 user 边界切分回合，tool 配对永远不会被拆散；再次压缩时旧 preamble 并入转录递归再总结，不重复堆积
- 没有更早回合时不浪费一次 LLM 调用（结构化 carry-over 照常保留）

**Anthropic 对话消息递增缓存断点**
- 旧版只缓存 system+tools，长会话中每轮新增消息全部落在缓存外、每轮全价重算前缀
- 新版把 cache_control 断点钉在最近两个已完成回合的末尾（当前回合内位置不变 → 前缀持续命中；进新回合时自然前移，只增量写入一小段）；总量恪守 Anthropic 上限 4 个（system+tools 占 2）
- 端点拒绝 cache_control 时仍自动整体降级（原有兜底不变）

**token 计量与窗口推断**
- 估算比率不再固定 chars//3：每次模型调用后用真实 input tokens 以 EMA 校准 chars/token（夹在 [1.2, 8] 防异常值），中文会话的估算误差从数倍收敛到常数级；比率随会话持久化
- 上下文窗口按模型名自动推断（GLM-4.5+/GLM-5 200K、DeepSeek 128K、Kimi 256K、Qwen3 256K、Gemini 1M、GPT-4.1 1M 等），显式配置仍优先——修掉"openai 兼容端默认 1M 让 60%/80% 压缩阈值在 128K 模型上永远触发不了"的问题
- 上下文条新增"≈N 轮"剩余回合估算（按每回合输入增长 EMA）

**测试**：新增 14 项（分层压缩/骨架/递归压缩/缓存断点位置与稳定性/≤4 总量/校准收敛/窗口推断/剩余回合估算），全量 340 项。

## 0.19.0 (2026-10-01)

运营与韧性（v0.19 主题：**hooks 可视化 + MCP 自愈**）：

**/hooks 查看面板**
- 终端 /hooks 与 Web /hooks：按事件分组列出已配置的 hook 规则（命令、matcher、timeout），空配置时展示全部 8 个可挂事件与决策协议提示
- /help 与 Web 命令提示同步收录

**MCP 自动重启（自愈）**
- stdio 服务器进程意外崩溃 → 下一次工具调用自动重启服务器（重新握手 + 重新拉取工具清单）并重试同一调用，不再永久失败
- 显式 stop（会话退出）不触发重启；重启次数在 /mcp 状态可见（"自动重启 N 次"）
- 重启失败时给出明确的 ToolError（含服务器自身的错误信息）

**PyPI 首发（v0.18.2 tag）**
- release 流水线实测：build + wheel 冒烟成功；publish 因 PyPI 可信发布方尚未登记而等待（一次性人工登记后 `gh run rerun` 或重推 tag 即可发布）

**一致性门禁**
- 新增 `scripts/check_consistency.py`（22 项检查：版本四处一致 / 测试数 / 工具数 / 终端与 Web 命令清单 / 架构图覆盖 / 行数声明 / .gitignore），并接入 CI 独立作业——每次推送强制核对文档声明与代码事实
- 顺手修复：app.js 死命令 /extensions 移除；/review 与 /copy、/todos 补进终端 /help；/review 补进 README 清单
- 测试 321 → **326 项**


## 0.18.2 (2026-09-30)

品牌界面拾遗（ui-optimize 流程收尾三项）：

- **Web /diff 差异化呈现**：/diff 结果不再以纯文本系统行显示，改为工单内的等宽差异视图——新增行绿底、删除行红底、元信息弱化（终端 /diff 此前已着色，行为对齐）
- **长会话历史分页**：历史回放只渲染最近 200 条消息，顶部提供「加载更早的 N 条消息」入口（向前对齐工单边界，逐页加载、工单编号自动重排、滚动锚定原位置）——千条消息级会话首屏不再卡顿
- **移动端安全区适配**：viewport-fit=cover + 刘海/手势条 inset（composer 底部、侧栏底部、顶栏顶部）

测试 321 项全绿。

## 0.18.1 (2026-09-30)

品牌界面打磨（按 ui-ux-pro-max / ui-optimize 规则库全面审计）：

**修复回归**
- Web 端 Markdown 保真：v0.15 起 Web 桥接误用终端流渲染器，模型的 `**加粗**`/表格/列表在服务端就被转成终端字形（•/│），浏览器端无法再渲染。新增 RawStream——终端做 Markdown 转换，Web 桥接走原始流，各得其所

**可访问性（WCAG）**
- 流式输出不再向屏幕阅读器刷屏：#chat 移除 aria-live，新增视觉隐藏状态区，仅播报回合开始/完成/需签核/错误等关键事件
- 签核卡自动聚焦「允许」主按钮（焦点管理）；Esc 在有活动签核卡时=拒绝
- 全部弹窗补 role="dialog" + aria-modal；打开时聚焦首个控件
- ⚠/❓ emoji 字形替换为品牌同款描边 SVG；工单条码标记 aria-hidden（纯装饰）

**交互与触控**
- 全局 :active 按压反馈（scale .985）；button/a 加 touch-action: manipulation 消除 300ms 点按延迟
- 命令提示（/cmdhint）可点击直接填入；上翻时出现「回到底部」悬浮按钮

**性能与排版**
- 流式渲染 rAF 节流 + 后台标签页自动暂停；打字光标改 CSS ::after（移除每帧全树遍历）
- 工单 content-visibility: auto——长会话视口外工单跳过渲染
- 非标准字重 620/640/660 → 600（非可变字体后备不再跳变）；等宽数字 tabular-nums；md 标题台阶 19/17 → 20/18

测试 321 项全绿。
## 0.18.0 (2026-09-30)

生态纵深（v0.18 主题：**hooks 体系 + MCP resources/prompts + 全 shell 环境持久化 + 扩展市场**）：

**hooks 事件体系扩展**

- 事件从 2 个扩到 8 个：`session_start` / `user_prompt_submit` / `pre_tool_use` / `post_tool_use` / `stop` / `subagent_stop` / `pre_compact` / `turn_end`
- 配置支持多规则 + matcher：`"hooks": {"pre_tool_use": [{"matcher": "Bash|edit_file", "command": "...", "timeout": 10}]}`（字符串旧格式完全兼容；matcher 按工具名正则，仅作用于工具事件；每规则可配 timeout）
- JSON 决策协议：hook stdout 输出 `{"decision": "block", "reason": "…"}` 即阻断（reason 反馈给模型自动改道）；`{"decision": "approve"}` 对 pre_tool_use 跳过标准权限确认——**deny 规则 / 工作区锁 / 敏感路径门禁 / plan 只读不受影响，hook 永远不能放宽安全边界**（有专项测试锁定该语义）

**MCP resources / prompts 支持**

- resources → 只读工具 `mcp__<server>__get__<name>`（每服务器上限 20 个），模型可直接读取服务器资源
- prompts → `/prompt <服务器> <提示名> [键=值 …]` 调用并发起回合（终端与 Web 一致）
- 能力声明按「键存在性」判定——服务器以空对象 `{}` 声明支持，真值判定会误判为不支持（实测抓到的 bug）；`/mcp` 状态显示资源与 prompt 数量

**持久 shell：PowerShell / cmd 适配**

- 环境持久化从 bash 扩展到全部三种 shell：PowerShell（`$env:` 重放 + `Get-ChildItem Env:` 转储）、cmd（`set` 重放 + `set` 转储）
- cmd 的引号会破坏 `/V:ON` 延迟展开——值不加引号、逐字符 `^` 转义（含空格）、`&` 紧贴值尾避免尾随空格并进值、含 `%`/`"` 的值跳过
- 多行值在行式转储中无法安全回传，自动跳过；bash 的 base64/NUL 管道不变

**扩展市场索引**

- `minicode --market` / `/market` 列出可安装扩展包（名称/版本/描述/来源）
- `minicode --install <包名>` 自动从市场解析安装源；索引源来自用户级 `marketplaces` 配置（缺省内置社区索引，支持本地路径自建）
- 安全：项目级 `marketplaces` 属受限键（可重定向安装源，必须过信任门禁）；市场只回答「有什么、从哪装」——安装与执行仍走既有信任体系

测试 304 → **321 项**（新增 test_v018_ecosystem.py：matcher/JSON 协议/approve 不越 deny/stop 与 pre_compact 触发、cmd 与 PowerShell 环境往返、MCP resources/prompts 端到端、市场列表/按名安装/受限键）。


## 0.17.0 (2026-09-28)

模型故障转移与缓存可观测（v0.17 主题：**Failover + Cache Insight**）：

**模型故障转移（FailoverProvider）**

- 配置 `fallbacks` 后主模型失败自动切换备用：
  ```json
  {"fallbacks": [{"model": "glm-4.6"},
                 {"provider": "anthropic", "model": "claude-sonnet-4-5", "api_key": "..."}]}
  ```
  model 必填，provider/base_url/api_key 缺省继承主配置，支持 `"fallbacks": ["模型名"]` 字符串简写
- 触发条件：连接失败 / 限速重试耗尽 / 5xx / 模型不存在 / 端点不支持工具等，且**尚未产出任何流事件**——部分输出绝不跨模型重放
- 跨厂商切换无损：会话消息是中性格式，OpenAI 兼容主模型失败可切 Anthropic 备用；切换过程实时提示（终端 ⚠ / Web warn 事件）；上下文压缩（/compact）同样享受故障转移
- `/model` 切换作用于主模型并立即切回主模型；`/status` 显示备用链；`/models` 与 `--probe` 自动委托主端点
- 安全：`fallbacks` 加入项目配置受限键（可携带 api_key——恶意仓库的 fallbacks 必须过信任门禁才生效）

**cache 命中率深度展示**

- 归一化语义明确：input 为总输入，cache_read 是其中命中缓存的部分（OpenAI prompt_tokens_details 与 Anthropic cache 字段统一到同一语义）
- `/cost`：`in 45.2k · out 3.1k · 缓存命中 38.0k（84%）· 写入 2.1k`
- 回合后 token 行（`▲ in … · 缓存 9.9k (80%)`）与 Web 底栏使用量同步显示命中率；新增 `Session.cache_stats()`


## 0.16.0 (2026-09-28)

分发层（v0.16 主题：**扩展生态 + PyPI 发布 + MCP 升级**）：

**扩展分发：`minicode install`**

- `minicode --install <git仓库|本地目录>` 一条命令安装扩展包（`--user` 装入 `~/.minicode`，`--force` 覆盖同名）
- 约定目录自动发现（`skills/`、`commands/`、`agents/`、`tools/`）；可选 `minicode.json` manifest 显式声明 name/version/路径；单文件 `.md` 技能自动包装成技能目录
- 安全边界延续既有信任体系：安装只复制文件——插件 `.py` 首次加载强制确认；manifest 中的 `mcpServers` 明确拒绝自动安装（提示手动配置）；git 源浅克隆、临时目录必清理，绝不触碰用户本地目录
- 无需 API 配置即可安装（--install 在配置加载之前处理）

**MCP 升级**

- 协议版本 2024-11-05 → **2025-06-18**；只认旧版的服务器被拒时自动用 `2024-11-05` 重协商
- stdio 读取移入后台线程：服务器挂起时请求按配置超时返回错误，不再卡死整个回合（此前 `readline()` 会永久阻塞）
- 每服务器可配超时：`mcpServers` 配置新增 `"timeout"` 键（秒，默认 30）

**PyPI 发布流水线**

- 新增 `.github/workflows/release.yml`：推送 `v*` 标签 → 构建 sdist/wheel → wheel 冒烟（安装 + `--version` + fake LLM 自检）→ PyPI Trusted Publishing（OIDC，无需 API token；需在 PyPI 登记可信发布方）
- pyproject 元数据补全：classifiers / keywords / urls / authors（发布候选就绪）


## 0.15.0 (2026-09-28)

呈现层与内核韧性（v0.15 主题：**终端渲染 + 持久 shell + Provider 对称性**）：

**终端 Markdown 渲染（零依赖）**

- StreamRenderer 升级为完整终端 Markdown：标题（#/## 加粗着色）、无序/有序列表（`•` 与 ✓/○ 任务框）、引用（`│` 前缀）、分隔线、行内 **粗体**/*斜体*/`代码`/~~删除线~~/[链接](url) 转 ANSI
- GFM 表格：缓冲到表格结束后按显示宽度（CJK 全角计 2）对齐输出；过宽表格原样降级——宁可朴素不可错位
- 流式语义不变：只对完整行渲染；无色模式（NO_COLOR/非 tty）下内容逐字保留；围栏代码永不 markdown 化

**持久 shell 环境（bash）**

- `export` / `source activate` / `unset` 从此跨调用存活，与 cwd 持久化同一机制：ShellState 创建时抓取环境基线，每条命令尾部附加 `env -0 | base64 -w0` 转储做差分，下一条命令前重放变化项
- 转储与重放全链路 base64——导出脚本里永远只有 `[A-Za-z0-9+/=]`，值含引号/`$`/反斜杠/CJK 均无法破坏语法；`env -0` 的 NUL 字节绝不进入解码链（否则会被输出解码链误判成 UTF-16）
- 非法变量名（如 bash 导出的 `BASH_FUNC_x%%`）自动跳过；转储为空视为不可信直接跳过，绝不把「无转储」误判为「全部 unset」；powershell/cmd 静默降级（仅 cwd 持久化）

**Provider 对称性**

- Anthropic 路径补齐 OpenAI 路径的鲁棒性语义：thinking / tools / cache_control 被端点拒绝时逐级降级重试，流式不可用自动回退非流式（新增非流式事件解析，与流式同一套事件语义）；已产出事件后绝不重放
- 缓存 token 可见：OpenAI（`prompt_tokens_details.cached_tokens`）与 Anthropic（`cache_read/creation_input_tokens`）均解析；cache 计入上下文占用并单列展示（回合后 token 行与 /cost 显示「缓存命中」）
- `think`/`ultrathink` 关键词在 OpenAI 兼容端落地为 `reasoning_effort`（不再是空操作）；显式档位仍然优先，关键词更强时升档

**子智能体隔离与测试卫生**

- 每个子代理独立 ShellState：并行 dispatch_agents 不再竞争 cwd/env/后台进程
- SubUI 权限确认自动拒绝：子代理没有交互终端，不再从并行工作线程抢占 stdin
- 新增 `tests/conftest.py` autouse 夹具：会话落盘重定向到临时目录，测试不再污染真实 `~/.minicode/sessions`
- `/api/status` 的 provider 字段报告真实 provider 名（原先恒为配置值）

测试 264 → **283 项**（新增 `tests/test_v015_terminal.py`：渲染器结构/表格 CJK 对齐/围栏保护、env 差分与真实 bash 往返、cache 解析、非流式事件、SubUI 非交互）。


## 0.14.0 (2026-09-28)

手感层追平（v0.14 主题：**安全修复 + 中断/排队/粘贴**）——对齐 Claude Code 用户感知最强的日常交互：

**安全（P0）**

- **修复 Web UI 本地提权漏洞**：静态 app.js 此前免鉴权即可获取且内嵌真实 token，本机任意进程可借它调用 `/api/shell` 任意执行命令、经 `/api/fs/list` 遍历全盘。现改为 **HttpOnly cookie 鉴权**（`SameSite=Strict`，配合既有 DNS rebinding 校验与 127.0.0.1 绑定）：页面与静态资源零凭据，首访经 `?token=` 链接种下 cookie，`X-Minicode-Token` 头对程序化客户端继续可用
- **Web 回合可中断**：新增 `POST /api/stop` 与输入区停止按钮（busy 时出现）。停止时未答复的确认卡按「拒绝」落定，agent 线程不再被无限阻塞；前端同步清理挂起的签核卡
- `/api/command` 错误不再以 200 返回（LLMError → 400），前端正确显示错误而非「（完成）」

**终端交互（P1）**

- **Esc 中断回合**：回合执行期间按 Esc 立即停止——已流出的部分回复保留（标注「回复被用户中断」），已发出的工具调用全部回填结果，会话状态保持一致，下一回合安全开始。实现于 `agent.interrupt_event`（协作式中断），终端 Esc 与 Web 停止按钮共用同一条链路
- **消息排队**：回合进行中直接输入下一句话并回车即排队（低干扰提示「⏎ 已排队」），回合结束后自动逐条执行；队列消息同样支持 `/命令` 与 `!` 直通。POSIX 用 cbreak+select、Windows 用 msvcrt 轮询（`watcher.py`），与 confirm/choose 经 `INPUT_ACTIVE` 互斥，不抢 stdin
- **bracketed paste**：开启 `?2004` 模式，粘贴多行文本整段进入输入框，不再被拆成多条消息逐行触发回合；检测到旧版 readline/libedit 标记泄漏时自动关闭（自校正）
- **Windows 原生行编辑器**（`winedit.py`，msvcrt 零依赖）：无 pyreadline3 时获得完整行编辑——←→/Home/End、↑↓ 历史导航（落盘 `~/.minicode/history` 跨会话保留）、斜杠与 @文件 Tab 补全、多行粘贴（burst 检测）、CJK 宽度光标渲染、Esc 清空、Ctrl+U/Ctrl+W；readline 路径同步修复 ANSI 提示符导致的光标错位（`\001/\002` 包裹）与历史不落盘问题
- **read-before-edit 硬性强制**（对齐 Claude Code）：`edit_file`、`apply_patch` 的 Update/Delete 段、`write_file` 覆盖已有文件，必须先 `read_file`，未读先改直接拒绝并提示重读；工具描述同步注明

**修复与工程**

- **修复 JOB TICKET 三处失效样式**：`--inset` / `--bubble-border` 变量未定义（工单阴影与用户气泡描边整条失效）、`@keyframes pulse` 未定义（RUNNING 章不脉冲）；清理 `.newchat` 死选择器、`.send[hidden]` 显式还原隐藏语义（display 声明压过 UA 规则导致停止按钮常显）；文件头注释更新为双设计系统
- `_config_update` 忙检查加锁（原为无锁 TOCTOU）
- 测试 248 → **264 项**：新增 `tests/test_v014_interrupt.py`（16 项：协作式中断/部分回复保留/排队解析/read-before-edit/Web 停止链路）；加固 serve 测试端口就绪等待，消除全量跑时偶发连接拒绝


## 0.13.0 (2026-09-27)

设计系统升级：**JOB TICKET（执行工单）**——为 minicode 建立专属品牌语言，对话回合即工单：

- 回合工单化：每次用户请求开启一张新工单（编号 Nº 001 递增 + 时间戳 + 条码），回合内所有工具调用与回复归入同一张工单
- 状态戳：工单运行中显示脉冲 RUNNING，结束盖 DONE / ERRORS 章（随成败变色，轻微倾斜的印章质感）
- 工单之间以打孔撕裂线分隔（两端圆形打孔）——印刷车间工单的隐喻
- 确认卡升级为签核区：SIGN-OFF · 签核 标签 + 余烬色边框
- 修复版本号自 0.12.2 起未随提交更新的问题（__init__/pyproject 停在 0.12.1）→ 0.13.0
- 顺带修复：系统行不再误建空工单；历史回放按工单分组并保留错误标记


## 0.12.7 (2026-09-27)

暗色主题下的原生控件配色修复：

- 修复切换暗色主题后，四个权限模式下拉选项与设置弹窗中的原生控件（下拉列表/复选框/滚动条）仍是白底的问题
- 根因：未声明 CSS `color-scheme`，浏览器按浅色渲染原生控件；现暗色主题声明 `color-scheme: dark`、浅色声明 `light`
- 全局 `select option` 补充主题化背景与文字色（跨浏览器兜底），确认卡此前已主题化不受影响


## 0.12.6 (2026-09-27)

- 扩展详情全文改为自然文章渲染：Markdown 符号（#/」**「/-）转为真正的标题、加粗与编号列表，插件源码保持代码视图
- 清空全部历史会话（377 个测试积累文件 + 归档 + LAST 指针），侧边栏归零


## 0.12.5 (2026-09-27)

扩展详情查看——点击任意扩展条目即可查看完整介绍：

- `GET /api/ext/detail?type=<skills|plugins|agents|commands|mcp>&name=<名称>`
- 技能 / 命令 / 子智能体：frontmatter 元信息（描述/可用工具/模型）+ 全文内容
- 插件：源码全文与文件位置
- MCP：服务器状态 + 工具清单（名称/描述/参数概要）
- 详情弹窗含元信息表格（类型/名称/来源/描述/位置）与等宽全文视图；列表行悬停高亮提示可点击
- 扩展条目上的操作按钮（使用/运行/删除）不影响详情触发


## 0.12.4 (2026-09-27)

扩展面板改分页签管理——五类扩展分离开，内容再多也井然有序：

- 页签栏：技能 / 插件 / 子智能体 / 命令 / MCP 五个页签独立切换，页签上显示当前数量徽标
- 筛选框：按名称或描述实时过滤当前页签内容，扩展多了也能秒定位
- 每个页签独立的新建 / 导入 / 重载 / 使用 / 删除操作，互不干扰
- 纯前端重构，接口无变化


## 0.12.3 (2026-09-27)

原生资源管理器选工作区 + 扩展直接导入（应需求重构交互）：

- 工作区选择：「浏览目录…」弹出 **Windows 原生文件夹选择对话框**（tkinter 标准库实现，任意盘符/任意位置，置顶显示），选定即添加并切换工作区；本机缺 tkinter 时自动回退到网页目录浏览
- 扩展直接导入：技能/插件/自定义命令/子智能体四个区块均新增「导入」按钮——弹原生文件选择器（支持多选），服务端校验类型后拷入工作区 `.minicode/` 对应目录并自动热重载：
  - 技能：导入含 SKILL.md 的文件夹（整个目录连同资源一起拷贝）或单个 .md
  - 插件：导入 .py 文件（可多选）
  - 命令 / 子智能体：导入 .md 文件（可多选）
- 同名冲突跳过并提示；`POST /api/fs/pick`（原生对话框，串行化防多弹）与 `POST /api/ext/import`（类型校验 + 结果明细）


## 0.12.2 (2026-09-27)

界面排版重塑——参考 ChatGPT / Claude / ZCode 的厂商共识模式（联网调研 + 双主题浏览器验证）：

- 侧边栏：「扩展」按钮移至「新会话」上方；新会话改为余烬色主按钮（每屏唯一主 CTA）
- 会话历史按日期分组（今天 / 近 7 天 / 更早），服务端会话列表补 ts 时间戳
- 模式选择 / 连接状态 / token 用量从独立状态栏迁入输入卡底部控制行（厂商主流形态）；模型徽标改为可点击（直达模型设置）
- 移除独立状态栏，顶栏只保留 会话列表 / 上下文余量 / 主题 / 压缩
- 修复工作区图标未限宽导致撑爆侧边栏的问题


## 0.12.1 (2026-09-27)

工作区选择器与扩展自由增删使用（浏览器全流程验证）：

- 服务端目录浏览器：工作区弹窗「浏览目录…」打开逐级导航弹窗（驱动器/上一级/跳转/选择此目录），选定即添加并切换工作区——不再需要手输绝对路径
- 扩展自由增删使用：
  - 技能：+ 新建（名称/描述/Markdown 内容表单）、使用（发起回合让模型加载）、删除（内置技能受保护）
  - 插件：+ 新建（生成模板文件）、删除、重载（热刷新注册表，无需重启）
  - 自定义子智能体：+ 新建（描述/tools/model/系统提示词）、删除
  - 自定义命令：+ 新建、运行（填入输入框以 / 触发）、删除
- POST /api/ext/<type>/<create|delete> + /api/ext/reload；名字 slug 白名单校验，写入仅限工作区 .minicode/** 与 ~/.minicode/**


## 0.12.0 (2026-09-27)

智能体本体升级——并行多子智能体 + 扩展体系可视化，Web 与终端功能完全对齐：

并行多子智能体：
- 新增 `dispatch_agents` 工具：一次调用并行分派 2-6 个独立任务给子智能体（线程池，最多 4 并发），单任务失败不影响其他，报告按任务标注汇总
- 系统提示词教会模型何时并行分派（多个独立调研同时发出，而非串行排队）；子智能体仍为只读且不可嵌套派生（安全边界不变）

扩展体系可视化（Web 端「扩展」面板）：
- 技能（内置/用户/项目三级来源标注）、插件工具、自定义子智能体、自定义命令（可点击直接运行）、MCP 服务器状态——五类扩展一目了然
- `GET /api/extensions` 提供结构化目录

终端功能对齐核查：/api/command 补齐 /output-style /doctor /stats，加上此前的 24 条，终端全部斜杠命令在 Web 均可用（/copy 由消息悬停复制承载，/resume 由侧边栏承载，/exit 对 Web 无意义）

测试 241 → 245（并行分派/失败隔离/注册表/扩展接口）。

## 0.11.1 (2026-09-27)

按 ui-ux-pro-max 设计规则库对 Web 界面做无障碍与触控审计修复（优先级 CRITICAL→MEDIUM 全覆盖）：

可访问性（CRITICAL）：
- 弹窗支持 Escape 关闭并返还焦点到输入框（modal-escape / focus-management）
- 全部图标按钮补 aria-label（aria-labels）；#chat 加 role="log" aria-live="polite"，新消息对读屏器可感知
- `--faint` 色提亮：暗色 #6b6558→#8a8375、浅色 #a8a294→#7a7466，小号元数据文本对比度达 ≥4.5:1（此前约 2.6–3.4:1）

触控（CRITICAL）：
- 触屏设备（hover: none）下会话重命名/归档/删除按钮常显，不再依赖悬停（hover-vs-tap）
- 触控目标扩展至 ≥40px（图标按钮/会话操作/主按钮，touch-target-size）；全局 touch-action: manipulation

表单与细节（MEDIUM）：
- API Key 输入框增加显示/隐藏切换（password-toggle）
- ⚙/⇅ emoji 图标换成内联线条 SVG（no-emoji-icons）；会话行截断补完整 title 提示
- Markdown 表格加横向滚动容器（窄屏不破版）；移动端输入框 16px 防 iOS 聚焦自动放大
- 任务列表悬停位移从 padding 动画改为 transform（transform-performance，不触发 reflow）

## 0.11.0 (2026-09-27)

Web 界面产品化——会话管理、多工作区、模型 API 配置、终端功能全量平移（浏览器双主题验证）：

会话管理（侧边栏）：
- 悬停操作：重命名（✎）、归档（▣）、删除（✕，二次确认）；侧边栏「已归档」分组 + 恢复
- 接口：`/api/session/archive|unarchive|rename|delete`（名字白名单校验）

多工作区：
- 侧边栏「工作区」区 + 管理弹窗：添加（绝对路径）/ 切换 / 移除，持久化于 `~/.minicode/workspaces.json`
- 切换工作区重建 agent（系统提示、brain、shell 状态、检查点全部随工作区刷新）并开启新会话
- `workspace_lock`：Web 模式下智能体写操作硬拒绝工作区（及 /add-dir 授权目录）之外的路径，yolo 也不例外（bash 无法静态判定，与终端同一局限）

模型 API 配置：
- ⚙ 设置弹窗：提供方 / Base URL / API Key（脱敏显示 ****tail，留空不改）/ 模型 / max_tokens / 上下文上限 / 思考力度
- 保存后重建 provider 并保留当前会话与检查点；可选勾选持久化到 ~/.minicode.json（含密钥需主动勾选）
- 「测试连接」按钮一键运行端点四项探测（models / 非流式 / 流式 / 工具调用）

终端功能全量平移：
- `/api/command` 分发器：mode/undo/rewind/diff/limit/reasoning/cost/context/tools/todos/brain/memory/export/transcript/plans/agents/skills/mcp/model/models/add-dir/verify/help 等 27 条命令，输出以系统行回显
- turn 型命令（/init /commit /pr /review 及自定义命令）自动转为回合执行；/rewind 弹出回退点选择器
- `!命令` 直通本地执行（`/api/shell`）；输入框 `/` 前缀实时命令提示
- 计划模式批准条：计划生成后一键「批准并实施」（等价 REPL 的 Enter 门）

测试 236 → 241（会话管理 / 命令分发 / 配置 / 工作区 / 边界锁定）。

## 0.10.3 (2026-09-26)

界面去 AI 味、立品牌——「Ink & Ember」设计系统：近单色 + 发丝线 + 编辑感排版，产品化骨架补齐（浏览器双主题逐屏验证）：

- 视觉身份：品牌符号定为终端提示符 `❯`（余烬色方块 mark），主题「墨 & 余烬」——暖石墨暗色 / 暖象牙浅色，强调色收敛为陶土橙一色
- 去光响化：移除极光背景、渐变标题、流光文字、呼吸光环、玻璃模糊等全部装饰渐变，只留发丝线分割与精准留白
- 产品骨架：新增历史会话侧边栏（`/api/sessions` 列表 + `/api/session/open` 打开回放，名字白名单防路径穿越）与 IDE 式底部状态栏（连接态 / 权限模式 / token 用量）
- 欢迎页改编辑风：衬线大标题 + 等宽 eyebrow + 编号任务列表（发丝线行，悬停余烬色箭头）
- 工具调用改「执行日志」形态：等宽芯片图标 + 方块轨道节点；确认卡左侧余烬色竖线；思考面板简化为左线 + 三点呼吸 + 等宽计时（`已深度思考 · 0.1s`）
- 表头改等宽大写、代码块头等宽大写语言标签、消息元数据等宽时间戳——元数据全部等宽化
- 移动端：侧边栏抽屉化（≤880px），汉堡按钮唤出

## 0.10.2 (2026-09-26)

界面设计系统重定——以「Graphite & Electric Violet」建立 minicode 自己的视觉身份，暗色默认，浏览器双主题逐屏验证：

- 全新氛围层：极光色斑背景（智能体工作时呼吸变亮）+ 顶部点阵网格淡出，内容玻璃分层
- 暗色默认主题（石墨黑 #0a0a10 + 电光紫罗兰 #7c8aff→#b48cff 渐变），浅色主题重调为暖纸色
- 深度思考面板签名：思考中显示旋转渐变流光边框（conic-gradient mask），完成自动折叠
- 工具调用改为「活动轨道」：卡片左侧时间轴圆点 + 连接线，运行中节点脉冲——智能体执行轨迹一目了然
- 流式回复带发光打字光标；标题渐变色流动；欢迎页 logo 呼吸光环
- 全部按钮/卡片带内嵌高光（inset highlight）与投影层次；发送按钮等待态变轨道旋转环
- 表格升级圆角容器 + 斑马行；滚动条/选中色/焦点环统一入设计系统
- prefers-reduced-motion 与移动端（≤640px 收起轨道）适配

## 0.10.1 (2026-09-26)

Web 界面全面重设计——对标 DeepSeek 聊天界面的克制与质感（明暗双主题，浏览器内逐屏视觉验证）：

- 浅色为默认主题，一键切换深色（偏好记忆在 localStorage），全套色彩/阴影/圆角变量化
- DeepSeek 式深度思考面板：流光"深度思考中…"动画 → 完成后自动折叠为"已深度思考（用时 X 秒）"，点击展开
- 工具运行流光状态：卡片边框高亮 + 摘要流光动画，结果回填后按行数决定自动展开（≤8 行）或折叠
- 全部图标从 emoji 换成内联线条 SVG（闪电 logo/月亮太阳/层级/加号），工具图标改为单色字形芯片——Windows 不再出现彩色 emoji
- 欢迎页重做：渐变品牌标识 + 渐变标题 + 四个可点击任务建议 chips
- Markdown 渲染补齐 GFM 表格（条纹行、表头底色）；助手消息悬停出现"复制"按钮
- 输入卡片聚焦态蓝色描边 + 投影，发送按钮脉冲等待态
- FakeProvider 支持 `reasoning` 字段：离线演示也能展示思考面板
- 浏览器实测通过：欢迎页/工具卡片/错误归因/确认卡片/思考面板/表格代码块/双主题

## 0.10.0 (2026-09-26)

浏览器 Web 界面——对标桌面端聊天体验，仍保持零依赖（标准库 HTTP 服务 + 原生前端，无 Node/构建链）：

- `minicode --ui` 一条命令启动：本地 HTTP 服务 + 自动打开浏览器（http://127.0.0.1:8765）
- 暗色聊天流界面：流式回复逐字渲染、思考过程可折叠、自研 Markdown 渲染（代码块高亮容器 + 复制按钮，全量转义防 XSS）
- 工具调用卡片：按工具类型着色（读/写/命令/meta/MCP），点击展开完整输出，与终端的 `●/⎿` 体验对齐
- 浏览器内交互确认：写文件/命令的 `允许 / 本次总是 / 拒绝`、敏感路径与高危命令确认、ask_user 选项卡片（含多选与自由输入）直接在页面点选——agent 线程阻塞等答复，语义与 REPL 的 input() 一致
- 头部控制台：权限模式切换、上下文余量条（按占用变色）、累计用量、🧹 新会话 / 🗜 压缩
- 断线自愈：SSE 自动重连 + 页面加载时从 /api/messages 回放历史；连接状态指示灯
- 实现要点：`WebBridgeUI` 把所有 UI 调用转成 JSON 事件广播（自动剥离 ANSI 色码），回合复用 REPL 的 `_run_turn`（会话保存/自检门禁/复盘/自动压缩行为完全一致）；SSE 心跳、`hmac.compare_digest` token 鉴权、Host 校验防 DNS rebinding
- 新增 10 项测试覆盖事件桥接/静态页/鉴权/回合事件流/浏览器确认流（235 项全绿）

## 0.9.3 (2026-09-26)

代码审查闭环——补齐信任边界的敞口，收紧两处授权粒度：

安全（P1）：
- 项目配置 `.minicode.json` 受限字段门禁：`api_key`/`base_url`/`mcpServers`/`hooks`/`permissions`/`verify_command`/`extra_body`/`webfetch_allow_private` 不再自动生效（克隆恶意仓库不再能静默重定向你的 API 流量、改写权限或执行任意命令），首次遇到经项目信任门禁确认后应用；其余安全字段（model/context_limit 等）保持自动生效
- 信任标记按内容指纹记录：插件文件或受限配置被改动后自动失效并重新询问（旧版信任一次终身有效）

授权与提示注入面（P2）：
- bash「本次总是」改为按首词前缀授权（批准 `npm install` 只放行 `npm` 开头的命令），且破坏性命令（`rm -rf` 等）即使命中前缀也强制确认
- `brain_write` 从 meta 改为 write 类：写入项目大脑需确认（防提示注入借大脑跨会话存活）；系统提示注入大脑时标注来源为智能体自动写入、需验证
- 破坏性命令模式补齐：`curl … | sh` 管道执行远程脚本、`find … -delete`、`… | xargs rm`

工程（P3）：
- apply_patch 拒绝同一文件的重复段落（原来第二个 Update File 段会静默丢失改动）
- `~/.minicode/checkpoints/` 自动只保留最新 20 个会话目录
- 移除 cli.py 模块级全局 `args_state`（--append-system-prompt 走 Config）；serve token 比较改 `hmac.compare_digest`；系统提示构建不再重复调用 git 子进程
- 新增 17 项测试（受限配置门禁/信任指纹失效/前缀授权/大脑确认/重复段落/检查点清理）

## 0.9.2 (2026-08-30)

智能体行为优化——让模型更少踩坑、更高效利用上下文：
- 工具结果智能摘要：read_file 按行折叠中段、grep 按文件分组保留前 N 匹配、bash 保留首尾+错误行、list_dir/glob 计数摘要；未注册工具回退通用截断
- 工具失败归因反思：识别文件不存在/权限拒绝/参数错误/超时/stale-edit 等错误类型，自动附加可操作的修正建议；同类错误连续 3 次强制提示换策略
- 上下文压缩语义感知：microcompaction 按工具重要性区分保留比例——write/edit 保留 800 字符、bash/read 保留 300、只读列表保留 180、错误结果统一保留 400
- 新增 19 项测试覆盖三项优化

## 0.9.1 (2026-08-30)

代码审查闭环修复与工程化补强：
- 修复 server.py /api/clear 与 /api/compact 的 NameError（改用 outer.agent）
- 清理 shell.py BashOutputTool 重复的 input_schema 死代码
- 统一 mcp.py 版本号从 __version__ 取（消除 stdio/HTTP 三处不一致）
- 新增 ruff 配置（E/F/W，py39 目标）与 lint CI job；ruff 抓到 2 个潜伏 F821 未导入 bug 并修复
- 测试 CI 加 pytest-cov 覆盖率报告
- 测试文件从版本号命名重命名为功能域命名（git mv 保留历史）
- 新增 --no-save / "save_sessions": false 隐私模式，会话完全不落盘
- 新增 MINICODE_DEBUG=1 结构化日志（~/.minicode/debug.log），serve 崩溃异常入日志
- web_search 失败时明确提示改用 web_fetch 或检查代理
- fake demo 改为纯只读流程，零文件副作用
- 文档同步：README 行数/架构图/隐私模式/斜杠命令更新

## 0.9.0 (2026-08-30)

化用 ZCode 的设计：Skills 系统（内置系统化调试/TDD/交付前验证/写计划四技能 + 项目自定义）、
exit_plan 预授权（allowed_prompts）、ask_user 自由输入与选项预览、bash_output 阻塞等待、
todo 优先级、post-hook 反馈回喂、Git 安全护栏入系统提示。

## 0.8.x

安全加固：敏感路径门禁（yolo 也不放行）、web_fetch SSRF 防护、插件/钩子首次信任门禁、
CRLF 修复、后台进程回收、serve 500、POINTER 校验、提示注入防御、LICENSE。

## 0.7.x

完全访问模式、上下文余量条（默认 1M，/limit 自定义）、!命令直通、@文件提及、
命令前缀匹配、/copy、/commit、/pr、计划存档、复盘自动入脑、结构化压缩、
serve 本地 API、本地 Python 插件、MCP HTTP。

## 0.6.x

多模型适配层（429 退避、thinking 回传、参数垫片、非流式回退）、/models /probe。

## 0.5.x

项目大脑、自检回路、圆桌模式、--budget、断点续跑。

## 0.1–0.4

初始实现：Agent 循环、双协议适配、16 工具、权限体系、检查点、并行执行、apply_patch。
