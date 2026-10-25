"""Webhook：驗簽、去重、寫入佇列（§10、A06、A10）。"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.models import ExternalIdentity, Organization, User
from app.line.signature import sign

BACKEND = Path(__file__).resolve().parents[2]
SECRET = "test-channel-secret"
USER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000aa")


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = tmp_path / "app.db"
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    command.upgrade(cfg, "head")
    with Session(create_engine(f"sqlite:///{db}")) as s:
        org = Organization(name="公司")
        s.add(org)
        s.flush()
        s.add(User(id=USER_ID, organization_id=org.id, display_name="王小明"))
        s.flush()
        s.add(ExternalIdentity(user_id=USER_ID, provider="line", provider_scope="P1", subject="U-bound"))
        s.commit()
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db}")
    monkeypatch.setenv("LINE_CHANNEL_SECRET", SECRET)
    monkeypatch.setenv("WORKER_ENABLED", "false")

    from app.main import app

    with TestClient(app) as c:
        c.db_path = db
        yield c


def post(client, events, *, secret=SECRET, signature=None):
    body = json.dumps({"destination": "Ubot", "events": events}, ensure_ascii=False).encode()
    return client.post("/api/line/webhook", content=body,
                       headers={"x-line-signature": signature or sign(body, secret),
                                "content-type": "application/json"})


def text_event(event_id="E1", user="U-bound", text="明天10點開會", source_type="user"):
    return {"type": "message", "webhookEventId": event_id, "replyToken": f"rt-{event_id}",
            "source": {"type": source_type, "userId": user}, "timestamp": 1792890000000,
            "message": {"type": "text", "id": "m1", "text": text}}


def jobs(client):
    con = sqlite3.connect(client.db_path)
    rows = con.execute("select message_text, payload, reply_token, reply_deadline, user_id from async_jobs").fetchall()
    con.close()
    return [(t, json.loads(p), rt, dl, uid) for t, p, rt, dl, uid in rows]


def test_a10_bad_signature_rejected(client):
    assert post(client, [text_event()], signature="forged").status_code == 401
    assert post(client, [text_event()], secret="wrong-secret").status_code == 401
    assert jobs(client) == []


def test_verification_request_with_no_events(client):
    assert post(client, []).status_code == 200


def test_text_message_enqueued_with_reply_deadline(client):
    assert post(client, [text_event()]).status_code == 200
    [(text, payload, token, deadline, uid)] = jobs(client)
    assert (text, payload, token, uid) == ("明天10點開會", {"kind": "text"}, "rt-E1", USER_ID.hex)
    assert deadline is not None


def test_a06_redelivered_event_enqueued_once(client):
    post(client, [text_event("E1")])
    post(client, [text_event("E1")])  # LINE 重送
    post(client, [text_event("E1"), text_event("E2")])
    assert len(jobs(client)) == 2


def test_group_messages_ignored(client):
    post(client, [text_event(source_type="group")])
    assert jobs(client) == []


def test_unbound_user_gets_binding_job(client):
    post(client, [text_event(user="U-stranger")])
    [(_, payload, _, _, uid)] = jobs(client)
    assert payload == {"kind": "unbound"} and uid is None


def test_sticker_and_postback_kinds(client):
    sticker = {**text_event("E3"), "message": {"type": "sticker", "id": "s"}}
    postback = {"type": "postback", "webhookEventId": "E4", "replyToken": "rt-E4",
                "source": {"type": "user", "userId": "U-bound"}, "postback": {"data": "action=confirm&draft=x&v=1"}}
    post(client, [sticker, postback])
    kinds = sorted((j[1]["kind"], j[1].get("postback")) for j in jobs(client))
    assert kinds == [("non_text", None), ("postback", "action=confirm&draft=x&v=1")]
