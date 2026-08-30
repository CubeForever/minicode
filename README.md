# minicode ⚡

对标 Claude Code 与 Codex CLI 的终端编码智能体，融合两者能力，并有三大独创特色与完整平台化能力。**纯 Python 标准库实现，零第三方依赖**（Python ≥ 3.9），支持 OpenAI 兼容 API（GLM / DeepSeek / Kimi / Qwen / OpenAI / Ollama…）与 Anthropic API。

## 三大独创特色（CC / Codex 都没有）

### 🧠 项目大脑（Project Brain）
智能体干活时自动把**跨会话有用**的知识沉淀到 `.minicode/BRAIN.md`——项目事实（构建/测试命令）、坑（gotcha）、决策（为什么这么做）、失败尝试（别再走这条路）。每个新会话自动继承，越用越懂你的项目。**失败回合还会自动复盘入脑**（机械提取，零额外成本）。`/brain` 查看、`/brain clear` 重置。

### ✅ 自检回路（Self-verify Gate）
`/verify pytest -q` 一条命令开启硬门禁：之后**每次改动文件自动跑验收命令**，失败就把报错喂回给智能体自动修复（最多两轮），通过才交付。

### 🗣 圆桌模式（consult_panel）
难题不一条路走到黑：**并行**派出 3 个独立视角的子智能体——务实派、架构派、风险猎手——各写分析报告；`debate=true` 开启**辩论轮**，各视角互看对方报告后反驳修正，再综合出结论。

## 平台化能力（v0.7.0）

- **敏感路径门禁**：`.env` / `.git/` / `*.pem` / `id_rsa` 等关键资产的写入/删除强制确认（yolo 也不放行，非交互自动拒绝）；命令 `rm -rf .git`、`echo x > .env` 同样被拦
- **计划存档**：plan 模式批准的计划自动存 `.minicode/plans/`，活跃计划注入每个后续会话（"上次的方案执行到哪了"），`/plans` 管理
- **Git 集成**：`/commit`（感知本会话改动，生成 Conventional Commits 并提交）、`/pr [base]`（生成 PR 描述，gh 可用则直接创建）
- **`minicode serve`**：本地 HTTP API（127.0.0.1 + token 鉴权），编辑器插件/CI/脚本可调用 `POST /api/turn`——从工具变平台
- **本地 Python 插件**：`.minicode/tools/*.py` 放进去即成为一等工具（函数签名即 schema，比 MCP 更轻）
- **MCP 双传输**：stdio + streamable HTTP 远程服务器
- **结构化压缩**：compact 时机械保留改过的文件/todo/关键命令，只对对话正文做摘要，细节不再丢失
- **`/stats`**：历史会话用量与工具错误率统计

再加：`--budget 50000` 单回合 token 预算护栏、断点续跑（`-c` 恢复会话时自动接续未完成 todo）。

```text
❯ @src/auth.py 帮我看看登录为什么失败，先别改代码
  ● read_file src/auth.py
  ● bash pytest tests/test_auth.py -q
  ⎿ 1 failed in 0.4s
  ● exit_plan
  │ 1. 修复 token 过期判断（auth.py:42）
  │ 2. 补充过期用例 tests/test_auth.py
  │ 3. 运行 pytest 验证
  [Enter] 开始实施计划  [n] 继续讨论 ❯
```

## 特性全景（对标 Claude Code）

| Claude Code | minicode |
|---|---|
| 权限模式 shift+tab | `/mode`：default / accept-edits / **plan** / **full-access**（完全访问：自动执行、高危需确认；`/yolo` 快捷） |
| Plan mode + ExitPlanMode | `plan` 模式只读调研，模型用 `exit_plan` 提交计划，Enter 批准后自动实施 |
| Checkpoint / rewind | 写文件前自动快照，`/undo` `/rewind` |
| CLAUDE.md 记忆 | `MINICODE.md`（兼容 CLAUDE.md / AGENTS.md，支持 `@文件` 导入） |
| think / ultrathink | 相同关键词 → thinking 预算 4k/10k/32k（Anthropic） |
| Task 子智能体 + 自定义 agents | `dispatch_agent` + `.minicode/agents/*.md`（frontmatter：description/tools/model） |
| MCP 服务器 | `mcpServers` 配置（stdio），工具自动注册为 `mcp__<server>__<tool>`，`/mcp` 查看 |
| BashOutput / KillShell | `bash run_in_background` + `bash_output` / `bash_kill` |
| WebFetch / WebSearch | `web_fetch`（HTML→文本）/ `web_search`（DuckDuckGo） |
| NotebookEdit | `notebook_edit`（.ipynb 单元替换/插入/删除） |
| 图片视觉输入 | `read_file` 对 .png/.jpg/.gif/.webp 返回图像块（视觉模型可用） |
| 文件新鲜度检查 | 文件被外部修改后未重读就编辑会被拒绝 |
| AskUserQuestion | `ask_user` 编号选择题 |
| 自定义 commands | `.minicode/commands/*.md`，`$ARGUMENTS` 接参 |
| settings.json permissions | `permissions.allow / deny` 规则（`Bash(git *)`、`mcp__srv__*` 通配） |
| Hooks (pre/post tool) | `hooks.pre_tool_use`（stdin JSON，非零退出拦截）/ `post_tool_use` |
| Prompt caching | Anthropic system+tools 自动打 cache_control |
| API 重试 | 429/5xx 指数退避，遵循 Retry-After |
| /cost /context /export /init /doctor | 全部实现 |
| `claude -p` headless | `minicode -p`（支持 `--output-format json`、管道输入） |
| --allowedTools / --append-system-prompt / --resume / --dangerously-skip-permissions | 同名旗标全部实现 |

