from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from app.auth.line_login import VERIFY_URL, InvalidIdToken, LineIdTokenVerifier

CHANNEL = "1650000000"


def claims(**kw):
    return {"iss": "https://access.line.me", "sub": "U123", "aud": CHANNEL,
            "exp": int(time.time()) + 600, "iat": int(time.time()), "name": "王小明", **kw}


@pytest.fixture
async def verifier():
    async with httpx.AsyncClient() as client:
        yield LineIdTokenVerifier(CHANNEL, client)


@respx.mock
async def test_valid_token(verifier):
    route = respx.post(VERIFY_URL).mock(return_value=httpx.Response(200, json=claims()))
    result = await verifier.verify("tok")
    assert result.sub == "U123" and result.name == "王小明"
    assert route.calls.last.request.content.decode() == f"id_token=tok&client_id={CHANNEL}"


@pytest.mark.parametrize("bad", [{"aud": "other"}, {"iss": "https://evil"}, {"exp": 1}, {"sub": ""}])
@respx.mock
async def test_rejects_bad_claims(verifier, bad):
    respx.post(VERIFY_URL).mock(return_value=httpx.Response(200, json=claims(**bad)))
    with pytest.raises(InvalidIdToken):
        await verifier.verify("tok")


@respx.mock
async def test_rejects_when_line_says_invalid(verifier):
    respx.post(VERIFY_URL).mock(return_value=httpx.Response(400, json={"error": "invalid_request"}))
    with pytest.raises(InvalidIdToken):
        await verifier.verify("tok")


async def test_requires_channel_id():
    async with httpx.AsyncClient() as client:
        with pytest.raises(InvalidIdToken):
            await LineIdTokenVerifier("", client).verify("tok")


async def test_expired_invitation(tmp_path):
    from app.auth.service import create_invitation, preview_invitation
    from app.db import Base
    from app.db.engine import create_engine, session_factory
    from app.db.models import Organization, User
    from app.services.errors import InvitationInvalid

    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'x.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime(2026, 10, 21, tzinfo=timezone.utc)
    async with session_factory(engine)() as s:
        org = Organization(name="公司")
        s.add(org)
        await s.flush()
        member = User(organization_id=org.id, display_name="王小明")
        s.add(member)
        await s.flush()
        token = await create_invitation(s, member, now=now)
        assert (await preview_invitation(s, token, now=now + timedelta(hours=71))).display_name == "王小明"
        with pytest.raises(InvitationInvalid):
            await preview_invitation(s, token, now=now + timedelta(hours=72))
    await engine.dispose()
