"""Web search skill — bounded Bing HTML search with no API key."""

import asyncio
import ipaddress
import logging
import re
import socket
import time
from collections import OrderedDict
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import getproxies

import httpx

from mochi.skills.base import Skill, SkillContext, SkillResult

log = logging.getLogger(__name__)

_MAX_QUERY_LEN = 500
_DEFAULT_TIMEOUT_S = 10
_DEFAULT_MAX_RESULTS = 5
_CACHE_TTL_S = 300
_CACHE_SIZE = 256
_MAX_SEARCH_RESPONSE_BYTES = 1024 * 1024
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_EXTRACTED_CHARS = 20_000
_MAX_REDIRECTS = 5
_SYSTEM_HTTPS_PROXY = bool(getproxies().get("https"))
_PROXY_FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")
_PRIVATE_IPV4_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "224.0.0.0/4",
        "240.0.0.0/4",
    )
)


# ---------------------------------------------------------------------------
# TTL-bounded LRU cache
# ---------------------------------------------------------------------------

class _TtlCache:
    """Simple TTL + size-bounded LRU cache."""

    def __init__(self, max_size: int = 256, ttl_s: int = 300):
        self._max_size = max_size
        self._ttl_s = ttl_s
        self._store: OrderedDict[str, tuple[float, str]] = OrderedDict()

    def get(self, key: str) -> str | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        inserted_at, value = entry
        if time.monotonic() - inserted_at > self._ttl_s:
            del self._store[key]
            return None
        self._store.move_to_end(key)
        return value

    def put(self, key: str, value: str) -> None:
        if key in self._store:
            del self._store[key]
        self._store[key] = (time.monotonic(), value)
        while len(self._store) > self._max_size:
            self._store.popitem(last=False)


_cache = _TtlCache(max_size=_CACHE_SIZE, ttl_s=_CACHE_TTL_S)


# ---------------------------------------------------------------------------
# Search via Bing HTML
# ---------------------------------------------------------------------------

class _BingSearchParser(HTMLParser):
    """Extract Bing's organic result title, URL, and snippet."""

    def __init__(self, limit: int):
        super().__init__(convert_charrefs=True)
        self.limit = limit
        self.results: list[dict[str, str]] = []
        self._current: dict[str, str] | None = None
        self._li_depth = 0
        self._in_h2 = False
        self._in_title_link = False
        self._in_snippet = False
        self._title_parts: list[str] = []
        self._snippet_parts: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = dict(attrs)
        if (
            tag == "li"
            and self._current is None
            and "b_algo" in (attributes.get("class") or "").split()
        ):
            self._current = {"href": ""}
            self._li_depth = 1
            self._title_parts = []
            self._snippet_parts = []
            return
        if self._current is None:
            return
        if tag == "li":
            self._li_depth += 1
        elif tag == "h2":
            self._in_h2 = True
        elif tag == "a" and self._in_h2 and not self._current["href"]:
            href = (attributes.get("href") or "").strip()
            if href.startswith(("https://", "http://")):
                self._current["href"] = href
                self._in_title_link = True
        elif tag == "p":
            self._in_snippet = True

    def handle_endtag(self, tag: str) -> None:
        if self._current is None:
            return
        if tag == "a":
            self._in_title_link = False
        elif tag == "h2":
            self._in_h2 = False
        elif tag == "p":
            self._in_snippet = False
        elif tag == "li":
            self._li_depth -= 1
            if self._li_depth == 0:
                self._finish_result()

    def handle_data(self, data: str) -> None:
        if self._current is None:
            return
        if self._in_title_link:
            self._title_parts.append(data)
        elif self._in_snippet:
            self._snippet_parts.append(data)

    def _finish_result(self) -> None:
        assert self._current is not None
        title = " ".join("".join(self._title_parts).split())
        snippet = " ".join("".join(self._snippet_parts).split())
        href = self._current["href"]
        if title and href and len(self.results) < self.limit:
            self.results.append({
                "title": title,
                "href": href,
                "body": snippet,
            })
        self._current = None
        self._li_depth = 0
        self._in_h2 = False
        self._in_title_link = False
        self._in_snippet = False


async def _bing_search(
    query: str,
    max_results: int = 5,
    timeout_s: int = _DEFAULT_TIMEOUT_S,
) -> str:
    """Search one reachable backend within one overall deadline."""
    cache_key = f"{query}|{max_results}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
    }
    async with asyncio.timeout(timeout_s):
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s),
            follow_redirects=True,
            headers=headers,
        ) as client:
            async with client.stream(
                "GET",
                "https://www.bing.com/search",
                params={"q": query, "count": max_results},
            ) as response:
                response.raise_for_status()
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > _MAX_SEARCH_RESPONSE_BYTES:
                        raise ValueError("Search response exceeds the 1 MB limit.")
                    chunks.append(chunk)
                encoding = response.encoding or "utf-8"
                html = b"".join(chunks).decode(encoding, errors="replace")
        parser = _BingSearchParser(max_results)
        parser.feed(html)
        parser.close()
    results = parser.results
    if not results:
        return "[0 results]"

    output = "\n\n".join(
        (
            f"{index}. {result['title']}\n"
            f"   {result['href']}\n"
            f"   {result['body'][:300]}"
        )
        for index, result in enumerate(results, 1)
    )
    _cache.put(cache_key, output)
    return output


