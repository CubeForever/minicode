# Changelog

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
