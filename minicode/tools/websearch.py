"""WebSearch tool: DuckDuckGo HTML endpoint, parsed with stdlib only (best-effort)."""
from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

from .base import Tool, ToolError, truncate_middle
from .webfetch import USER_AGENT


class _DDGParser(HTMLParser):
    """Extracts results from html.duckduckgo.com/html/ markup."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results = []
        self._cur = None
        self._mode = None  # "title" | "snippet"

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        attrs = dict(attrs)
        cls = attrs.get("class", "")
        if "result__a" in cls:
            self._cur = {"url": _clean_url(attrs.get("href", "")), "title": "", "snippet": ""}
            self._mode = "title"
        elif "result__snippet" in cls and self._cur is not None:
            self._mode = "snippet"

    def handle_data(self, data):
        if self._cur is None or not self._mode:
            return
        if self._mode == "title":
            self._cur["title"] += data
        else:
            self._cur["snippet"] += data

    def handle_endtag(self, tag):
        if tag == "a" and self._mode:
            if self._mode == "title":
                self._mode = None  # wait for snippet
            else:
                self._mode = None
                if self._cur["title"]:
                    self.results.append(self._cur)
                self._cur = None


def _clean_url(href: str) -> str:
    """DDG wraps targets in /l/?uddg=<urlencoded>; unwrap when present."""
    if "uddg=" in href:
        try:
            q = urllib.parse.urlparse(href)
            params = urllib.parse.parse_qs(q.query)
            if params.get("uddg"):
                return params["uddg"][0]
        except ValueError:
            pass
    if href.startswith("//"):
        return "https:" + href
    return href


def parse_results(html: str, limit: int = 10) -> list:
    parser = _DDGParser()
    try:
        parser.feed(html)
    except Exception:
        pass
    out = []
    for r in parser.results[:limit]:
        out.append({"title": " ".join(r["title"].split()),
                    "url": r["url"],
                    "snippet": " ".join(r["snippet"].split())[:300]})
    return out


class WebSearchTool(Tool):
    name = "web_search"
    kind = "read"
    description = ("Search the web (DuckDuckGo) and return the top results: "
                   "title, URL and snippet. Use web_fetch to read a result in full.")
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query."},
        },
        "required": ["query"],
    }

    def describe_call(self, args: dict) -> str:
        return str(args.get("query") or "")

    def run(self, args: dict, ctx) -> str:
        query = str(args.get("query") or "").strip()
        if not query:
            raise ToolError("query is required")
        data = urllib.parse.urlencode({"q": query, "kl": "wt-wt"}).encode()
        req = urllib.request.Request(
            "https://html.duckduckgo.com/html/", data=data,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                html = resp.read(2_000_000).decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            raise ToolError(f"search failed: HTTP {e.code}")
        except (urllib.error.URLError, OSError) as e:
            raise ToolError(f"search failed: {e} —— 当前网络可能无法访问 DuckDuckGo；"
                            "可改用 web_fetch 直接访问搜索引擎页面，或检查代理设置")
        results = parse_results(html)
        if not results:
            return f"no results found for {query!r} (the search engine layout may have changed)"
        lines = [f"{i}. {r['title']}\n   {r['url']}\n   {r['snippet']}"
                 for i, r in enumerate(results, 1)]
        return truncate_middle("\n".join(lines), 12000)