核心体验：Agent 多轮循环、流式输出（代码块着色）、思考流显示、token 用量与自动压缩、会话持久化与 `/resume`、多行输入（行尾 `\`）、Tab 补全（readline / pyreadline3）、跨平台 shell（Git Bash / PowerShell / cmd，cwd 持久）。

### v0.4.0：融合 Codex 的能力 + 独有增强

| 能力 | 来源 | 说明 |
|---|---|---|
| `apply_patch` | Codex | 一次调用完成多文件、多 hunk 补丁（Add/Update/Delete），先全量校验再落盘，任何错误不产生半成品；自动纳入检查点 |
| 只读工具并行执行 | 两者 | 同一条消息里的多个 read 工具并发跑（最多 4 线程），结果按序渲染，探索类回合显著提速 |
| microcompaction | Claude Code | 上下文过 60% 时自动把旧的大工具结果替换为占位摘要（保留最近 8 条原文），推迟全量 compact |
| ripgrep 加速 | — | 检测到 `rg` 时 grep 自动切换（本机环境即生效），保留 Python 兜底 |
| `/diff` | Codex | 会话内所有文件改动 vs 改动前状态的总览 diff |
| 高危命令告警 | 两者 | `rm -r*`、`git push -f`、`git reset --hard`、`Remove-Item -Recurse` 等模式自动警示（yolo 模式也不静默） |
| profiles + `--profile` | Codex | `profiles: {"fast": {...}}` 一键切换模型/服务商组合 |
| reasoning effort | 两者 | `/reasoning` 或配置 `reasoning_effort`（low/medium/high）：OpenAI 兼容端即时生效，Anthropic 转为思考预算 |
| stream-json | Claude Code | `-p --output-format stream-json` 逐消息 NDJSON 输出，便于程序化集成 |
| 新 hooks | Claude Code | `user_prompt_submit`（可拦截提示词）、`turn_end` 事件，加上原有 pre/post_tool_use |
| Ctrl+C 双击退出 | Claude Code | 单击打断生成，两秒内再按退出 |

## 多模型适配层（v0.6.0，经真实中转站实测打磨）

minicode 对"任意 OpenAI 兼容端点/模型"做了系统性适配，全部机制来自真实联调发现的问题：

| 现实问题 | minicode 的处理 |
|---|---|
| 限速（429 rpm exhausted） | 指数退避重试 2s→5s→15s→30s，遵循 Retry-After |
| 模型拒绝 `max_tokens` | 自动去掉该参数重试；要求 `max_completion_tokens` 的自动改名 |
| 模型不支持 tools | 识别特征报错 → `ToolUnsupportedError` → 自动降级纯对话模式并提示 |
| 思考模型（DeepSeek/GLM）要求回传 `reasoning_content` | 思考内容存入历史并随助手消息回传，多轮工具链不中断 |
| 流式被服务端破坏 / 空流 | 自动降级非流式请求，结果归一化 |
| 工具调用流没有 index 字段 / arguments 是对象 | 到达顺序槽位归一化 + 参数序列化垫片 |
| 模型列表里有但实际不可用（404） | `--probe` / `/probe` 实测四项：models / 非流式 / 流式 / 工具调用，逐项给出延迟与结论 |
| 不确定中转站有什么模型 | `/models` 直接列出 |

换任意模型的三步：`export OPENAI_BASE_URL=...` → `minicode --probe` 看四项探测 → `--yolo` 开干。thinking 模型（DeepSeek/GLM/o 系）的思考流、`reasoning_effort`、`max_tokens` 差异都已处理。

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
pip install -e .[dev]        # [dev] 额外装 pytest；python -m pytest tests -q 验证
```

