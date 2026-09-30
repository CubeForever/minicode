"""扩展市场索引（只读目录）——市场只回答「有什么、从哪装」；安装仍走
install_pack + 既有信任门禁（插件确认/受限配置），index 本身永不执行代码。

索引源（`~/.minicode.json` 的 `marketplaces` 列表，缺省内置社区源）：
    https:// 远程 JSON（经 SSRF 守卫）或本地路径 / file:// 路径
索引格式：
    {"marketplace": "…", "packs": [
        {"name": "review-pack", "description": "…", "source": "<git url|目录>",
         "version": "1.0.0", "author": "…", "tags": ["…"]}]}

用户级配置 `marketplaces` 始终生效；项目级 `marketplaces` 属受限键
（可把安装源重定向到攻击者索引，必须过信任门禁）。
"""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import List, Optional, Tuple

DEFAULT_MARKETPLACES = (
    "https://raw.githubusercontent.com/CubeForever/minicode-market/main/index.json",
)
_FETCH_TIMEOUT = 12


class MarketError(RuntimeError):
    pass


def marketplace_urls(cfg=None) -> List[str]:
    user_urls = list(getattr(cfg, "marketplaces", None) or [])
    return user_urls or list(DEFAULT_MARKETPLACES)


def _read_local(path: str) -> dict:
    p = Path(path.replace("file://", "")).expanduser()
    return json.loads(p.read_text(encoding="utf-8"))


def fetch_index(url: str) -> dict:
    """拉取并校验一个索引。远程仅接受 https 且主机必须是公网地址。"""
    if url.startswith(("file://", "./", "../")) or Path(url).is_absolute():
        data = _read_local(url)
    elif url.startswith("https://"):
        from .tools.webfetch import _assert_public_host
        _assert_public_host(url, allow_private=False)
        req = urllib.request.Request(url, headers={"User-Agent": "minicode"})
        with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    else:
        raise MarketError(f"marketplace 必须是 https 地址或本地路径：{url}")
    if not isinstance(data, dict) or not isinstance(data.get("packs"), list):
        raise MarketError("索引格式无效（缺少 packs 列表）")
    return data


def list_packs(cfg=None) -> Tuple[List[dict], List[str]]:
    """聚合全部索引源的扩展包（按 name+source 去重），返回 (packs, errors)。"""
    packs: List[dict] = []
    errors: List[str] = []
    seen = set()
    for url in marketplace_urls(cfg):
        try:
            data = fetch_index(url)
        except Exception as e:
            errors.append(f"{url}：{e}")
            continue
        for p in data.get("packs") or []:
            if not (isinstance(p, dict) and p.get("name") and p.get("source")):
                continue
            key = (p["name"], p["source"])
            if key in seen:
                continue
            seen.add(key)
            packs.append(p)
    return packs, errors


def resolve_pack(cfg, name: str) -> Optional[dict]:
    """按包名在全部索引源中查找（先到先得）。"""
    packs, _errors = list_packs(cfg)
    for p in packs:
        if p.get("name") == name:
            return p
    return None


def resolve_from_user_config(name: str) -> Optional[dict]:
    """`--install <包名>` 场景（完整配置尚未加载）：只用用户级 marketplaces
    解析包名——项目级属于受限键，不参与安装源解析。"""
    from types import SimpleNamespace
    from .config import USER_CONFIG, _read_json
    urls = (list(_read_json(USER_CONFIG).get("marketplaces") or [])
            or list(DEFAULT_MARKETPLACES))
    return resolve_pack(SimpleNamespace(marketplaces=urls), name)


def format_market(packs: List[dict]) -> str:
    if not packs:
        return "市场里暂时没有可安装的扩展包。"
    lines = []
    for p in packs:
        version = f" v{p['version']}" if p.get("version") else ""
        author = f" · {p['author']}" if p.get("author") else ""
        tags = f" · {', '.join(p['tags'])}" if p.get("tags") else ""
        lines.append(f"  {p['name']}{version}{author}{tags}")
        if p.get("description"):
            lines.append(f"      {str(p['description'])[:100]}")
        lines.append(f"      源：{p['source']}")
    lines.append("安装：minicode --install <包名或 git 地址>（--user 装用户级）")
    return "\n".join(lines)
