import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

import mochi.skills.web_search.handler as web_handler
from mochi.ai_client import (
    _clean_model_reply,
    _format_current_time_context,
    _meal_source_for_current_message,
    _tool_loop_exhaustion_message,
)
from mochi.skills.base import SkillContext
from mochi.skills.perception.handler import PerceptionSkill
from mochi.skills.web_search.handler import (
    WebSearchSkill,
    _extract_readable_text,
    _validate_public_https_url,
)
from mochi.transport import ImageAttachment
from mochi.transport.utils import split_bubbles


def test_model_facing_runtime_text_stays_truthful_and_natural():
    now = datetime(
        2026, 8, 17, 10, 3, 41,
        tzinfo=timezone(timedelta(hours=8)),
    )
    assert _format_current_time_context(now) == (
        "当前时间：2026-08-17 10:03:41 +0800（星期一）"
    )
    assert _meal_source_for_current_message(None) == "text"
    assert _meal_source_for_current_message(
        ImageAttachment(data=b"image"),
    ) == "photo"

    outcomes = [
        (
            True,
            [
                {"status": "failed", "state_changed": False},
                {"status": "success", "state_changed": True},
            ],
            "刚才只处理成功了一部分，剩下的还没改完。",
        ),
        (
            True,
            [{"status": "success", "state_changed": True}],
            "处理已经完成。",
        ),
        (
            False,
            [{"status": "failed", "state_changed": False}],
            "处理过程出了点问题，你再说一次试试？",
        ),
    ]
    for successful_effects, tool_audit, expected in outcomes:
        assert _tool_loop_exhaustion_message(
            successful_effects=successful_effects,
            tool_audit=tool_audit,
        ) == expected

    reply = _clean_model_reply(
        "First thought ||| Second thought\n\n"
        "Use `left ||| right` in code."
    )
    assert reply == (
        "First thought\n\nSecond thought\n\nUse `left ||| right` in code."
    )
    assert split_bubbles(reply) == [
        "First thought",
        "Second thought",
        "Use `left ||| right` in code.",
    ]
    fenced = "Here\n\n```python\nprint(1)\n\nprint(2)\n```\n\nDone"
    assert split_bubbles(fenced) == [
        "Here",
        "```python\nprint(1)\n\nprint(2)\n```\n\nDone",
    ]
    many = "\n\n".join(f"paragraph {index}" for index in range(10))
    bubbles = split_bubbles(many, max_bubbles=3)
    assert len(bubbles) == 3
    assert "paragraph 9" in bubbles[-1]


def test_look_around_can_read_all_bounded_details(monkeypatch):
    captured = {}

    def fake_read_cached_views(sources, *, detail):
        captured.update(sources=sources, detail=detail)
        return [{"source": f"source-{index}"} for index in range(5)]

    monkeypatch.setattr(
        "mochi.skills.perception.handler.observers.read_cached_views",
        fake_read_cached_views,
    )
    skill = PerceptionSkill()
    result = asyncio.run(skill.execute(SkillContext(
        trigger="tool_call",
        actor="main",
        tool_name="look_around",
        args={"detail": True},
    )))
    payload = json.loads(result.output)

    assert captured == {"sources": None, "detail": True}
    assert payload["mode"] == "detail"
    assert len(payload["sources"]) == 5
    schema = skill.get_tools()[0]["function"]["parameters"]["properties"]
    assert "maxItems" not in schema["sources"]


def test_web_reader_is_safe_readable_and_routed(monkeypatch):
    unsafe_urls = (
        "http://example.com/page",
        "https://localhost/page",
        "https://127.0.0.1/page",
        "https://10.0.0.1/page",
        "https://198.18.0.1/page",
        "******example.com/page",
    )
    for url in unsafe_urls:
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
    assert _extract_readable_text(gbk_html, "text/html", None) == "你好，世界"

    assert {
        (tool["function"]["name"], tool["_load"])
        for tool in WebSearchSkill().get_tools()
    } == {
        ("web_search", "routed"),
        ("read_web_page", "routed"),
    }
