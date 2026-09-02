"""Web search and bounded public web-page reading."""

import asyncio
import hashlib
import ipaddress
import json
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
_DEFAULT_TIMEOUT_S = 20
_DEFAULT_MAX_RESULTS = 5
_SEARCH_TIMEOUT_S = 10
_SEARCH_MAX_RESPONSE_BYTES = 1024 * 1024
_BING_SEARCH_URL = "https://www.bing.com/search"
_BAIDU_SEARCH_URL = "https://qianfan.baidubce.com/v2/ai_search/web_search"
_BAIDU_RECENCY_VALUES = frozenset({"week", "month", "semiyear", "year"})
_CACHE_TTL_S = 300
_CACHE_SIZE = 256
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

_ASCII_LETTER_RE = re.compile(r"[A-Za-z]")
_CJK_RE = re.compile(
    "["
    "\u2e80-\u2eff"
    "\u3040-\u30ff"
    "\u3100-\u312f"
    "\u31a0-\u31bf"
    "\u31c0-\u31ef"
    "\u3400-\u4dbf"
    "\u4e00-\u9fff"
    "\uac00-\ud7af"
    "\uf900-\ufaff"
    "\U00020000-\U0002fa1f"
    "]"
)


def _uses_english_search(query: str) -> bool:
    return bool(_ASCII_LETTER_RE.search(query)) and not _CJK_RE.search(query)


def _bing_request_options(
    query: str,
    max_results: int,
) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    english = _uses_english_search(query)
    headers = {
        "Accept-Language": "en-US,en;q=0.9" if english else "zh-CN,zh;q=0.9",
        "User-Agent": (
            "Mozilla/5.0"
            if english
            else (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/128.0.0.0 Safari/537.36"
            )
        ),
    }
    params = {"q": query, "count": str(max_results)}
    cookies: dict[str, str] = {}
    if english:
        params["ensearch"] = "1"
        cookies["SRCHHPGUSR"] = "SRCHLANG=EN"
    return headers, params, cookies


def _single_line(parts: list[str]) -> str:
    return re.sub(r"\s+", " ", "".join(parts)).strip()


class _BingSearchParser(HTMLParser):
    """Extract organic results from Bing's li.b_algo result cards."""

    def __init__(self, max_results: int) -> None:
        super().__init__(convert_charrefs=True)
        self.max_results = max_results
        self.results: list[tuple[str, str, str]] = []
        self._result_depth = 0
        self._h2_depth = 0
        self._in_title_link = False
        self._in_snippet = False
        self._snippet_finished = False
        self._title_parts: list[str] = []
        self._snippet_parts: list[str] = []
        self._href = ""

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attrs_by_name = dict(attrs)
        if tag == "li":
            classes = (attrs_by_name.get("class") or "").split()
            if self._result_depth:
                self._result_depth += 1
            elif "b_algo" in classes and len(self.results) < self.max_results:
                self._result_depth = 1
            return
        if not self._result_depth:
            return
        if tag == "h2":
            self._h2_depth += 1
        elif tag == "a" and self._h2_depth and not self._in_title_link:
            self._in_title_link = True
            self._href = attrs_by_name.get("href") or ""
        elif tag == "p" and not self._snippet_finished:
            self._in_snippet = True

    def handle_endtag(self, tag: str) -> None:
        if not self._result_depth:
            return
        if tag == "a" and self._in_title_link:
            self._in_title_link = False
        elif tag == "h2" and self._h2_depth:
            self._h2_depth -= 1
        elif tag == "p" and self._in_snippet:
            self._in_snippet = False
            self._snippet_finished = True
        elif tag == "li":
            self._result_depth -= 1
            if not self._result_depth:
                self._finish_result()

    def handle_data(self, data: str) -> None:
        if self._in_title_link:
            self._title_parts.append(data)
        if self._in_snippet:
            self._snippet_parts.append(data)

    def _finish_result(self) -> None:
        title = _single_line(self._title_parts)
        href = self._href.strip()
        snippet = _single_line(self._snippet_parts)[:200]
        if title and href:
            self.results.append((title, href, snippet))
        self._h2_depth = 0
        self._in_title_link = False
        self._in_snippet = False
        self._snippet_finished = False
        self._title_parts = []
        self._snippet_parts = []
        self._href = ""


