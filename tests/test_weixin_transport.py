"""WeChat owner isolation contract."""

from unittest.mock import AsyncMock

import pytest

from mochi.transport.weixin import WeixinTransport


@pytest.mark.asyncio
async def test_non_owner_is_rejected_before_main_dispatch(monkeypatch):
    import mochi.transport.weixin as weixin

    transport = WeixinTransport()
    transport.restore_owner_id("owner", source="test")
    callback = AsyncMock()
    monkeypatch.setattr(weixin, "_on_message_callback", callback)

    await transport._handle_message({
        "from_user_id": "intruder",
        "context_token": "intruder-context",
        "item_list": [{"type": 1, "text_item": {"text": "hello"}}],
    })

    callback.assert_not_awaited()
    assert "intruder" not in transport._context_tokens
