"""Configuration: defaults < user config < project config < environment < CLI flags."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .ui import bold, dim, yellow

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
    turn_budget: int = 0                             # max tokens per turn (0 = off)
    webfetch_allow_private: bool = False             # allow web_fetch to hit internal IPs
    hooks_from_project: dict = field(default_factory=dict)  # hooks defined by project config (trust-gated)
    extra_dirs: list = field(default_factory=list)   # /add-dir
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
    merged: dict = {}
    for path in (USER_CONFIG, PROJECT_CONFIG):
        merged.update({k: v for k, v in _read_json(path).items() if v is not None})

    profile = getattr(args, "profile", None)
    if profile:
        profiles = merged.get("profiles") or {}
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
    if not api_key and not fake:
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

    return Config(
        provider=provider,
        api_key=api_key,
        base_url=str(merged.get("base_url") or os.environ.get(url_env) or pd["base_url"]),
        model=str(getattr(args, "model", None) or merged.get("model")
                  or os.environ.get(model_env) or pd["model"]),
        max_tokens=max_tokens,
        context_limit=int(merged.get("context_limit") or pd["context_limit"]),
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
        turn_budget=int(merged.get("turn_budget") or 0),
        webfetch_allow_private=bool(merged.get("webfetch_allow_private", False)),
        hooks_from_project=_as_dict(_read_json(PROJECT_CONFIG).get("hooks")),
        extra_dirs=list(merged.get("extra_dirs") or []),
        debug=bool(os.environ.get("MINICODE_DEBUG")),
    )
