"""Configuration: defaults < user config < project config < environment < CLI flags."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .ui import bold, dim

USER_CONFIG = Path.home() / ".minicode.json"
PROJECT_CONFIG = Path(".minicode.json")

PROVIDER_DEFAULTS = {
    "anthropic": {
        "model": "claude-sonnet-4-5",
        "base_url": "https://api.anthropic.com",
        "context_limit": 200_000,   # Claude 官方上限；可在配置中调
    },
    "openai": {
        "model": "gpt-4o",
        "base_url": "https://api.openai.com/v1",
        "context_limit": 1_000_000,  # 默认 1M（现代长上下文模型）；可调
    },
}

# 项目配置（.minicode.json，可能来自他人仓库）中会改写安全边界的字段：
# 端点/密钥重定向、权限弱化、任意进程/命令。它们不会自动生效——先存入
# cfg.project_restricted，经 cli._project_trust_gate 确认后由
# apply_restricted_config 应用。其余字段（model/context_limit/timeout 等）
# 危害有限，保持自动生效。
RESTRICTED_KEYS = {
    "api_key", "base_url", "mcp_servers", "mcpServers", "hooks",
    "permissions", "verify_command", "lint_command", "extra_body",
    "webfetch_allow_private", "fallbacks", "marketplaces",
}
_RESTRICTED_DICT_KEYS = {"permissions", "extra_body", "mcp_servers", "hooks"}


def _normalize_fallbacks(value) -> list:
    """fallbacks 配置 → [{"model": 必填, "provider"/"base_url"/"api_key" 可选}]。"""
    if not isinstance(value, list):
        return []
    out = []
    for fb in value:
        if isinstance(fb, str):     # 简写：仅模型名，其余继承主配置
            fb = {"model": fb}
        if isinstance(fb, dict) and fb.get("model"):
            out.append({"model": str(fb["model"]),
                        "provider": fb.get("provider"),
                        "base_url": fb.get("base_url"),
                        "api_key": fb.get("api_key")})
    return out


@dataclass
class Config:
    provider: str = "openai"
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    max_tokens: Optional[int] = None
    context_limit: int = 128000
    shell: Optional[str] = None          # bash | powershell | cmd
    timeout: int = 120
    include_usage: bool = True
    mode: str = "default"                # default | accept-edits | plan | yolo
    permissions: dict = field(default_factory=dict)  # {"allow": [...], "deny": [...]}
    hooks: dict = field(default_factory=dict)        # {"pre_tool_use": "cmd", "post_tool_use": "cmd"}
    extra_body: dict = field(default_factory=dict)   # merged into every LLM request body
    mcp_servers: dict = field(default_factory=dict)  # {"name": {"command": ..., "args": [...], "env": {...}}}
    output_style: str = "default"                    # default | explanatory
    reasoning_effort: str = ""                       # "" | low | medium | high
    verify_command: str = ""                         # self-verify gate, e.g. "pytest -q"
    lint_command: str = ""                           # 编辑后 lint 快速回路，如 "ruff check {files}"；空=自动探测
    turn_budget: int = 0                             # max tokens per turn (0 = off)
    save_sessions: bool = True                       # False 时不落盘会话历史（隐私模式）
    webfetch_allow_private: bool = False             # allow web_fetch to hit internal IPs
    fallbacks: list = field(default_factory=list)    # 模型故障转移：[{"model": ..., "provider"/"base_url"/"api_key" 可选}]
    marketplaces: list = field(default_factory=list) # 扩展市场索引源（https URL 或本地路径）
    extra_dirs: list = field(default_factory=list)   # /add-dir
    project_restricted: dict = field(default_factory=dict)  # 项目配置中的受限字段（信任门禁后生效）
    plugins_allowed: bool = True                     # 项目信任门禁结果（cli 设置）
    workspace_lock: bool = False                     # Web 工作区锁定：写操作硬拒绝工作区之外
    append_system_prompt: str = ""                   # --append-system-prompt
    debug: bool = False
    cwd: Optional[Path] = None

    @property
    def shell_name(self) -> str:
        if self.shell:
            return self.shell
        try:
            from .tools.shell import detect_shell
            return detect_shell(None)
        except Exception:
            return "bash"


# 模型名子串 → 官方上下文窗口。未显式配置 context_limit 时按模型家族
# 推断——否则 openai 兼容端默认 1M 会让 60%/80% 压缩阈值在 128K 模型上
# 完全失真（永远触发不了 microcompaction / 自动压缩）。先专后泛，
# 未匹配到时回落 provider 默认值。
_CONTEXT_BY_MODEL: tuple = (
    (("claude",), 200_000),
    (("gpt-4.1",), 1_000_000),
    (("gpt-5",), 400_000),
    (("o3", "o4-mini"), 200_000),
    (("gpt-4o", "gpt-4-turbo"), 128_000),
    (("glm-4.5", "glm-4.6", "glm-5"), 200_000),
    (("glm",), 128_000),
    (("deepseek",), 128_000),
    (("kimi", "moonshot"), 256_000),
    (("qwen3",), 262_144),
    (("qwen",), 131_072),
    (("gemini",), 1_000_000),
    (("grok",), 256_000),
    (("doubao",), 256_000),
    (("minimax-m1",), 1_000_000),
    (("minimax",), 200_000),
    (("llama",), 128_000),
)


def infer_context_limit(model: str, fallback: int) -> int:
    low = (model or "").lower()
    for names, limit in _CONTEXT_BY_MODEL:
        if any(name in low for name in names):
            return limit
    return fallback


def _read_json(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _setup_guide() -> str:
    return (
        f"\n{bold('未检测到 API 配置')} — 任选一种方式配置 minicode：\n\n"
        f"{dim('1) 环境变量（OpenAI 兼容：GLM / DeepSeek / Kimi / OpenAI 等）')}\n"
        f"   export OPENAI_API_KEY=你的密钥\n"
        f"   export OPENAI_BASE_URL=https://open.bigmodel.cn/api/paas/v4   {dim('# 例：智谱 GLM')}\n"
        f"   export OPENAI_MODEL=glm-4.6\n\n"
        f"{dim('2) 或使用 Claude（Anthropic）')}\n"
        f"   export ANTHROPIC_API_KEY=你的密钥\n"
        f"   export ANTHROPIC_MODEL=claude-sonnet-4-5\n\n"
        f"{dim('3) 或写入配置文件 ' + str(USER_CONFIG))}\n"
        f'   {"{"}\"provider\": \"openai\", \"api_key\": \"...\", \"base_url\": \"...\", \"model\": \"...\"{"}"}\n\n'
        f"{dim('本地离线体验：')} MINICODE_FAKE_LLM=demo python -m minicode -p hi --yolo\n"
    )


def _as_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def load_config(args) -> Optional[Config]:
    user = _read_json(USER_CONFIG)
    project = _read_json(PROJECT_CONFIG)
    merged: dict = {k: v for k, v in user.items() if v is not None}
    # 项目配置只自动应用安全字段；RESTRICTED_KEYS 中的字段存入
    # project_restricted，等信任门禁确认后再应用。
    restricted = {k: project[k] for k in RESTRICTED_KEYS
                  if k in project and project[k] is not None}
    merged.update({k: v for k, v in project.items()
                   if v is not None and k not in RESTRICTED_KEYS})

    profile = getattr(args, "profile", None)
    if profile:
        profiles = user.get("profiles") or {}  # profiles 是用户级配置，项目不可定义
        prof = profiles.get(profile)
        if not isinstance(prof, dict):
            print(f"未知 profile: {profile!r}（可选：{', '.join(profiles) or '无'}）")
            return None
        merged.update({k: v for k, v in prof.items() if v is not None})

    provider = merged.get("provider") or getattr(args, "provider", None)
    if not provider:
        provider = "anthropic" if os.environ.get("ANTHROPIC_API_KEY") else "openai"
    pd = PROVIDER_DEFAULTS.get(provider, PROVIDER_DEFAULTS["openai"])

    key_env = "ANTHROPIC_API_KEY" if provider == "anthropic" else "OPENAI_API_KEY"
    url_env = "ANTHROPIC_BASE_URL" if provider == "anthropic" else "OPENAI_BASE_URL"
    model_env = "ANTHROPIC_MODEL" if provider == "anthropic" else "OPENAI_MODEL"

    fake = os.environ.get("MINICODE_FAKE_LLM", "").strip()
    api_key = str(merged.get("api_key") or os.environ.get(key_env) or "")
    # 项目受限字段里带了 api_key 时先放行——键是否可用由信任门禁决定
    if not api_key and not fake and not restricted.get("api_key"):
        print(_setup_guide())
        return None

    max_tokens = merged.get("max_tokens")
    if provider == "anthropic" and not max_tokens:
        max_tokens = 8192

    mode = "yolo" if getattr(args, "yolo", False) else (merged.get("mode") or "default")
    if mode == "yolo":
        mode = "full-access"  # legacy alias
    if mode not in ("default", "accept-edits", "plan", "full-access"):
        mode = "default"

    model = str(getattr(args, "model", None) or merged.get("model")
                or os.environ.get(model_env) or pd["model"])

    return Config(
        provider=provider,
        api_key=api_key,
        base_url=str(merged.get("base_url") or os.environ.get(url_env) or pd["base_url"]),
        model=model,
        max_tokens=max_tokens,
        context_limit=int(merged.get("context_limit")
                          or infer_context_limit(model, pd["context_limit"])),
        shell=merged.get("shell"),
        timeout=int(merged.get("timeout") or 120),
        include_usage=bool(merged.get("include_usage", True)),
        mode=mode,
        permissions=_as_dict(merged.get("permissions")),
        hooks=_as_dict(merged.get("hooks")),
        extra_body=_as_dict(merged.get("extra_body")),
        mcp_servers=_as_dict(merged.get("mcpServers") or merged.get("mcp_servers")),
        output_style=merged.get("output_style") or "default",
        reasoning_effort=str(merged.get("reasoning_effort") or ""),
        verify_command=str(merged.get("verify_command") or ""),
        lint_command=str(merged.get("lint_command") or ""),
        turn_budget=int(merged.get("turn_budget") or 0),
        save_sessions=bool(merged.get("save_sessions", True)),
        webfetch_allow_private=bool(merged.get("webfetch_allow_private", False)),
        fallbacks=_normalize_fallbacks(merged.get("fallbacks")),
        marketplaces=[str(u) for u in (merged.get("marketplaces") or [])
                      if isinstance(u, str)],
        project_restricted=restricted,
        extra_dirs=list(merged.get("extra_dirs") or []),
        append_system_prompt=str(getattr(args, "append_system_prompt", "") or ""),
        debug=bool(os.environ.get("MINICODE_DEBUG")),
    )


def apply_restricted_config(cfg) -> None:
    """信任门禁通过后，把项目配置的受限字段应用到 cfg。

    字典类字段（permissions/hooks/extra_body/mcp_servers）与用户配置合并
    （项目同名键覆盖，用户自己的 deny 规则等保留），标量直接替换。
    """
    for raw_key, value in (getattr(cfg, "project_restricted", {}) or {}).items():
        key = "mcp_servers" if raw_key == "mcpServers" else raw_key
        if key in _RESTRICTED_DICT_KEYS and isinstance(value, dict) \
                and isinstance(getattr(cfg, key, None), dict):
            merged = dict(getattr(cfg, key) or {})
            merged.update(value)
            setattr(cfg, key, merged)
        elif hasattr(cfg, key):
            setattr(cfg, key, value)
    cfg.project_restricted = {}
