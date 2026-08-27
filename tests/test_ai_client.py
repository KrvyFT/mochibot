"""Bounded public web access contract."""

import pytest

import mochi.skills.web_search.handler as web_handler
from mochi.skills.web_search.handler import (
    WebSearchSkill,
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
    assert {
        (tool["function"]["name"], tool["_load"])
        for tool in WebSearchSkill().get_tools()
    } == {
        ("web_search", "routed"),
        ("read_web_page", "routed"),
    }
