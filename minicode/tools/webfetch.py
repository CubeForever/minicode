"""WebFetch tool: fetch a URL and return readable text (stdlib only).

SSRF guard: the target host is resolved and blocked when it points at
private / loopback / link-local / reserved addresses (e.g. 169.254.169.169
cloud metadata or internal services). Redirects are followed manually and
re-checked. Set ``webfetch_allow_private: true`` in config to lift this.
"""
from __future__ import annotations

import ipaddress
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

from .base import Tool, ToolContext, ToolError, truncate_middle

USER_AGENT = ("Mozilla/5.0 (compatible; minicode/0.8; +https://localhost) "
              "AppleWebKit/537.36 Chrome/120 Safari/537.36")

MAX_REDIRECTS = 3


def _assert_public_host(url: str, allow_private: bool) -> str:
    host = urllib.parse.urlparse(url).hostname
    if not host:
        raise ToolError("url has no host")
    if allow_private:
        return host
    import socket
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise ToolError(f"cannot resolve host {host}: {e}")
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            raise ToolError(
                f"blocked: {host} resolves to a private/loopback address ({ip}) — "
                "internal network access is disabled by default "
                "(config: webfetch_allow_private)")
    return host


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # handle redirects manually so targets are re-checked

_SKIP_TAGS = {"script", "style", "noscript", "svg", "head", "template", "iframe"}
_BLOCK_TAGS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
               "section", "article", "header", "footer", "pre", "blockquote",
               "table", "ul", "ol", "hr", "option"}


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list = []
        self.title = ""
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._skip_depth:
            return
        if self._in_title:
            self.title += data
        else:
            self.parts.append(data)


def html_to_text(data: str) -> str:
    """Very small HTML → text extraction (stdlib html.parser)."""
    parser = _TextExtractor()
    try:
        parser.feed(data)
    except Exception:
        return data  # not really HTML; show raw
    text = "".join(parser.parts)
    lines = [ln.strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


class WebFetchTool(Tool):
    name = "web_fetch"
    kind = "read"
    description = ("Fetch a URL and return its readable text content (HTML converted to "
                   "text, scripts/styles removed). Use for documentation, APIs, pages.")
    input_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Absolute http(s) URL."},
        },
        "required": ["url"],
    }

    def describe_call(self, args: dict) -> str:
        return str(args.get("url") or "")

    def run(self, args: dict, ctx: ToolContext) -> str:
        url = str(args.get("url") or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            raise ToolError("url must start with http:// or https://")
        allow_private = bool(getattr(ctx.config, "webfetch_allow_private", False))
        opener = urllib.request.build_opener(_NoRedirect)
        current = url
        for _redirect in range(MAX_REDIRECTS + 1):
            _assert_public_host(current, allow_private)
            req = urllib.request.Request(current, headers={"User-Agent": USER_AGENT})
            try:
                with opener.open(req, timeout=25) as resp:
                    ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                    raw = resp.read(3_000_000)
                    final_url = resp.geturl()
                break
            except urllib.error.HTTPError as e:
                if e.code in (301, 302, 303, 307, 308) and _redirect < MAX_REDIRECTS:
                    loc = e.headers.get("Location")
                    if not loc:
                        raise ToolError(f"redirect without Location from {current}")
                    current = urllib.parse.urljoin(current, loc)
                    continue
                raise ToolError(f"HTTP {e.code} fetching {url}")
            except (urllib.error.URLError, OSError, ValueError) as e:
                raise ToolError(f"fetch failed: {e}")
        else:
            raise ToolError(f"too many redirects fetching {url}")
        if ctype and not (ctype.startswith("text/") or ctype in
                          ("application/json", "application/xml", "application/xhtml+xml",
                           "application/javascript", "application/x-yaml")):
            raise ToolError(f"unsupported content-type: {ctype} (only text-ish pages)")
        text = raw.decode("utf-8", errors="replace")
        if "html" in ctype or text.lstrip()[:200].lower().startswith(("<!doctype html", "<html")):
            extractor = _TextExtractor()
            try:
                extractor.feed(text)
                title = extractor.title.strip()
                text = html_to_text(text)
            except Exception:
                title = ""
            header = f"[{final_url}]\n{title}\n" if title else f"[{final_url}]\n"
        else:
            header = f"[{final_url}]\n"
        return header + truncate_middle(text.strip(), 20000)
