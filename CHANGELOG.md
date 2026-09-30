# Changelog

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
