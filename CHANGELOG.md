# Changelog

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
