# minicode ⚡

终端里的编码智能体，能力对标 Claude Code / Codex CLI，并有三项独创设计。**零第三方依赖**（纯 Python 标准库，≥ 3.9），适配所有 OpenAI 兼容 API（GLM / DeepSeek / Kimi / Qwen / OpenAI / Ollama / 各类中转站）与 Anthropic API。

当前状态：**v0.9.1 · 20 个内置工具 · 189 项自动化测试 · 核心约 6000 行可通读源码 · MIT 开源**

## 它能做什么

- **多轮自主任务**：给它一句话，它自己读代码 → 改代码 → 跑测试 → 汇报结果；流式输出、思考过程可视化、每回合可中断
- **20 个内置工具**：文件读写编辑、多文件补丁（apply_patch）、Jupyter 编辑、glob/grep/list 搜索、bash（含后台进程管理）、子智能体、三视角圆桌、网页抓取/搜索、交互提问、任务清单
- **四种权限模式**：`default`（写操作逐个确认）→ `accept-edits`（自动接受编辑）→ `plan`（只读调研出计划，批准后实施）→ `full-access`（全自动，仅高危操作需确认）
- **跨会话记忆**：项目大脑自动沉淀事实/坑/决策/失败教训，失败回合自动复盘入脑，越用越懂你的项目
- **自检门禁**：`/verify pytest -q` 后每次改动自动跑验收命令，失败自动修复（最多两轮）
- **多模型适配层**：429 退避、思考模型回传、参数垫片、非流式回退、工具调用格式归一化——换任意端点先跑 `--probe` 诊断
- **平台化**：`serve` 本地 HTTP API、本地 Python 插件目录、MCP（stdio + HTTP）、自定义命令/子智能体/技能、headless JSON 输出
- **工程保险**：文件检查点（/undo /rewind）、会话持久化、上下文自动压缩（默认 1M，/limit 自定义）、敏感路径门禁、SSRF 防护

## 优势

- **零依赖、全部可读**：核心约 6000 行纯标准库代码，没有黑盒。想加工具是 100 行的事，想改任何行为都有据可查
- **不锁定模型**：一个环境变量切换 GLM / DeepSeek / Claude / 本地 Ollama；兼容层在真实中转站联调中打磨，新端点的怪癖大多自动消化
- **安全边界内建**：敏感路径强制确认（yolo 也不放行）、SSRF 封禁、第三方插件/钩子首次信任确认、危险命令告警
- **三项独创**（Claude Code / Codex 均无）：项目大脑（Brain）、自检回路（Verify Gate）、圆桌模式（Panel）

## 局限性（诚实清单）

- 提示注入无法被任何外壳 100% 消除——已做门禁/SSRF 封禁/系统提示防御三层缓解，不可信代码库请用 plan/default 模式
- `serve` 模式本质是以你的用户身份执行命令（仅本机 + token 鉴权），不要暴露到公网
- bash 无系统级沙箱（Windows 无可移植方案），用敏感路径门禁替代
- 主要在 Windows 实测，macOS/Linux 有适配但欢迎反馈；Python 3.9 静态合规、运行验证以 3.10+ 为主
- 对话历史明文存于 `~/.minicode/sessions/`；图片输入依赖视觉模型；无 vim 键位与图片粘贴

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
minicode "修复登录 bug"          # 启动即执行
minicode -c                     # 恢复上次会话
minicode --probe                # 实测端点四项能力（装新模型先跑这个）
minicode -p "总结项目" --output-format json < task.txt   # headless
```

### 常用服务商

| 服务商 | base_url | model 示例 |
|---|---|---|
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` | `glm-4.6` |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
| Kimi | `https://api.moonshot.cn/v1` | `kimi-k2-0905-preview` |
| OpenAI | （默认） | `gpt-4o` |
| Anthropic | （默认） | `claude-sonnet-4-5` |
| Ollama 本地 | `http://localhost:11434/v1` | `qwen3:8b`（key 随便填） |

## 使用指南

### 快捷交互

| 操作 | 说明 |
|---|---|
| `!cmd` | 命令直通本地执行，不经过模型 |
| `@文件` | 把文件内容/图片附加给模型 |
| `/yolo` `/plan` `/edits` | 一键切模式 |
| `/comp` 等前缀 | 唯一匹配自动补全 |
| `/model 2` | 按序号切模型；`/model deepseek` 模糊匹配 |
| `/copy` | 复制上条回复到剪贴板 |
| `think / ultrathink` | 提示词含关键词自动加大思考预算 |
| 行尾 `\` | 多行续写 |

### 斜杠命令

`/help` `/mode` `/undo` `/rewind` `/diff` `/compact` `/verify` `/brain` `/memory` `/limit` `/context` `/cost` `/stats` `/model` `/models` `/probe` `/reasoning` `/tools` `/status` `/doctor` `/agents` `/skills` `/mcp` `/add-dir` `/plans` `/todos` `/transcript` `/output-style` `/resume` `/export` `/init` `/commit` `/pr` `/copy` `/exit`

### 自定义扩展

- 技能：`.minicode/skills/<名字>/SKILL.md`（模型按需加载的工作流）
- 命令：`.minicode/commands/<名字>.md`（`$ARGUMENTS` 接参）
- 子智能体：`.minicode/agents/<名字>.md`（可指定 tools/model）
- 工具插件：`.minicode/tools/<名字>.py`（函数签名即 schema）

## 架构

```text
minicode/
├── cli.py          REPL / 斜杠命令 / 计划批准门
├── agent.py        主循环：权限、门禁、hooks、检查点、思考预算
├── llm.py          双协议适配 + SSE 归一化 + 重试 + 缓存
├── mcp.py          MCP 客户端（stdio / HTTP）
├── session.py      会话 / 压缩 / 持久化
├── checkpoints.py  文件检查点（undo / rewind）
├── guard.py        敏感路径门禁
├── prompts.py      系统提示词（注入防御 / Git 护栏）
├── plugins.py      本地插件加载
├── server.py       serve 模式（本地 HTTP API）
├── ui.py           终端渲染 / 余量条 / 确认框
├── lineinput.py    Tab 补全（readline）
├── fake.py         离线假模型
└── tools/          20 个工具实现
```

## 测试

```bash
python -m pytest tests -q    # 189 项，覆盖协议解析/工具/安全/全功能链路
```

## 安全与信任边界（使用前必读）

- **提示注入无法被外壳 100% 消除**：已做三层缓解（敏感路径门禁、SSRF 封禁、系统提示防御），不可信代码库请用 plan/default 模式
- **`serve` = 以你的身份执行命令**：仅绑定 127.0.0.1 + token 鉴权，切勿暴露公网
- **对话历史明文存于 `~/.minicode/sessions/`**：让模型读过密钥文件就会落盘
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