# ---------------------------------------------------------------------------
# Bounded public web-page reading
# ---------------------------------------------------------------------------

def _validate_public_https_url(url: str) -> str:
    normalized, _ = _resolve_public_https_url(url)
    return normalized


def _resolve_public_https_url(
    url: str,
) -> tuple[str, ipaddress.IPv4Address | ipaddress.IPv6Address]:
    parsed = urlsplit(url)
    if parsed.scheme.lower() != "https":
        raise ValueError("Only HTTPS web pages can be read.")
    if parsed.username or parsed.password:
        raise ValueError("URLs containing credentials are not allowed.")
    if not parsed.hostname:
        raise ValueError("URL must include a hostname.")

    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError("Localhost URLs are not allowed.")

    direct_address = True
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        direct_address = False
        try:
            resolved = socket.getaddrinfo(
                host,
                parsed.port or 443,
                type=socket.SOCK_STREAM,
            )
        except socket.gaierror as exc:
            raise ValueError(f"Could not resolve hostname: {host}") from exc
        addresses = list(dict.fromkeys(
            ipaddress.ip_address(item[4][0])
            for item in resolved
        ))

    allow_proxy_fake = (
        _SYSTEM_HTTPS_PROXY
        and not direct_address
        and bool(addresses)
        and all(address in _PROXY_FAKE_IP_NETWORK for address in addresses)
    )
    if not addresses or any(
        _is_private_address(address, allow_proxy_fake=allow_proxy_fake)
        for address in addresses
    ):
        raise ValueError("Local or private network URLs are not allowed.")
    normalized = urlunsplit((
        "https",
        parsed.netloc,
        parsed.path or "/",
        parsed.query,
        "",
    ))
    return normalized, addresses[0]


def _is_private_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
    *,
    allow_proxy_fake: bool = False,
) -> bool:
    if isinstance(address, ipaddress.IPv4Address):
        if allow_proxy_fake and address in _PROXY_FAKE_IP_NETWORK:
            return False
        return any(address in network for network in _PRIVATE_IPV4_NETWORKS)
    if address.ipv4_mapped:
        return _is_private_address(address.ipv4_mapped)
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
    )