def _extract_bing_results(
    body: bytes,
    encoding: str | None,
    max_results: int,
) -> str:
    detected_encoding = _detect_page_encoding(body, encoding)
    try:
        html = body.decode(detected_encoding, errors="replace")
    except LookupError:
        html = body.decode("utf-8", errors="replace")

    parser = _BingSearchParser(max_results)
    parser.feed(html)
    parser.close()
    if not parser.results:
        return "[0 results]"
    return "\n\n".join(
        f"{index}. {title}\n   {href}\n   {snippet}"
        for index, (title, href, snippet) in enumerate(parser.results, 1)
    )


async def _bing_search(query: str, max_results: int = 5) -> str:
    cache_key = f"bing|{query}|{max_results}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        async with asyncio.timeout(_SEARCH_TIMEOUT_S):
            output = await _bing_search_within_deadline(query, max_results)
    except TimeoutError as exc:
        raise ValueError(
            f"Search timed out after {_SEARCH_TIMEOUT_S} seconds."
        ) from exc
    if output == "[0 results]":
        raise ValueError("Bing search returned no results.")
    _cache.put(cache_key, output)
    return output


async def _bing_search_within_deadline(query: str, max_results: int) -> str:
    headers, params, cookies = _bing_request_options(query, max_results)

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(_SEARCH_TIMEOUT_S),
        headers=headers,
        cookies=cookies,
        follow_redirects=True,
    ) as client:
        async with client.stream(
            "GET",
            _BING_SEARCH_URL,
            params=params,
        ) as response:
            response.raise_for_status()
            declared_size = response.headers.get("content-length")
            if declared_size:
                try:
                    too_large = int(declared_size) > _SEARCH_MAX_RESPONSE_BYTES
                except ValueError:
                    too_large = False
                if too_large:
                    raise ValueError("Search response is larger than the 1 MB limit.")

            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > _SEARCH_MAX_RESPONSE_BYTES:
                    raise ValueError("Search response is larger than the 1 MB limit.")
                chunks.append(chunk)

            return await asyncio.to_thread(
                _extract_bing_results,
                b"".join(chunks),
                response.charset_encoding,
                max_results,
            )


def _format_baidu_results(payload: dict, max_results: int) -> str:
    code = payload.get("code")
    if code:
        raise ValueError(f"Baidu search returned error code {code}.")

    raw_references = payload.get("references")
    if isinstance(raw_references, dict):
        raw_references = [raw_references]
    if not isinstance(raw_references, list):
        raise ValueError("Baidu search response did not contain references.")

    results: list[str] = []
    for item in raw_references:
        if not isinstance(item, dict) or item.get("type", "web") != "web":
            continue
        title = _single_line([str(item.get("title") or item.get("web_anchor") or "")])
        url = str(item.get("url") or "").strip()
        snippet = _single_line([
            str(item.get("snippet") or item.get("content") or "")
        ])[:500]
        if not title or not url:
            continue
        details = " · ".join(
            value
            for value in (
                _single_line([str(item.get("website") or "")]),
                _single_line([str(item.get("date") or "")]),
            )
            if value
        )
        description = " · ".join(value for value in (details, snippet) if value)
        results.append(f"{len(results) + 1}. {title}\n   {url}\n   {description}")
        if len(results) >= max_results:
            break

    if not results:
        raise ValueError("Baidu search returned no web results.")
    return "\n\n".join(results)


