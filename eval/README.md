# eval 回路 — 真实任务成功率度量

372 个合成测试只能保证"代码行为符合预期"，不能回答"**真实任务成功率是多少**"。
这个目录就是回答第二个问题的回路：一组小型真实任务（修 bug / 加特性 / 重构），
无头跑 minicode，按可执行校验判定通过，统计成功率、时长与 token。

## 指标口径（v0.22.1 固定，对比时必须遵守）

基线（eval/baselines/，13/13 = 100%）说明当前任务集对一线模型处于**饱和区**：
成功率的区分度有限，且会长期贴着 100%。因此版本间对比的口径固定为：

- **主指标：token 降幅 + 耗时降幅**（与 eval/baselines/ 基线对比，
  必须同模型、同端点、同 --timeout；基线：输入 553,437 / 输出 16,606 tok、
  平均 100.7s/任务）
- **护栏指标：成功率不低于基线**——回落即视为回归，先排查再谈收益

## 运行

```bash
python scripts/run_eval.py                # 全部任务（用当前环境变量的模型/端点）
python scripts/run_eval.py --filter json  # 只跑 id 含 "json" 的任务
python scripts/run_eval.py --timeout 240  # 单任务超时（秒，默认 300）
python scripts/run_eval.py --list         # 列出任务
```

模型配置与日常使用一致（`OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL`
或 `ANTHROPIC_*`）。结果打印 Markdown 友好表格并写入 `eval/results/eval-<时间戳>.json`。

## 任务格式（eval/tasks/*.json）

```json
{
  "id": "bugfix-off-by-one",
  "category": "bugfix",
  "instruction": "给智能体的任务指令（自然语言）",
  "setup": {
    "files": {"app.py": "初始文件内容…"},
    "commands": ["git init -q"]
  },
  "check": {
    "command": "{python} app.py",
    "expect_output": "10 20 30 40"
  }
}
```

- `command` 占位符：`{python}` = 当前解释器、`{check}` = `eval/checks/<id>.py`、
  `{sandbox}` = 任务沙箱绝对路径
- `expect_output` 可选：校验命令 stdout 必须包含该子串
- 复杂断言写成校验脚本放 `eval/checks/<id>.py`，以沙箱路径为 `argv[1]`。
  **校验脚本在沙箱外**，智能体改不到它，无法作弊。

## 新增任务的原则

1. 确定性：任何合格模型都能一次做对，断言不依赖环境
2. 小：初始文件 ≤ 40 行，指令 ≤ 3 句
3. 可作弊面为零：不把答案写进 setup 文件，校验逻辑放 checks/（沙箱外）

## CI

`.github/workflows/eval.yml` 每夜（北京时间 02:00）自动运行，结果上传为
artifact。任务失败**不会**弄红 CI（eval 是度量，不是门禁）；配置
`EVAL_OPENAI_API_KEY` / `EVAL_OPENAI_BASE_URL` / `EVAL_OPENAI_MODEL`
secrets 后自动启用，未配置则跳过。

## 安全前提

任务以 `--yolo`（全自动）在临时沙箱目录运行——与 Codex / Claude Code 的
自动评测同一前提：仅在可信环境执行，不要在含敏感数据的机器上对不可信
任务集开 yolo。
