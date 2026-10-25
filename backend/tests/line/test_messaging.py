from __future__ import annotations

import json

import httpx
import pytest
import respx

from app.line.messaging import LineMessenger
from app.worker.ports import MessengerError, PushQuotaExceeded, ReplyTokenInvalid

MSG = [{"type": "text", "text": "hi"}]


@pytest.fixture
async def messenger():
    async with httpx.AsyncClient() as client:
        yield LineMessenger("cat_test", client)


@respx.mock
async def test_reply_request_format(messenger):
    route = respx.post("https://api.line.me/v2/bot/message/reply").mock(return_value=httpx.Response(200, json={}))
    await messenger.reply("rt", MSG)
    req = route.calls.last.request
    assert req.headers["Authorization"] == "Bearer cat_test"
    assert json.loads(req.content) == {"replyToken": "rt", "messages": MSG}


@respx.mock
async def test_invalid_reply_token_mapped(messenger):
    respx.post("https://api.line.me/v2/bot/message/reply").mock(
        return_value=httpx.Response(400, json={"message": "Invalid reply token"})
    )
    with pytest.raises(ReplyTokenInvalid):
        await messenger.reply("rt", MSG)


@respx.mock
async def test_other_reply_error_is_generic(messenger):
    respx.post("https://api.line.me/v2/bot/message/reply").mock(return_value=httpx.Response(500))
    with pytest.raises(MessengerError) as exc:
        await messenger.reply("rt", MSG)
    assert not isinstance(exc.value, ReplyTokenInvalid)


@respx.mock
async def test_push_sends_retry_key(messenger):
    route = respx.post("https://api.line.me/v2/bot/message/push").mock(return_value=httpx.Response(200, json={}))
    await messenger.push("U1", MSG, retry_key="0f8c2a6e-3b1d-4c9a-9e7f-5a2b8c4d6e1f")
    req = route.calls.last.request
    assert req.headers["X-Line-Retry-Key"] == "0f8c2a6e-3b1d-4c9a-9e7f-5a2b8c4d6e1f"
    assert json.loads(req.content) == {"to": "U1", "messages": MSG}


@respx.mock
async def test_push_duplicate_retry_key_treated_as_sent(messenger):
    respx.post("https://api.line.me/v2/bot/message/push").mock(return_value=httpx.Response(409))
    await messenger.push("U1", MSG, retry_key="k")


@respx.mock
async def test_push_monthly_limit_mapped(messenger):
    respx.post("https://api.line.me/v2/bot/message/push").mock(
        return_value=httpx.Response(429, json={"message": "You have reached your monthly limit."})
    )
    with pytest.raises(PushQuotaExceeded):
        await messenger.push("U1", MSG, retry_key="k")


@respx.mock
async def test_loading_request_format(messenger):
    route = respx.post("https://api.line.me/v2/bot/chat/loading/start").mock(
        return_value=httpx.Response(202, json={})
    )
    await messenger.start_loading("U1", 20)
    assert json.loads(route.calls.last.request.content) == {"chatId": "U1", "loadingSeconds": 20}
