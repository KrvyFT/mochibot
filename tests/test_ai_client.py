"""Bounded public web access contract."""

import asyncio

import pytest

import mochi.skills.web_search.handler as web_handler
from mochi.skills.base import SkillContext
from mochi.skills.web_search.handler import (
    WebSearchSkill,
    _format_baidu_results,
    _extract_readable_text,
    _validate_public_https_url,
)


def test_web_reader_is_safe_readable_and_routed(monkeypatch):
    for url in (
        "http://example.com/page",
        "https://localhost/page",
        "https://127.0.0.1/page",
        "https://10.0.0.1/page",
        "https://198.18.0.1/page",
        "******example.com/page",
    ):
        with pytest.raises(ValueError):
            _validate_public_https_url(url)

    monkeypatch.setattr(web_handler, "_SYSTEM_HTTPS_PROXY", False)
    monkeypatch.setattr(
        web_handler.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(
            web_handler.socket.AF_INET,
            web_handler.socket.SOCK_STREAM,
            6,
            "",
            ("198.18.0.7", 443),
        )],
    )
    with pytest.raises(ValueError):
        _validate_public_https_url("https://attacker.example/page")

    html = (
        b"<html><body><h1>Title</h1><script>hidden()</script>"
        b"<p>Hello <strong>world</strong>.</p></body></html>"
    )
    assert _extract_readable_text(html, "text/html", "utf-8") == (
        "Title\n\nHello world."
    )
    gbk_html = '<meta charset="gbk"><p>你好，世界</p>'.encode("gbk")
    assert _extract_readable_text(gbk_html, "text/html", None) == (
        "你好，世界"
    )
    skill = WebSearchSkill()
    assert {
        (tool["function"]["name"], tool["_load"])
        for tool in skill.get_tools()
    } == {
        ("web_search", "routed"),
        ("read_web_page", "routed"),
    }
    assert skill.config_schema == [{
        "key": "BAIDU_API_KEY",
        "type": "str",
        "secret": True,
        "default": "",
        "description": "Optional Baidu Qianfan AI Search API key",
        "internal": False,
    }]

    formatted = _format_baidu_results(
        {
            "request_id": "request",
            "references": [
                {
                    "type": "web",
                    "title": "Current story",
                    "url": "https://news.example/story",
                    "snippet": "Verified summary",
                    "website": "Example News",
                    "date": "2026-08-28",
                },
                {
                    "type": "image",
                    "title": "Skipped image",
                    "url": "https://images.example/item",
                },
            ],
        },
        5,
    )
    assert "Current story" in formatted
    assert "2026-08-28" in formatted
    assert "Skipped image" not in formatted

    calls = []

    async def _baidu(query, *, api_key, max_results, recency):
        calls.append(("baidu", query, api_key, max_results, recency))
        return "1. Baidu result"

    async def _bing(query, max_results):
        calls.append(("bing", query, max_results))
        return "1. Bing result"

    monkeypatch.setattr(web_handler, "_baidu_search", _baidu)
    monkeypatch.setattr(web_handler, "_bing_search", _bing)
    skill.config = {"BAIDU_API_KEY": "secret"}
    result = asyncio.run(skill.execute(SkillContext(
        trigger="tool_call",
        tool_name="web_search",
        args={"query": "today", "max_results": 3, "recency": "week"},
    )))
    assert result.success
    assert result.output == "1. Baidu result"
    assert calls == [("baidu", "today", "secret", 3, "week")]

    async def _failed_baidu(*_args, **_kwargs):
        raise ValueError("Baidu API key was rejected.")

    monkeypatch.setattr(web_handler, "_baidu_search", _failed_baidu)
    fallback = asyncio.run(skill.execute(SkillContext(
        trigger="tool_call",
        tool_name="web_search",
        args={"query": "today", "recency": "week"},
    )))
    assert fallback.success
    assert "Bing fallback" in fallback.output
    assert "recency filter was not enforced" in fallback.output
    assert fallback.output.endswith("1. Bing result")