**前置要求**：Python ≥ 3.9（Windows / macOS / Linux 均可；Windows 装 Git 即自带所需的 bash）。
**没有 Python？** 从 python.org 或 Microsoft Store 装 3.10+ 即可——本项目零第三方依赖。
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

### 运行

```bash
minicode                          # 交互 REPL
minicode "修复登录 bug"            # 启动即执行
minicode -c                       # 恢复上次会话
minicode --resume                 # 交互式挑历史会话
minicode -p "总结这个项目" --output-format json < task.txt   # headless + JSON
minicode -p "跑测试" --allowed-tools "Bash(pytest *),read_file" --yolo
```

### 斜杠命令

`/mode` `/clear` `/compact` `/undo` `/rewind` `/model` `/context` `/cost` `/memory` `/todos` `/agents` `/mcp` `/add-dir` `/transcript` `/output-style` `/doctor` `/tools` `/status` `/resume` `/export` `/init` `/help` `/exit`，外加 `.minicode/commands/` 里的自定义命令（内置 `/review` 已提供）。

### 配置文件（~/.minicode.json 或项目 ./.minicode.json）

```json
{
  "provider": "anthropic",
  "api_key": "...",
  "permissions": { "allow": ["Bash(git *)", "Bash(pytest *)"], "deny": ["Bash(rm *)"] },
  "hooks": { "pre_tool_use": "python guard.py" },
  "mcpServers": {
    "filesystem": { "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "."] }
  },
  "extra_body": { "temperature": 0.2 },
  "shell": "bash", "timeout": 180, "mode": "accept-edits", "output_style": "default",
  "context_limit": 1000000
}
```

### 自定义子智能体示例（.minicode/agents/scout.md）

```markdown
---
description: 代码侦察兵，快速摸清结构
tools: read_file, grep, glob, list_dir
model: glm-4.6
---
你是代码侦察兵。收到任务后快速搜索阅读，输出结构化报告。
```

模型里即可 `dispatch_agent(prompt="摸清 auth 模块", subagent_type="scout")`。

## 架构

```text
minicode/
├── cli.py           入口：REPL、全部斜杠命令、自定义命令/子智能体加载、计划批准门
├── agent.py         主循环：模型⇄工具、权限规则、hooks、检查点、思考预算、计划模式
├── llm.py           OpenAI 兼容 + Anthropic 双协议；SSE 归一化；重试；缓存；图像块
├── mcp.py           MCP stdio 客户端（JSON-RPC），工具桥接为 mcp__server__tool
├── session.py       历史、todo、用量、压缩、持久化、导出、文件 mtime 追踪
├── checkpoints.py   文件检查点（undo / rewind）
├── prompts.py       系统提示词（环境/规则/子智能体/风格/记忆导入）
├── ui.py            流式渲染、spinner、y/a/n 确认、选择题、静默 UI
├── lineinput.py     Tab 补全（readline / pyreadline3，缺省回退 input()）
├── config.py        默认 < 用户配置 < 项目配置 < 环境变量 < CLI 旗标
├── fake.py          离线假模型
└── tools/           read/write/edit/notebook/glob/grep/list/bash(+bg)/todo/
                     dispatch_agent/web_fetch/web_search/exit_plan/ask_user
```

设计要点：LLM 流归一化为统一事件（`text_delta`/`tool_call`/`finish`+最终消息），Agent 不感知协议差异；工具结果为中立消息（含图像块），按协议转换；MCP 工具与内置工具走同一条确认/权限/检查点管线。

## 测试

```bash
python -m pytest tests -q     # 54 个用例
```

覆盖：双协议 SSE 解析与累积、消息转换（图像块/thinking 回放/工具结果分组）、文件工具（含新鲜度拒绝）、bash（持久 cwd/超时/后台生命周期）、MCP 端到端（内置 echo 服务器）、Notebook 编辑、WebSearch 解析、计划模式拦截、权限规则、检查点回滚、自定义命令/子智能体加载、headless JSON 输出。

## 参与贡献

minicode 欢迎所有人一起迭代升级——**零依赖 + 小代码库意味着每个人都能读完整个实现**。贡献路径由易到难：

