"""對話處理器端到端（假的 NOVARK 解析器；資料庫為正式設定的檔案型 SQLite）。"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.chat import ConversationHandler
from app.db import Base
from app.db.engine import create_engine, session_factory
from app.db.models import Event, InboundEvent, Organization, Team, TeamMember, User
from app.llm import ParseResult
from app.llm.schemas import ParsedEvent, ParsedQuery, ParsedSchedule, ParsedTarget
from app.worker import ChatJob

NOW = datetime(2026, 10, 21, 2, 0, tzinfo=timezone.utc)  # 台北 10/21（三）10:00
LIFF = "https://liff.line.me/app"


class FakeParser:
    def __init__(self, sessions=None):
        self.replies: dict[str, ParsedSchedule] = {}
        self.calls: list[tuple[str, dict | None]] = []
        self.sessions = sessions
        self.write_during_parse = False

    async def parse_schedule(self, message, ctx, *, request_id, pending_draft=None, deadline=None):
        self.calls.append((message, pending_draft))
        if self.write_during_parse:
            # 模擬 webhook 在 NOVARK 呼叫期間寫入；若處理器占著交易，這裡會被鎖住
            async with self.sessions() as s:
                s.add(InboundEvent(channel_id="C", webhook_event_id=uuid.uuid4().hex, payload={}))
                await s.commit()
        return ParseResult(parsed=self.replies[message], trace_id="t", calls=[])


def create(title="國網中心拜訪", start=datetime(2026, 11, 1, 14), **kw) -> ParsedSchedule:
    return ParsedSchedule(intent="create", event=ParsedEvent(title=title, start_local=start, **kw))


@pytest.fixture
async def env(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'chat.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = session_factory(engine)
    async with sessions() as s:
        org = Organization(name="公司")
        s.add(org)
        await s.flush()
        me, mate = User(organization_id=org.id, display_name="我"), User(organization_id=org.id, display_name="王小明")
        s.add_all([me, mate])
        await s.flush()
        team = Team(organization_id=org.id, name="業務部")
        s.add(team)
        await s.flush()
        s.add_all([TeamMember(organization_id=org.id, team_id=team.id, user_id=u.id) for u in (me, mate)])
        await s.commit()
    parser = FakeParser(sessions)
    handler = ConversationHandler(sessions, parser, liff_url=LIFF, now=lambda: NOW)
    yield handler, parser, sessions, me, mate
    await engine.dispose()


def job(user, text=None, *, kind="text", postback=None):
    payload = {"kind": kind}
    if postback:
        payload["postback"] = postback
    return ChatJob(job_id=uuid.uuid4().hex, user_id=str(user.id), line_user_id="U1", message_text=text,
                   received_at=NOW, reply_token="rt", reply_deadline=NOW + timedelta(seconds=30), payload=payload)


async def say(handler, user, text):
    return (await handler.handle(job(user, text), deadline=None)).messages


async def tap(handler, user, data):
    return (await handler.handle(job(user, kind="postback", postback=data), deadline=None)).messages


def card_postback(msg, label):
    for b in msg["contents"]["footer"]["contents"]:
        if b["action"]["label"] == label:
            return b["action"]["data"]
    raise AssertionError(label)


def card_texts(msg):
    return [c["text"] for c in msg["contents"]["body"]["contents"]]


async def event_count(sessions):
    async with sessions() as s:
        return await s.scalar(select(func.count()).select_from(Event).where(Event.status == "active"))


async def test_create_card_then_confirm(env):
    handler, parser, sessions, me, _ = env
    parser.replies["11/1 14:00 國網中心拜訪"] = create()

    [card] = await say(handler, me, "11/1 14:00 國網中心拜訪")
    assert card["type"] == "flex"
    body = card_texts(card)
    assert "國網中心拜訪" in body and "2026/11/01（日） 14:00–15:00" in body
    assert "結束時間為預設 1 小時" in body
    assert not any(t.startswith("分享") for t in body)  # 預設團隊可見，不再提示分享範圍
    assert not any(t.startswith("地點") for t in body)  # 沒提供地點就不顯示（§5.1）
    assert await event_count(sessions) == 0

    [done] = await tap(handler, me, card_postback(card, "確認"))
    assert done["text"].startswith("已建立：國網中心拜訪\n2026/11/01（日） 14:00–15:00")
    assert await event_count(sessions) == 1

    [again] = await tap(handler, me, card_postback(card, "確認"))  # A06 連點
    assert again["text"].startswith("（先前已確認）已建立")
    assert await event_count(sessions) == 1


async def test_plain_confirm_word_commits_single_pending_draft(env):
    handler, parser, sessions, me, _ = env
    parser.replies["11/1 14:00 國網中心拜訪"] = create()
    await say(handler, me, "11/1 14:00 國網中心拜訪")

    [done] = await say(handler, me, "確認")

    assert done["text"].startswith("已建立")
    assert [c[0] for c in parser.calls] == ["11/1 14:00 國網中心拜訪"]  # 「確認」不送 NOVARK


async def test_a03_missing_time_asks_then_merges_answer(env):
    handler, parser, sessions, me, _ = env
    parser.replies["明天下午拜訪客戶"] = ParsedSchedule(
        intent="create", event=ParsedEvent(title="拜訪客戶"), missing_fields=["start_local"])
    parser.replies["兩點"] = create("拜訪客戶", datetime(2026, 10, 22, 14))

    [question] = await say(handler, me, "明天下午拜訪客戶")
    assert question["text"] == "請問幾點開始？（例如 14:00）"

    [card] = await say(handler, me, "兩點")
    message, pending = parser.calls[-1]
    assert pending["original_message"] == "明天下午拜訪客戶"  # 上一輪草稿交給模型合併
    assert "2026/10/22（四） 14:00–15:00" in card_texts(card)
    assert card_postback(card, "確認").endswith("&v=2")  # 合併到同一份草稿，版本遞增


async def test_year_inferred_note_on_card(env):
    handler, parser, _, me, _ = env
    parser.replies["1/5 10:00 開工"] = create("開工", datetime(2026, 1, 5, 10))
    [card] = await say(handler, me, "1/5 10:00 開工")
    body = card_texts(card)
    assert "2027/01/05（二） 10:00–11:00" in body and "年份推定為明年（2027）" in body


async def test_this_week_day_passed_asks(env):
    handler, parser, _, me, _ = env
    parser.replies["這週一下午3點開會"] = create("開會", datetime(2026, 10, 19, 15))
    [question] = await say(handler, me, "這週一下午3點開會")
    assert question["text"].startswith("這週的那一天已經過了")


async def test_stale_card_after_edit(env):
    handler, parser, _, me, _ = env
    parser.replies["明天下午拜訪客戶"] = ParsedSchedule(
        intent="create", event=ParsedEvent(title="拜訪客戶"), missing_fields=["start_local"])
    parser.replies["兩點"] = create("拜訪客戶", datetime(2026, 10, 22, 14))
    parser.replies["改三點"] = create("拜訪客戶", datetime(2026, 10, 22, 15))
    await say(handler, me, "明天下午拜訪客戶")
    [old_card] = await say(handler, me, "兩點")
    # 草稿已完整後再說一次 → 建立新草稿；舊卡仍可確認自己那一版
    [new_card] = await say(handler, me, "改三點")
    assert card_postback(old_card, "確認") != card_postback(new_card, "確認")


async def test_query_shows_busy_for_teammate_and_scope_note(env):
    handler, parser, sessions, me, mate = env
    async with sessions() as s:
        s.add(Event(organization_id=mate.organization_id, owner_user_id=mate.id, title="祕密拜訪",
                    start_at=datetime(2026, 10, 22, 6, tzinfo=timezone.utc),
                    end_at=datetime(2026, 10, 22, 7, tzinfo=timezone.utc), timezone="Asia/Taipei",
                    visibility="busy_only", source="manual"))
        await s.commit()
    parser.replies["這週大家的行程"] = ParsedSchedule(intent="query", query=ParsedQuery())

    [reply] = await say(handler, me, "這週大家的行程")
    assert "王小明：忙碌" in reply["text"] and "祕密" not in reply["text"]
    assert "2026/10/22（四） 14:00–15:00" in reply["text"]
    assert "僅顯示本系統已登錄且您有權限查看的行程" in reply["text"]


async def test_a14_empty_query_does_not_promise_free_time(env):
    handler, parser, _, me, _ = env
    parser.replies["王小明明天有空嗎"] = ParsedSchedule(intent="query", query=ParsedQuery(
        person_names=["王小明"], range_start=date(2026, 10, 22), range_end=date(2026, 10, 22)))
    [reply] = await say(handler, me, "王小明明天有空嗎")
    assert reply["text"].startswith("本系統沒有已登錄行程")


async def test_unknown_member_in_query(env):
    handler, parser, _, me, _ = env
    parser.replies["李經理明天忙嗎"] = ParsedSchedule(intent="query", query=ParsedQuery(person_names=["李經理"]))
    [reply] = await say(handler, me, "李經理明天忙嗎")
    assert reply["text"] == "找不到成員「李經理」。"


async def _make_event(handler, parser, me, msg, parsed):
    parser.replies[msg] = parsed
    [card] = await say(handler, me, msg)
    await tap(handler, me, card_postback(card, "確認"))


async def test_cancel_single_match(env):
    handler, parser, sessions, me, _ = env
    await _make_event(handler, parser, me, "11/1 14:00 國網中心拜訪", create())
    parser.replies["取消國網中心拜訪"] = ParsedSchedule(intent="cancel", target=ParsedTarget(title_keyword="國網中心"))

    [card] = await say(handler, me, "取消國網中心拜訪")
    assert card_texts(card)[0] == "請確認取消行程"
    [done] = await tap(handler, me, card_postback(card, "確認取消"))
    assert done["text"].startswith("已取消：國網中心拜訪")
    assert await event_count(sessions) == 0


async def test_update_keeps_duration_and_shows_before(env):
    handler, parser, _, me, _ = env
    await _make_event(handler, parser, me, "11/1 14:00 國網中心拜訪",
                      create(end_local=datetime(2026, 11, 1, 16)))
    parser.replies["國網中心拜訪改到15點"] = ParsedSchedule(
        intent="update", target=ParsedTarget(title_keyword="國網中心"),
        event=ParsedEvent(start_local=datetime(2026, 11, 1, 15)))

    [card] = await say(handler, me, "國網中心拜訪改到15點")
    body = card_texts(card)
    assert "2026/11/01（日） 15:00–17:00" in body and "原本：2026/11/01（日） 14:00–16:00" in body


async def test_a05_multiple_matches_offer_choices(env):
    handler, parser, _, me, _ = env
    await _make_event(handler, parser, me, "a", create("週會", datetime(2026, 10, 26, 10)))
    await _make_event(handler, parser, me, "b", create("週會", datetime(2026, 11, 2, 10)))
    parser.replies["取消週會"] = ParsedSchedule(intent="cancel", target=ParsedTarget(title_keyword="週會"))

    [choice] = await say(handler, me, "取消週會")
    items = choice["quickReply"]["items"]
    assert [i["action"]["label"] for i in items] == ["10/26 10:00 週會", "11/02 10:00 週會"]

    [card] = await tap(handler, me, items[1]["action"]["data"])
    assert "2026/11/02（一） 10:00–11:00" in card_texts(card)


async def test_cancel_with_no_match(env):
    handler, parser, _, me, _ = env
    parser.replies["取消牙醫"] = ParsedSchedule(intent="cancel", target=ParsedTarget(title_keyword="牙醫"))
    [reply] = await say(handler, me, "取消牙醫")
    assert reply["text"].startswith("找不到符合的行程")


async def test_abandon_button(env):
    handler, parser, sessions, me, _ = env
    parser.replies["x"] = create()
    [card] = await say(handler, me, "x")
    [reply] = await tap(handler, me, card_postback(card, "放棄"))
    assert reply["text"] == "已放棄這份草稿。"
    [late] = await tap(handler, me, card_postback(card, "確認"))
    assert late["text"].startswith("這份草稿已過期或已放棄")


async def test_other_users_card_cannot_be_confirmed(env):
    handler, parser, _, me, mate = env
    parser.replies["x"] = create()
    [card] = await say(handler, mate, "x")
    [reply] = await tap(handler, me, card_postback(card, "確認"))
    assert reply["text"].startswith("找不到")


@pytest.mark.parametrize("intent,expected", [("unsupported", "這個需求目前不支援"), ("clarify", "我不太確定")])
async def test_unsupported_and_clarify(env, intent, expected):
    handler, parser, _, me, _ = env
    parser.replies["?"] = ParsedSchedule(intent=intent)
    [reply] = await say(handler, me, "?")
    assert reply["text"].startswith(expected)


async def test_non_text_unbound_and_inactive(env):
    handler, parser, sessions, me, _ = env
    assert (await handler.handle(job(me, kind="non_text"), deadline=None)).messages[0]["text"].startswith("目前只支援文字")
    unbound = (await handler.handle(job(me, kind="unbound"), deadline=None)).messages[0]["text"]
    assert unbound.startswith("您尚未完成成員綁定") and LIFF in unbound
    async with sessions() as s:
        (await s.get(User, me.id)).status = "suspended"
        await s.commit()
    [reply] = await say(handler, me, "x")
    assert reply["text"].startswith("您的帳號目前無法使用")
    assert parser.calls == []


async def test_novark_call_happens_outside_db_transaction(env):
    handler, parser, _, me, _ = env
    parser.replies["11/1 14:00 國網中心拜訪"] = create()
    parser.write_during_parse = True
    [card] = await say(handler, me, "11/1 14:00 國網中心拜訪")  # 若持有寫入鎖，模擬的 webhook 寫入會逾時失敗
    assert card["type"] == "flex"
