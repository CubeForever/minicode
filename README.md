# minicode ⚡

终端里的编码智能体，能力对标 Claude Code / Codex CLI，并有三项独创设计。**零第三方依赖**（纯 Python 标准库，≥ 3.9），适配所有 OpenAI 兼容 API（GLM / DeepSeek / Kimi / Qwen / OpenAI / Ollama / 各类中转站）与 Anthropic API。

当前状态：**v0.18.0 · 21 个内置工具 · 并行多子智能体 · 终端 REPL + 浏览器 Web 界面（会话/工作区/模型配置/扩展全管理） · 321 项自动化测试 · CI 矩阵 9/9 全绿（ubuntu/macos/windows × Python 3.9/3.10/3.12） · MIT 开源**

## 它能做什么

- **多轮自主任务**：给它一句话，它自己读代码 → 改代码 → 跑测试 → 汇报结果；流式输出、思考过程可视化、**Esc 随时中断**（已生成的部分回复保留）
- **回合中排队**：智能体工作时直接输入下一句话并回车，回合结束自动接着执行——不用干等
- **终端原生 Markdown 渲染**：回复按结构渲染——标题、列表（含任务框）、引用、分隔线、行内样式，GFM 表格按 CJK 显示宽度对齐；零依赖实现，无色模式内容逐字不丢
- **并行多子智能体**：`dispatch_agents` 一次分派 2-6 个独立调研任务并行执行（独立 cwd/env 隔离），单任务失败不影响其他；配合自定义子智能体（.minicode/agents/*.md）按角色分工
- **浏览器 Web 界面**：`minicode --ui` 一条命令在浏览器里获得桌面级体验——JOB TICKET 工单设计语言（明暗双主题）、回合编号与状态戳、工具调用执行日志、深度思考折叠面板、权限签核按钮、停止按钮、分页签扩展管理（仍是零依赖：标准库 HTTP 服务 + 原生前端，无 Node/构建链）
- **21 个内置工具**：文件读写编辑（先读后改硬性强制）、多文件补丁（apply_patch）、Jupyter 编辑、glob/grep/list 搜索、bash / PowerShell / cmd（后台进程管理，环境变量跨调用持久）、单/并行事智能体、三视角圆桌、网页抓取/搜索、交互提问、任务清单
- **四种权限模式**：`default`（写操作逐个确认）→ `accept-edits`（自动接受编辑）→ `plan`（只读调研出计划，批准后实施）→ `full-access`（全自动，仅高危操作需确认）
- **跨会话记忆**：项目大脑自动沉淀事实/坑/决策/失败教训，失败回合自动复盘入脑，越用越懂你的项目
- **自检门禁**：`/verify pytest -q` 后每次改动自动跑验收命令，失败自动修复（最多两轮）
- **多模型适配层**：429 退避、思考模型回传、参数垫片、非流式回退、工具调用格式归一化——OpenAI 兼容端与 Anthropic 同一套鲁棒性语义；`think/ultrathink` 关键词落地生效、缓存命中可见。换任意端点先跑 `--probe` 诊断
- **模型故障转移**：`fallbacks` 配置备用模型链，主模型连接失败/限速耗尽/不支持工具时自动切换继续同一回合（可跨厂商，切换实时提示，部分输出绝不重放）
- **平台化**：`serve` 本地 HTTP API、本地 Python 插件目录、MCP（stdio + HTTP，含 resources/prompts）、hooks 护栏、扩展市场、自定义命令/子智能体/技能、headless JSON 输出
- **工程保险**：文件检查点（/undo /rewind）、会话持久化、上下文自动压缩（默认 1M，/limit 自定义）、敏感路径门禁、SSRF 防护
- **智能体行为优化**：工具结果按类型智能摘要（大文件按行折叠/grep 按文件分组/bash 保留错误行）、工具失败自动归因并给出修正建议、同类错误连续 3 次强制换策略、上下文压缩按工具重要性区分保留比例（写操作比读操作保留更多）

## 优势

- **零依赖、全部可读**：核心约 10100 行纯标准库代码，没有黑盒。想加工具是 100 行的事，想改任何行为都有据可查
- **不锁定模型**：一个环境变量切换 GLM / DeepSeek / Claude / 本地 Ollama；兼容层在真实中转站联调中打磨，新端点的怪癖大多自动消化
- **安全边界内建**：敏感路径强制确认（yolo 也不放行）、SSRF 封禁、第三方插件/钩子/项目受限配置首次信任确认（内容变更后重新询问）、危险命令告警
- **三项独创**（Claude Code / Codex 均无）：项目大脑（Brain）、自检回路（Verify Gate）、圆桌模式（Panel）

## 局限性（诚实清单）

- 提示注入无法被任何外壳 100% 消除——已做门禁/SSRF 封禁/系统提示防御三层缓解，不可信代码库请用 plan/default 模式
- `serve` 模式本质是以你的用户身份执行命令（仅本机 + token 鉴权），不要暴露到公网
- bash 无系统级沙箱（Windows 无可移植方案），用敏感路径门禁替代
- 对话历史默认明文存于 `~/.minicode/sessions/`（可用 `--no-save` 或配置 `"save_sessions": false` 完全关闭落盘）；图片输入依赖视觉模型；无 vim 键位与图片粘贴

## 下载安装

**方式一：一条命令安装（推荐，无需克隆）**

```bash
pip install git+https://github.com/CubeForever/minicode.git
```

装完在任意目录可用 `minicode` 或 `python -m minicode`。升级 `pip install -U git+...`；卸载 `pip uninstall minicode`（配置与历史会话保留）。

**方式二：克隆源码（便于参与贡献 / 用最新代码）**

```bash
git clone https://github.com/CubeForever/minicode.git
cd minicode
pip install -e .[dev]        # [dev] 额外装 pytest / ruff / pytest-cov；python -m pytest tests -q 验证
```

**前置要求**：Python ≥ 3.9（Windows / macOS / Linux 均可；Windows 装 Git 即自带所需的 bash）。
**不想装？** Download ZIP 解压后在解压目录内 `python -m minicode` 也能跑。

## 快速开始

```bash
# OpenAI 兼容（例：智谱 GLM）
export OPENAI_API_KEY=你的密钥
export OPENAI_BASE_URL=https://open.bigmodel.cn/api/paas/v4
export OPENAI_MODEL=glm-4.6

# 或 Claude
export ANTHROPIC_API_KEY=你的密钥

# 没有密钥？离线体验：
MINICODE_FAKE_LLM=demo minicode -p hi --yolo
```

```bash
minicode                        # 交互式 REPL
minicode --ui                   # 浏览器 Web 界面（自动打开 http://127.0.0.1:8765）
minicode "修复登录 bug"          # 启动即执行
minicode -c                     # 恢复上次会话
minicode --probe                # 实测端点四项能力（装新模型先跑这个）
minicode --install <git仓库|目录>  # 安装扩展包（技能/命令/子智能体/插件）
minicode -p "总结项目" --output-format json < task.txt   # headless
```

### 扩展包分发（--install）

把一个 git 仓库（或本地目录）作为「扩展包」一键装进项目 `.minicode/`（`--user` 装入 `~/.minicode/`）：

```bash
minicode --install https://github.com/someone/minicode-pack   # 装到当前项目
minicode --install ./my-pack --user                           # 装到用户级
```

包内按约定目录发现：`skills/`（含 SKILL.md 的子目录）、`commands/*.md`、`agents/*.md`、`tools/*.py`；也可在包根放 `minicode.json` manifest 显式声明。安全边界与手工复制一致：安装只复制文件，插件首次加载强制确认，manifest 携带的 MCP 服务器不会自动安装。

### 扩展市场（--market）

`minicode --market` / REPL 里 `/market` 浏览可安装扩展包（名称/版本/描述/来源），`minicode --install <包名>` 自动从市场解析安装源。索引源由 `~/.minicode.json` 的 `"marketplaces"` 列表配置（缺省内置社区索引，支持本地路径自建）；市场只提供目录，安装与执行仍走完整信任体系。

### Hooks（自动化护栏）

在配置里挂钩子，在关键节点自动执行你的命令（shell 执行，stdin 收到 JSON 事件）：

```json
{"hooks": {
  "pre_tool_use": [{"matcher": "Bash", "command": "my-guard.py", "timeout": 10}],
  "stop": "notify-done.sh",
  "pre_compact": "log-compact.sh"
}}
```

支持 8 个事件：`session_start` / `user_prompt_submit` / `pre_tool_use` / `post_tool_use` / `stop` / `subagent_stop` / `pre_compact` / `turn_end`。`matcher` 按工具名正则过滤（仅工具事件）；命令退出码非 0 或输出 `{"decision": "block", "reason": "…"}` 即阻断并把 reason 反馈给模型自动改道；pre_tool_use 可输出 `{"decision": "approve"}` 跳过标准确认——但 deny 规则、敏感路径门禁与 plan 只读不受影响，hook 永远不能放宽安全边界。

### Web 界面（`--ui`）

对标桌面端聊天体验，浏览器打开即用，会话行为与终端 REPL 完全一致（同一套权限门禁、检查点、自检门禁、复盘记忆）：

| 能力 | 说明 |
|---|---|
| 会话侧边栏 | 历史会话重命名 / 归档 / 删除 / 回放，移动端抽屉式 |
| 多工作区 | 添加 / 切换 / 移除工作区目录，智能体写操作锁定在当前工作区内 |
| 模型 API 配置 | ⚙ 弹窗改提供方 / Base URL / 密钥（脱敏）/ 模型 / 参数，一键测试连接，可选持久化 |
| 流式聊天 | 回复逐字渲染（终端方块光标）、思考面板（等宽计时 + 折叠）、Markdown/表格/代码块复制 |
| 执行日志 | 工具调用按时间轴排布，等宽芯片 + 可展开输出，错误自动归因可见 |
| 浏览器内确认 | 写文件/命令的 `允许 / 本次总是 / 拒绝` 与 ask_user 选项直接在页面点选 |
| 终端命令平移 | 输入框直接用 `/命令`（28 条）与 `!命令` 直通；/init /commit /pr 自动转回合；计划一键批准实施 |
| 状态栏 | IDE 式底部栏：连接态、权限模式切换、token 用量实时显示 |

安全边界与 `serve` 模式相同：仅绑定 127.0.0.1、HttpOnly cookie 鉴权（首访经 `?token=` 链接种下，SameSite=Strict 防 CSRF）、Host 校验防 DNS rebinding、页面与静态资源零凭据；不要暴露公网。

### 常用服务商

| 服务商 | base_url | model 示例 |
|---|---|---|
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` | `glm-4.6` |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
| Kimi | `https://api.moonshot.cn/v1` | `kimi-k2-0905-preview` |
| OpenAI | （默认） | `gpt-4o` |
| Anthropic | （默认） | `claude-sonnet-4-5` |
| Ollama 本地 | `http://localhost:11434/v1` | `qwen3:8b`（key 随便填） |

### 模型故障转移

在 `~/.minicode.json` 配置备用模型链，主模型失败（连接/限速耗尽/不支持工具等）自动切换继续同一回合：

```json
{
  "provider": "openai", "base_url": "https://open.bigmodel.cn/api/paas/v4",
  "api_key": "...", "model": "glm-4.6",
  "fallbacks": [
    "glm-4.6-air",
    {"provider": "anthropic", "model": "claude-sonnet-4-5", "api_key": "..."}
  ]
}
```

字符串即模型名（端点/密钥继承主配置）；对象可指定独立 provider/base_url/api_key 实现跨厂商切换。切换过程实时提示；`/cost` 与回合 token 行显示缓存命中率。

## 使用指南

### 快捷交互

| 操作 | 说明 |
|---|---|
| `Esc` | 回合执行中按 Esc 立即中断（已流出的部分回复保留）；输入中按 Esc 清空当前行 |
| 回合中直接输入 | 智能体工作时输入消息并回车即排队，回合结束自动逐条执行（支持 `/命令` 与 `!直通`） |
| 粘贴多行 | bracketed paste：整段粘贴为一个输入，不再逐行误触发回合 |
| `!cmd` | 命令直通本地执行，不经过模型 |
| `@文件` | 把文件内容/图片附加给模型 |
| `/yolo` `/plan` `/edits` | 一键切模式 |
| `/comp` 等前缀 | 唯一匹配自动补全 |
| `/model 2` | 按序号切模型；`/model deepseek` 模糊匹配 |
| `/copy` | 复制上条回复到剪贴板 |
| `think / ultrathink` | 提示词含关键词自动加大思考预算 |
| 行尾 `\` | 多行续写 |

> Windows 下无 pyreadline3 也没关系：内置自研行编辑器（历史导航、Tab 补全、多行粘贴、CJK 光标），输入历史落盘 `~/.minicode/history` 跨会话保留。

### 斜杠命令

`/help` `/clear` `/mode` `/undo` `/rewind` `/diff` `/compact` `/verify` `/brain` `/memory` `/limit` `/context` `/cost` `/stats` `/model` `/models` `/probe` `/reasoning` `/tools` `/status` `/doctor` `/agents` `/skills` `/mcp` `/prompt` `/market` `/add-dir` `/plans` `/todos` `/transcript` `/output-style` `/resume` `/export` `/init` `/commit` `/pr` `/copy` `/exit`

### 自定义扩展

- 技能：`.minicode/skills/<名字>/SKILL.md`（模型按需加载的工作流）
- 命令：`.minicode/commands/<名字>.md`（`$ARGUMENTS` 接参）
- 子智能体：`.minicode/agents/<名字>.md`（可指定 tools/model）
- 工具插件：`.minicode/tools/<名字>.py`（函数签名即 schema）

### 诊断

- `/doctor`：环境自检（Python / shell / API / 记忆文件 / 补全）
- `--probe` 或 `/probe`：实测端点兼容性（模型列表 / 非流式 / 流式 / 工具调用）
- 环境变量 `MINICODE_DEBUG=1`：把运行日志写入 `~/.minicode/debug.log`（排查 serve / 远程调用问题时使用）
- `--no-save` 或配置 `"save_sessions": false`：隐私模式，会话历史完全不落盘

## 架构

```text
minicode/
├── cli.py          REPL / 斜杠命令 / 计划批准门 / 回合排队
├── agent.py        主循环：权限、门禁、hooks、检查点、思考预算、协作式中断
├── llm.py          双协议适配 + SSE 归一化 + 重试 + 故障转移 + 缓存
├── config.py       配置加载 / profiles / 权限合并 / MCP 配置
├── mcp.py          MCP 客户端（stdio / HTTP，tools + resources + prompts）
├── session.py      会话 / 压缩 / 持久化
├── checkpoints.py  文件检查点（undo / rewind）
├── guard.py        敏感路径门禁
├── prompts.py      系统提示词（注入防御 / Git 护栏）
├── plugins.py      本地插件加载
├── install.py      扩展包安装（git / 本地目录 → .minicode）
├── market.py       扩展市场索引（只读目录）
├── plans.py        计划存档管理
├── server.py       serve 模式（headless 本地 HTTP API）
├── webui.py        Web 界面（SSE 事件流 + 浏览器内确认 + cookie 鉴权）
├── web/            前端静态资源（原生 HTML/CSS/JS，零构建）
├── ui.py           终端渲染 / Markdown / 余量条 / 确认框
├── lineinput.py    行编辑（readline / Windows 原生编辑器 + 历史落盘）
├── winedit.py      Windows 行编辑器（msvcrt，零依赖）
├── watcher.py      回合键盘监听（Esc 中断 / 消息排队）
├── fake.py         离线假模型
└── tools/          21 个工具实现
```

## 测试

```bash
python -m pytest tests -q    # 321 项，覆盖协议解析/工具/安全/全功能链路/Web 界面
```

## 安全与信任边界（使用前必读）

- **提示注入无法被外壳 100% 消除**：已做三层缓解（敏感路径门禁、SSRF 封禁、系统提示防御），不可信代码库请用 plan/default 模式
- **项目配置 `.minicode.json` 的受限字段需信任确认**：克隆来的项目配置里，`api_key`/`base_url`/`mcpServers`/`hooks`/`permissions`/`verify_command`/`extra_body`/`webfetch_allow_private` 不会自动生效（防止恶意仓库重定向你的 API 流量、弱化权限或执行任意命令）——首次遇到经确认，信任后按内容指纹记录，内容变更会重新询问；`model`/`context_limit`/`timeout` 等安全字段自动生效
- **`serve` = 以你的身份执行命令**：仅绑定 127.0.0.1 + token 鉴权，切勿暴露公网
- **对话历史默认明文存于 `~/.minicode/sessions/`**（可用 `--no-save` 或 `"save_sessions": false` 完全关闭落盘）：让模型读过密钥文件就会落盘
- **第三方插件/钩子是任意代码**：首次遇到强制确认，非交互默认不加载
- 完整威胁模型与缓解措施见下文安全小节与 `guard.py`

## 参与贡献

欢迎所有人一起迭代——零依赖 + 小代码库，每个人都能读完整个实现。路径由易到难：

1. 技能/命令/子智能体（纯 Markdown）
2. 多模型兼容报告（跑 `--probe` 提 issue，最急需）
3. 新工具（约 100 行，教程在 CONTRIBUTING）
4. 跨平台测试（macOS/Linux 跑 `pytest tests -q` 报告）
5. 核心改进（先开 issue 讨论）

详见 **[CONTRIBUTING.md](CONTRIBUTING.md)**。规则：一个 PR 一个主题、新功能必带测试、不加第三方运行时依赖、密钥不进仓库。

## 变更历史

见 [CHANGELOG.md](CHANGELOG.md)。