def _pinned_request_target(
    url: str,
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> tuple[str, str, str]:
    parsed = urlsplit(url)
    hostname = parsed.hostname or ""
    host_header = f"[{hostname}]" if ":" in hostname else hostname
    ip_host = f"[{address}]" if isinstance(address, ipaddress.IPv6Address) else str(address)
    if parsed.port and parsed.port != 443:
        host_header = f"{host_header}:{parsed.port}"
        ip_host = f"{ip_host}:{parsed.port}"
    request_url = urlunsplit((
        "https",
        ip_host,
        parsed.path or "/",
        parsed.query,
        "",
    ))
    return request_url, host_header, hostname


class _ReadableHtmlParser(HTMLParser):
    _BLOCK_TAGS = {
        "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt",
        "figcaption", "footer", "h1", "h2", "h3", "h4", "h5", "h6",
        "header", "li", "main", "nav", "ol", "p", "pre", "section",
        "table", "td", "th", "tr", "ul",
    }
    _SKIP_TAGS = {"script", "style", "noscript", "svg", "template"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        elif not self._skip_depth and tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif not self._skip_depth and tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._parts.append(data)

    def text(self) -> str:
        return _normalize_readable_text("".join(self._parts))


def _normalize_readable_text(text: str) -> str:
    lines = [
        re.sub(r"[^\S\r\n]+", " ", line).strip()
        for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    ]
    return "\n\n".join(line for line in lines if line)


_META_CHARSET_RE = re.compile(
    br"<meta[^>]+charset\s*=\s*[\"']?\s*([a-zA-Z0-9._-]+)",
    re.IGNORECASE,
)


def _detect_page_encoding(body: bytes, declared: str | None) -> str:
    if body.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if body.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    if declared:
        return declared
    match = _META_CHARSET_RE.search(body[:4096])
    if match:
        return match.group(1).decode("ascii")
    return "utf-8"


def _extract_readable_text(
    body: bytes,
    content_type: str,
    encoding: str | None,
) -> str:
    encoding = _detect_page_encoding(body, encoding)
    try:
        decoded = body.decode(encoding, errors="replace")
    except LookupError:
        decoded = body.decode("utf-8", errors="replace")

    if "html" not in content_type.lower() and not decoded.lstrip().startswith("<"):
        return _normalize_readable_text(decoded)

    parser = _ReadableHtmlParser()
    parser.feed(decoded)
    parser.close()
    return parser.text()


async def _read_web_page(url: str) -> str:
    try:
        async with asyncio.timeout(_DEFAULT_TIMEOUT_S):
            return await _read_web_page_within_deadline(url)
    except TimeoutError as exc:
        raise ValueError(
            f"Page read timed out after {_DEFAULT_TIMEOUT_S} seconds."
        ) from exc


async def _read_web_page_within_deadline(url: str) -> str:
    current = url
    timeout = httpx.Timeout(_DEFAULT_TIMEOUT_S)
    headers = {"User-Agent": "MochiBot/1.0 (+https://github.com/shikidmsh-rgb/mochibot)"}

    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
        headers=headers,
    ) as client:
        for redirect_count in range(_MAX_REDIRECTS + 1):
            current, address = await asyncio.to_thread(
                _resolve_public_https_url,
                current,
            )
            if _SYSTEM_HTTPS_PROXY:
                request_url = current
                request_headers = {"Connection": "close"}
                extensions = None
            else:
                request_url, host_header, sni_hostname = _pinned_request_target(
                    current,
                    address,
                )
                request_headers = {
                    "Host": host_header,
                    "Connection": "close",
                }
                extensions = {"sni_hostname": sni_hostname.encode("idna")}
            async with client.stream(
                "GET",
                request_url,
                headers=request_headers,
                extensions=extensions,
            ) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("Page redirect did not include a destination.")
                    if redirect_count >= _MAX_REDIRECTS:
                        raise ValueError("Page redirected too many times.")
                    current = urljoin(current, location)
                    continue

                response.raise_for_status()
                content_type = response.headers.get("content-type", "")
                media_type = content_type.split(";", 1)[0].strip().lower()
                if media_type and media_type not in {
                    "text/html",
                    "text/plain",
                    "application/xhtml+xml",
                }:
                    raise ValueError(
                        f"Unsupported page content type: {media_type}"
                    )
                declared_size = response.headers.get("content-length")
                if declared_size:
                    try:
                        if int(declared_size) > _MAX_RESPONSE_BYTES:
                            raise ValueError(
                                "Page response is larger than the 2 MB limit."
                            )
                    except ValueError as exc:
                        if "larger than" in str(exc):
                            raise

                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > _MAX_RESPONSE_BYTES:
                        raise ValueError(
                            "Page response is larger than the 2 MB limit."
                        )
                    chunks.append(chunk)

                text = _extract_readable_text(
                    b"".join(chunks),
                    content_type,
                    response.charset_encoding,
                )
                if not text:
                    raise ValueError("Page did not contain readable text.")
                truncated = len(text) > _MAX_EXTRACTED_CHARS
                text = text[:_MAX_EXTRACTED_CHARS].rstrip()
                suffix = (
                    "\n\n[Page text truncated at 20,000 characters.]"
                    if truncated
                    else ""
                )
                return f"URL: {current}\n\n{text}{suffix}"

    raise ValueError("Page could not be read.")


# ---------------------------------------------------------------------------
# Skill handler
# ---------------------------------------------------------------------------

class WebSearchSkill(Skill):
    async def execute(self, context: SkillContext) -> SkillResult:
        if context.tool_name == "read_web_page":
            url = (context.args.get("url") or "").strip()
            if not url:
                return SkillResult(output="Page URL is empty.", success=False)
            try:
                result = await _read_web_page(url)
                return SkillResult(output=result)
            except (httpx.HTTPError, ValueError, OSError) as exc:
                log.error("Web page read failed: %s", exc)
                return SkillResult(
                    output=f"Page read error: {exc}",
                    success=False,
                )

        if context.tool_name != "web_search":
            return SkillResult(output=f"Unknown tool: {context.tool_name}", success=False)

        query = (context.args.get("query") or "").strip()
        if not query:
            return SkillResult(output="Search query is empty.", success=False)
        if len(query) > _MAX_QUERY_LEN:
            return SkillResult(
                output=f"Query too long ({len(query)} chars, max {_MAX_QUERY_LEN}).",
                success=False,
            )

        max_results = context.args.get("max_results", _DEFAULT_MAX_RESULTS)
        max_results = max(1, min(10, int(max_results)))

        try:
            result = await _bing_search(
                query,
                max_results=max_results,
                timeout_s=_DEFAULT_TIMEOUT_S,
            )
            return SkillResult(output=result)
        except Exception as e:
            log.error("Web search failed: %s", e)
            return SkillResult(output=f"Search error: {e}", success=False)