async def _baidu_search(
    query: str,
    *,
    api_key: str,
    max_results: int = 5,
    recency: str = "",
    use_cache: bool = True,
) -> str:
    key_fingerprint = hashlib.sha256(api_key.encode()).hexdigest()[:12]
    cache_key = f"baidu|{key_fingerprint}|{query}|{max_results}|{recency}"
    if use_cache:
        cached = _cache.get(cache_key)
        if cached is not None:
            return cached

    payload: dict = {
        "messages": [{"role": "user", "content": query}],
        "search_source": "baidu_search_v2",
        "resource_type_filter": [{"type": "web", "top_k": max_results}],
        "sort": {"priority": "auto"},
    }
    if recency:
        payload["search_recency_filter"] = recency

    try:
        async with asyncio.timeout(_SEARCH_TIMEOUT_S):
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(_SEARCH_TIMEOUT_S),
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "User-Agent": "MochiBot/1.0",
                },
            ) as client:
                async with client.stream(
                    "POST",
                    _BAIDU_SEARCH_URL,
                    json=payload,
                ) as response:
                    if response.status_code in {401, 403}:
                        raise ValueError("Baidu API key was rejected.")
                    if response.status_code in {402, 429}:
                        raise ValueError("Baidu search quota is unavailable.")
                    if not 200 <= response.status_code < 300:
                        raise ValueError(
                            f"Baidu search returned HTTP {response.status_code}."
                        )

                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > _SEARCH_MAX_RESPONSE_BYTES:
                            raise ValueError(
                                "Baidu search response is larger than the 1 MB limit."
                            )
                        chunks.append(chunk)
    except TimeoutError as exc:
        raise ValueError(
            f"Baidu search timed out after {_SEARCH_TIMEOUT_S} seconds."
        ) from exc

    try:
        response_payload = json.loads(b"".join(chunks))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Baidu search returned invalid JSON.") from exc
    if not isinstance(response_payload, dict):
        raise ValueError("Baidu search returned an invalid response.")

    output = _format_baidu_results(response_payload, max_results)
    if use_cache:
        _cache.put(cache_key, output)
    return output


def _search_error_label(exc: Exception) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "request timed out"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.HTTPError):
        return "network request failed"
    if isinstance(exc, OSError):
        return "network connection failed"
    return str(exc).rstrip(".")


async def _web_search(
    query: str,
    *,
    api_key: str,
    max_results: int,
    recency: str,
) -> str:
    if not api_key:
        output = await _bing_search(query, max_results=max_results)
        if recency:
            return (
                "[Bing does not support the requested recency filter; "
                "these results may fall outside that window.]\n\n"
                f"{output}"
            )
        return output

    try:
        return await _baidu_search(
            query,
            api_key=api_key,
            max_results=max_results,
            recency=recency,
        )
    except (httpx.HTTPError, ValueError, OSError) as baidu_error:
        reason = _search_error_label(baidu_error)
        log.warning("Baidu search unavailable; using Bing fallback: %s", reason)
        try:
            fallback = await _bing_search(query, max_results=max_results)
        except (httpx.HTTPError, ValueError, OSError) as bing_error:
            raise ValueError(
                "Baidu search failed "
                f"({reason}); Bing fallback failed ({_search_error_label(bing_error)})."
            ) from bing_error
        return (
            f"[Baidu search unavailable: {reason}. "
            "The following results are from Bing fallback"
            + (
                " and the requested recency filter was not enforced"
                if recency
                else ""
            )
            + ".]\n\n"
            f"{fallback}"
        )


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
                return SkillResult(output=result, content_source="external_web")
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
        recency = str(context.args.get("recency") or "").strip().lower()
        if recency and recency not in _BAIDU_RECENCY_VALUES:
            return SkillResult(
                output=(
                    "Invalid recency. Use week, month, semiyear, year, "
                    "or leave it empty."
                ),
                success=False,
            )

        try:
            result = await _web_search(
                query,
                api_key=str(self.config.get("BAIDU_API_KEY") or "").strip(),
                max_results=max_results,
                recency=recency,
            )
            return SkillResult(output=result, content_source="external_web")
        except (httpx.HTTPError, ValueError, OSError) as exc:
            error = _search_error_label(exc)
            log.error("Web search failed: %s", error)
            return SkillResult(output=f"Search error: {error}", success=False)