1. **技能/命令/子智能体**——纯 Markdown，不改代码（`.minicode/skills|commands|agents/`）
2. **多模型兼容报告**——跑 `minicode --probe` 提交 issue，附输出即可，适配垫片因此持续进化（最急需）
3. **新工具**——继承 `Tool` 五步约 100 行，教程见 CONTRIBUTING
4. **跨平台测试**——macOS/Linux 上跑 `pytest tests -q` 报告结果
5. **核心改进**——权限/上下文/适配层，先开 issue 讨论设计

完整流程见 **[CONTRIBUTING.md](CONTRIBUTING.md)**（环境搭建、项目地图、规则：一个 PR 一个主题、新功能必带测试、不加第三方运行时依赖）。提交前 `pytest tests -q` 全绿。

## 安全与信任边界（公开使用前必读）

- **提示注入是所有编码智能体的共同风险面**：模型读取的文件/网页内容是"数据"而不是指令——minicode 已在系统提示中明确这一边界，并内置敏感路径门禁、SSRF 封禁与危险命令告警，但**没有任何外壳能 100% 消除注入**。给不可信代码库跑任务时，优先使用 `plan`/`default` 模式而不是 yolo。
- **敏感路径门禁**：`.env`、`.git/`、`*.pem`、`id_rsa`、`credentials.json` 等的写入/删除会强制确认（yolo 也不放行，非交互自动拒绝）；`rm -rf .git`、`echo x > .env` 类命令同样被拦。
- **web_fetch SSRF 防护**：解析目标主机，命中内网/环回/链路本地地址（含云元数据 169.254.169.169）默认拒绝，重定向逐跳复查。需要访问内网时配置 `"webfetch_allow_private": true`。
- **第三方代码 = 信任决策**：克隆他人仓库可能带 `.minicode/tools/*.py` 插件与项目 hooks——首次遇到会强制询问并写入 `~/.minicode/trusted/`；**非交互模式（-p / serve）默认不加载**。用户级（~/.minicode）配置始终视为本人意愿。
- **`minicode serve` = 以你的用户身份执行命令的本地服务**：仅绑定 127.0.0.1 + 启动时生成 token，但拿到 token 的本地进程即可触发工具执行。请勿将端口转发到公网。
- **API Key 明文保存在你的配置文件/环境变量中**，不会写入会话记录；但**对话历史（含工具输出）会明文存到 `~/.minicode/sessions/`**——如果让模型读过密钥文件，密钥会落盘。敏感项目可用 `MINICODE_HOME` 类隔离或定期清理该目录。
- **提示注入的教训**：让模型"读一下这个 URL/文件"时，你信任的是那个内容的来源。把它当成给实习生看邮件，而不是给_root_ 执行。


## 化用自 ZCode 的设计（v0.9.0）

minicode 的 harness 设计直接对标 ZCode（本智能体运行所在的宿主），以下机制化用自 ZCode 并做了终端化适配：

| ZCode 机制 | minicode 实现 |
|---|---|
| Skills（SKILL.md 按需加载工作流指令） | `skill` 工具 + `.minicode/skills/<name>/SKILL.md` + 内置四技能（系统化调试 / TDD / 交付前验证 / 写实施计划），`/skills` 查看 |
| ExitPlanMode 的 allowedPrompts | `exit_plan` 携带 `allowed_prompts`，批准后自动预授权（如 `Bash(pytest *)`） |
| AskUserQuestion 的 Other 选项 | `ask_user` 支持自由输入与选项 preview，`allow_other: false` 可关 |
| TaskOutput 阻塞等待 | `bash_output` 支持 `block: true` + `timeout`，等到新输出再返回 |
| TodoWrite 单项 in_progress + 优先级 | `todo_write` 支持 `priority: high/medium/low`，UI 渲染 `!!`/`·`，系统提示强制"最多一项进行中" |
| Hook 输出作为用户反馈 | `post_tool_use` 钩子的 stdout 以 `[hook feedback]` 回喂给模型 |
| Git 安全护栏（默认分支先建分支） | 系统提示内置："仅在用户要求时提交/推送；默认分支先建分支；删除/覆盖前先查看" |
| 后台任务 TaskStop | `bash_kill` + 退出时自动回收进程树 |

## 已知边界（诚实清单）

- MCP 仅支持 stdio 服务器；远程 HTTP/SSE 服务器未实现
- 图片仅支持工具结果回传（终端里无法粘贴剪贴板图片）
- vim 键位、流式输出时排队输入、IDE 集成、OAuth 登录未实现
- Tab 补全：POSIX 自带；Windows 需 `pip install pyreadline3`（可选，不装不影响 IME 中文输入）
- `web_search` 依赖 DuckDuckGo HTML 页面结构，属 best-effort
