# 参与贡献 minicode

感谢你愿意让这个项目变得更好！minicode 刻意保持**零第三方依赖**和**小而清晰的代码库**（核心约 11800 行），任何人都能读完整个实现。这份文档帮你找到最合适的贡献方式。

## 环境搭建

```bash
git clone <仓库地址> && cd minicode
pip install -e .[dev]        # 安装 + pytest / ruff / pytest-cov
python -m pytest tests -q    # 全部测试应当通过
python -m ruff check minicode tests   # lint 应当通过
python -m minicode           # 可选：配一个 API key 真跑，或离线模式：
MINICODE_FAKE_LLM=demo python -m minicode -p hi --yolo
```

## 项目地图（改哪里找哪里）

```
minicode/
├── cli.py        入口：REPL、斜杠命令、回合排队、自定义命令/子智能体加载
├── agent.py      主循环：模型⇄工具、权限/门禁/hooks/检查点/计划模式/协作式中断
├── llm.py        多模型适配层 ★ 新端点怪癖都修在这里；故障转移（FailoverProvider）
├── session.py    会话/压缩/持久化/用量与缓存统计
├── tools/        每个工具一个类（fs/shell/webfetch/panel/...，共 22 个）
├── guard.py      敏感路径门禁
├── ui.py         终端渲染（Markdown/语义色板：青=动作 黄=警 红=错 灰=元信息）
├── lineinput.py  行编辑（readline / 括号粘贴 / 历史落盘）
├── winedit.py    Windows 原生行编辑器（msvcrt）
├── watcher.py    回合键盘监听（Esc 中断 / 消息排队）
├── plugins.py    本地插件加载
├── install.py    扩展包安装（git / 本地目录）
├── market.py     扩展市场索引（只读）
├── mcp.py        MCP stdio + streamable HTTP（tools/resources/prompts）
├── webui.py      Web 界面后端（SSE + 浏览器内确认 + cookie 鉴权）
├── web/          前端静态资源（原生 HTML/CSS/JS）
└── server.py     serve 本地 API
```

## 贡献路径（由易到难，任选其一）

### 1. 技能、命令、子智能体（纯 Markdown，不改代码）
- 技能：`.minicode/skills/<名字>/SKILL.md`（frontmatter: name/description）
- 自定义命令：`.minicode/commands/<名字>.md`（`$ARGUMENTS` 接参）
- 子智能体：`.minicode/agents/<名字>.md`（可指定 tools/model）
写好后开 issue 分享，优秀的可以收进内置技能。

### 2. 多模型兼容报告（最有价值的贡献之一）
在不同端点/模型上跑 `minicode --probe`，遇到报错请开 issue 附上：
`/probe` 输出 + 模型名 + 端点类型。适配垫片（llm.py）因此持续进化——
历史上修过的怪癖包括 thinking 回传、max_tokens 差异、无 index 工具流、dict 参数等。

### 3. 新工具（五步，约 100 行）
1. 在 `minicode/tools/` 新建类继承 `Tool`：设 `name/description/kind/input_schema`
2. 实现 `run(args, ctx) -> str`（错误用 `raise ToolError(...)`，模型能理解并重试）
3. 在 `tools/__init__.py` 注册
4. `tests/` 加测试（参考 test_tools_fs.py 的写法）
5. 全量 `pytest tests -q` 通过后提 PR
原则：工具产出是给模型读的文本（英文、信息密、自动截断）；有副作用的设 `kind="write"/"bash"` 以纳入门禁。

### 4. 跨平台测试（急需）
当前主要在 Windows 实测。在 macOS/Linux 上跑 `pytest tests -q` 并报告结果；
POSIX 路径的 bug 修复直接提 PR。

### 5. 核心改进
权限体系、上下文管理、适配层。先开 issue 讨论设计再动手。

## 规则

- **一个 PR 一个主题**；新功能必须带测试；提交前 `pytest tests -q` 全绿
- **文档一致性由 CI 强制**：`python scripts/check_consistency.py` 必须全绿（版本 / 测试数 / 命令清单等声明与代码对齐），新增模块要同步 README 架构图
- **不加第三方运行时依赖**——这是项目的硬性原则（开发依赖 pytest/ruff/pytest-cov 除外）
- **不提交任何密钥**；安全漏洞不要开公开 issue，按 README 安全章节联系维护者
- 遵循现有代码风格（英文注释给模型读的输出、中文面向用户界面文案）
- 语义化版本；更新 CHANGELOG.md

## 行为准则

友善、尊重、对事不对人。每个人的时间和认知都是有限的。
