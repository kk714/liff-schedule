"""對話處理器：LINE 文字／postback → 草稿、確認、查詢（設計文件 §5、§7、§9.1）。

NOVARK 呼叫要 5–8 秒，絕不能在資料庫交易開著時呼叫：SQLite 的 BEGIN IMMEDIATE 會一直占著寫入鎖，
webhook 寫不進來。所以文字訊息分三段：

    ① 短交易讀取：使用者、對話狀態、未完成草稿（「確認」等簡短回答在這裡直接處理）
    ② 交易外呼叫 NOVARK（request_id＝job_id；預算用完後背景重跑時沿用同一個 idempotency key）
    ③ 短交易寫入：依 intent 建立／合併草稿、找目標行程、查詢

    create  → 新草稿或合併到未完成草稿 → 完整則回確認卡，不完整則追問
    update／cancel → 在本人行程中找目標：0 筆說找不到、1 筆回確認卡、多筆列候選（quick reply，A05）
    query   → 權限過濾後以固定模板回覆（A14：查無資料不保證有空）
    postback → confirm／abandon（draft_id＋version，不信任卡片內容，§9.2）、pick（選候選）
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from typing import Any, Protocol
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import ConversationSession, Event, EventDraft, User
from app.llm.context import ParseContext
from app.llm.schemas import ParsedQuery, ParsedSchedule, ParsedTarget
from app.rules.dates import YEAR_INFERRED, apply_date_rules
from app.services import DraftFields, DraftInput, DraftService, EventQueryService, draft_input_from_parsed
from app.services.errors import (
    DraftGone,
    DraftVersionConflict,
    EventVersionConflict,
    InvalidDraft,
    NotFound,
    RangeTooLarge,
    ServiceError,
)
from app.worker.jobs import ChatJob, HandlerResult

from . import cards
from .cards import text, with_link

CONFIRM_WORDS = {"確認", "確定", "好", "好的", "是", "對", "ok", "OK", "Ok"}
CONVERSATION_TTL = timedelta(minutes=30)
MAX_CANDIDATES = 5


class Parser(Protocol):
    async def parse_schedule(self, message: str, ctx: ParseContext, *, request_id: str,
                             pending_draft: dict[str, Any] | None = None, deadline: float | None = None): ...


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ConversationHandler:
    def __init__(
        self, sessions: async_sessionmaker, parser: Parser, *, liff_url: str = "",
        drafts: DraftService | None = None, queries: EventQueryService | None = None,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._sessions = sessions
        self._parser = parser
        self._liff = liff_url
        self._drafts = drafts or DraftService()
        self._queries = queries or EventQueryService()
        self._now = now

    async def handle(self, job: ChatJob, *, deadline: float | None) -> HandlerResult:
        kind = job.payload.get("kind", "text")
        if kind == "unbound":
            return HandlerResult([text(with_link(cards.UNBOUND, self._liff))])
        if kind == "non_text":
            return HandlerResult([text(cards.NON_TEXT)])
        user_id = uuid.UUID(job.user_id)
        if kind == "postback":
            async with self._sessions() as s:
                user = await s.get(User, user_id)
                messages = await self._postback(s, user, job) if _active(user) else [text(cards.INACTIVE)]
                await s.commit()
            return HandlerResult(messages)
        return HandlerResult(await self._text(job, user_id, deadline))

    # ---- 文字訊息 -------------------------------------------------------------

    async def _text(self, job: ChatJob, user_id: uuid.UUID, deadline: float | None) -> list[dict[str, Any]]:
        now = self._now()
        message = (job.message_text or "").strip()

        # ① 短交易讀取
        async with self._sessions() as s:
            user = await s.get(User, user_id)
            if not _active(user):
                return [text(cards.INACTIVE)]
            conv = await self._conversation(s, user)
            pending = await self._pending_draft(s, conv, now)
            if message in CONFIRM_WORDS and pending is not None and pending.status == "awaiting_confirmation":
                # §5.3：恰有一筆待確認草稿時才接受自然語言「確認」
                result = await self._confirm(s, user, conv, pending.id, pending.draft_version, now, job.job_id)
                await s.commit()
                return result
            context = dict(conv.context_json) if conv else {}
            collecting = pending is not None and pending.status == "collecting"
            merge = (pending.id, pending.draft_version) if collecting else None
            pending_info = None
            if collecting and context.get("parsed"):
                pending_info = {"original_message": context.get("original_message"), "parsed": context["parsed"]}
            tz_name = user.timezone
            await s.rollback()  # 結束讀取交易、釋放 SQLite 鎖；之後不可再讀這個 session 的 ORM 物件

        # ② 交易外呼叫 NOVARK
        ctx = ParseContext(now=now.astimezone(ZoneInfo(tz_name)), timezone=tz_name)
        result = await self._parser.parse_schedule(
            message, ctx, request_id=job.job_id, pending_draft=pending_info, deadline=deadline
        )
        original = f"{pending_info['original_message']} {message}" if pending_info else message
        ruled = apply_date_rules(result.parsed, original, ctx)
        parsed = ruled.parsed

        # ③ 短交易寫入
        async with self._sessions() as s:
            user = await s.get(User, user_id)
            if not _active(user):
                return [text(cards.INACTIVE)]
            conv = await self._conversation(s, user)
            try:
                if parsed.intent == "create":
                    messages = await self._create(s, user, conv, parsed, ruled.flags, merge, original, now)
                elif parsed.intent in ("update", "cancel"):
                    messages = await self._modify(s, user, conv, parsed, message, now)
                elif parsed.intent == "query":
                    messages = await self._query(s, user, parsed.query or ParsedQuery(), now)
                elif parsed.intent == "unsupported":
                    messages = [text(cards.UNSUPPORTED)]
                else:
                    messages = [text(cards.CLARIFY)]
            except ServiceError as exc:
                await s.rollback()
                return [text(self._error_text(exc))]
            await s.commit()
            return messages

    async def _create(self, s, user, conv, parsed: ParsedSchedule, flags, merge, original, now):
        data = draft_input_from_parsed(parsed)
        blockers = list(parsed.ambiguities)
        draft = None
        if merge is not None:
            try:
                draft = await self._drafts.update(s, user, merge[0], merge[1], data.fields, now=now, blockers=blockers)
            except (DraftVersionConflict, DraftGone, NotFound):
                draft = None  # 原草稿已失效，改建新草稿
        if draft is None:
            draft = await self._drafts.create(
                s, user, DraftInput("create", data.fields, blockers=blockers), now=now
            )
        await self._save_conversation(s, user, conv, now, pending_draft_id=draft.id, context={
            "original_message": original, "parsed": parsed.model_dump(mode="json"),
        })
        return [self._draft_message(draft, user, now, flags=flags)]

    async def _modify(self, s, user, conv, parsed: ParsedSchedule, message: str, now):
        candidates = await self._find_targets(s, user, parsed.target or ParsedTarget())
        if not candidates:
            return [text(cards.NOT_FOUND)]
        fields = DraftFields() if parsed.intent == "cancel" else draft_input_from_parsed(parsed).fields
        if len(candidates) == 1:
            return await self._start_modify_draft(s, user, conv, parsed.intent, fields, candidates[0], message, now)

        tz = ZoneInfo(user.timezone)
        await self._save_conversation(s, user, conv, now, pending_draft_id=None, context={
            "selection": {"intent": parsed.intent, "fields": fields.model_dump(mode="json"),
                          "candidates": [str(e.id) for e in candidates], "message": message},
        })
        items = [
            {"type": "action", "action": {"type": "postback", "label": _short_label(e, tz),
                                          "data": f"action=pick&i={i}", "displayText": _short_label(e, tz)}}
            for i, e in enumerate(candidates)
        ]
        return [text("找到多筆符合的行程，請選擇：", quick_reply=items)]

    async def _start_modify_draft(self, s, user, conv, intent, fields, event: Event, message, now):
        draft = await self._drafts.create(s, user, DraftInput(intent, fields, event_id=event.id), now=now)
        await self._save_conversation(s, user, conv, now, pending_draft_id=draft.id,
                                      context={"original_message": message})
        return [self._draft_message(draft, user, now, event=event)]

    async def _find_targets(self, s: AsyncSession, user: User, target: ParsedTarget) -> list[Event]:
        tz = ZoneInfo(user.timezone)
        conditions = []
        if target.title_keyword:
            conditions.append(Event.title.contains(target.title_keyword, autoescape=True))
        if target.start_local is not None:
            conditions.append(Event.start_at == target.start_local.replace(tzinfo=tz))
        elif target.on_date is not None:
            day_start = datetime.combine(target.on_date, datetime.min.time(), tz)
            conditions.append(or_(
                and_(Event.all_day.is_(False), Event.start_at >= day_start,
                     Event.start_at < day_start + timedelta(days=1)),
                and_(Event.all_day.is_(True), Event.start_date <= target.on_date, Event.end_date > target.on_date),
            ))
        if not conditions:
            return []
        stmt = (
            select(Event)
            .where(Event.owner_user_id == user.id, Event.organization_id == user.organization_id,
                   Event.status == "active", *conditions)
            .order_by(Event.start_date.is_(None), Event.start_date, Event.start_at)
            .limit(MAX_CANDIDATES)
        )
        return list((await s.scalars(stmt)).all())

    async def _query(self, s, user: User, q: ParsedQuery, now: datetime):
        tz = ZoneInfo(user.timezone)
        today = now.astimezone(tz).date()
        first = q.range_start or (today - timedelta(days=today.weekday()))  # 預設本週（§7.2-7）
        last = q.range_end or (first if q.range_start else first + timedelta(days=6))
        if last < first:
            last = first
        member_ids: list[uuid.UUID] = []
        for name in q.person_names:
            matches = (await s.scalars(
                select(User.id).where(User.organization_id == user.organization_id, User.status == "active",
                                      User.display_name.contains(name, autoescape=True))
            )).all()
            if not matches:
                return [text(f"找不到成員「{name}」。")]
            member_ids.extend(matches)

        page = await self._queries.list_visible(
            s, user, start=datetime.combine(first, datetime.min.time(), tz),
            end=datetime.combine(last + timedelta(days=1), datetime.min.time(), tz),
            member_ids=member_ids or None, keyword=q.keyword,
        )
        if not page.items:
            return [text(f"{cards.NO_EVENTS}\n（{cards.SCOPE_NOTE}）")]
        lines = [f"{_range_title(first, last)}："]
        for v in page.items:
            who = "我" if v.owner_user_id == user.id else v.owner_name
            if v.all_day:
                span = cards.fmt_span(None, None, True, v.start_date, v.end_date)
            else:
                span = cards.fmt_span(v.start_at.astimezone(tz).replace(tzinfo=None),
                                      v.end_at.astimezone(tz).replace(tzinfo=None), False)
            lines.append(f"・{span} {who}：{v.title if v.projection == 'detail' else '忙碌'}")
        if page.has_more:
            lines.append("（還有更多，請縮小範圍）")
        lines.append(f"（{cards.SCOPE_NOTE}）")
        return [text(with_link("\n".join(lines), self._liff))]

    # ---- postback -------------------------------------------------------------

    async def _postback(self, s: AsyncSession, user: User, job: ChatJob) -> list[dict[str, Any]]:
        params = {k: v[0] for k, v in parse_qs(job.payload.get("postback", "")).items()}
        now = self._now()
        conv = await self._conversation(s, user)
        try:
            action = params.get("action")
            if action in ("confirm", "abandon"):
                draft_id, version = uuid.UUID(params["draft"]), int(params["v"])
                if action == "confirm":
                    return await self._confirm(s, user, conv, draft_id, version, now, job.job_id)
                await self._drafts.abandon(s, user, draft_id, version, now=now)
                await self._clear_pending(s, conv, draft_id)
                return [text("已放棄這份草稿。")]
            if action == "pick":
                return await self._pick(s, user, conv, int(params["i"]), now)
        except (KeyError, ValueError):
            pass
        except ServiceError as exc:
            await s.rollback()
            return [text(self._error_text(exc))]
        return [text(cards.CLARIFY)]

    async def _pick(self, s, user, conv, index: int, now):
        selection = (conv.context_json if conv else {}).get("selection")
        if not selection or not 0 <= index < len(selection["candidates"]):
            return [text("這個選項已失效，請重新輸入。")]
        event = await s.get(Event, uuid.UUID(selection["candidates"][index]))
        if event is None or event.owner_user_id != user.id or event.status != "active":
            return [text(cards.NOT_FOUND)]
        fields = DraftFields.model_validate(selection["fields"])
        return await self._start_modify_draft(s, user, conv, selection["intent"], fields, event,
                                              selection.get("message", ""), now)

    async def _confirm(self, s, user, conv, draft_id, version, now, request_id):
        try:
            result = await self._drafts.confirm(s, user, draft_id, version, now=now, request_id=request_id)
        except ServiceError as exc:
            return [text(self._error_text(exc))]
        await self._clear_pending(s, conv, draft_id)
        ev, tz = result.event, ZoneInfo(user.timezone)
        verb = {"create": "已建立", "update": "已更新", "cancel": "已取消"}[result.intent]
        prefix = "（先前已確認）" if result.already_committed else ""
        return [text(with_link(f"{prefix}{verb}：{ev.title}\n{_event_span(ev, tz)}", self._liff))]

    # ---- 回覆內容 -------------------------------------------------------------

    def _draft_message(self, draft: EventDraft, user: User, now: datetime, *,
                       flags: list[str] | None = None, event: Event | None = None) -> dict[str, Any]:
        p = draft.payload_json
        if draft.status == "collecting":
            return text(cards.question_for(p.get("missing_fields", [])))
        tz = ZoneInfo(user.timezone)
        notes: list[str] = []
        if draft.intent == "create":
            start, end = _iso(p.get("start_local")), _iso(p.get("end_local"))
            span = cards.fmt_span(start, end, bool(p.get("all_day")))
            title, location, visibility = p.get("title"), p.get("location"), p.get("visibility")
            defaults = p.get("defaults_applied", [])
            if "duration_minutes=60" in defaults:
                notes.append("結束時間為預設 1 小時")
            if any(d.startswith("visibility=") for d in defaults):
                notes.append("分享範圍為預設值，可按「修改」調整")
            if YEAR_INFERRED in (flags or []) and start is not None:
                notes.append(f"年份推定為明年（{start.year}）")
            if start is not None and start.replace(tzinfo=tz) < now:
                notes.append("這是過去的日期（補登）")
        else:
            assert event is not None
            title = p.get("title") or event.title
            location = p.get("location") or event.location
            visibility = p.get("visibility") or event.visibility
            span = _event_span(event, tz)
            if draft.intent == "update":
                new_span = _preview_update_span(event, p, tz)
                if new_span != span:
                    notes.append(f"原本：{span}")
                    span = new_span
        return cards.confirm_card(
            draft_id=str(draft.id), draft_version=draft.draft_version, intent=draft.intent,
            title=title, span=span, location=location, visibility=visibility, notes=notes, liff_url=self._liff,
        )

    @staticmethod
    def _error_text(exc: ServiceError) -> str:
        if isinstance(exc, DraftVersionConflict):
            return "這張確認卡已過期（內容已更新），請使用最新的確認卡。"
        if isinstance(exc, DraftGone):
            return "這份草稿已過期或已放棄，請重新輸入。"
        if isinstance(exc, EventVersionConflict):
            return "行程在這期間已被修改，請重新操作。"
        if isinstance(exc, NotFound):
            return cards.NOT_FOUND
        if isinstance(exc, RangeTooLarge):
            return "查詢範圍太大，請縮小到 90 天以內。"
        if isinstance(exc, InvalidDraft):
            return "資料還不完整，請補充後再確認。"
        return cards.INACTIVE

    # ---- 對話狀態 -------------------------------------------------------------

    async def _conversation(self, s: AsyncSession, user: User) -> ConversationSession | None:
        return await s.scalar(select(ConversationSession).where(
            ConversationSession.user_id == user.id, ConversationSession.channel == "line"))

    async def _pending_draft(self, s, conv, now) -> EventDraft | None:
        if conv is None or conv.pending_draft_id is None:
            return None
        draft = await s.get(EventDraft, conv.pending_draft_id)
        if draft and draft.status in ("collecting", "awaiting_confirmation") and draft.expires_at > now:
            return draft
        return None

    async def _save_conversation(self, s, user, conv, now, *, pending_draft_id, context) -> None:
        if conv is None:
            conv = ConversationSession(organization_id=user.organization_id, user_id=user.id, channel="line",
                                       expires_at=now + CONVERSATION_TTL)
            s.add(conv)
        conv.pending_draft_id = pending_draft_id
        conv.context_json = context
        conv.expires_at = now + CONVERSATION_TTL
        await s.flush()

    async def _clear_pending(self, s, conv, draft_id) -> None:
        if conv is not None and conv.pending_draft_id == draft_id:
            conv.pending_draft_id = None
            conv.context_json = {}
            await s.flush()


def _active(user: User | None) -> bool:
    return user is not None and user.status == "active"


def _iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _event_span(ev: Event, tz: ZoneInfo) -> str:
    if ev.all_day:
        return cards.fmt_span(None, None, True, ev.start_date, ev.end_date)
    return cards.fmt_span(ev.start_at.astimezone(tz).replace(tzinfo=None),
                          ev.end_at.astimezone(tz).replace(tzinfo=None), False)


def _preview_update_span(ev: Event, payload: dict[str, Any], tz: ZoneInfo) -> str:
    start, end = _iso(payload.get("start_local")), _iso(payload.get("end_local"))
    if start is None and end is None:
        return _event_span(ev, tz)
    if ev.all_day:
        span_days = ev.end_date - ev.start_date
        new_start = (start or datetime.combine(ev.start_date, datetime.min.time())).date()
        return cards.fmt_span(None, None, True, new_start, new_start + span_days)
    old_start = ev.start_at.astimezone(tz).replace(tzinfo=None)
    old_end = ev.end_at.astimezone(tz).replace(tzinfo=None)
    new_start = start or old_start
    new_end = end or (new_start + (old_end - old_start))
    return cards.fmt_span(new_start, new_end, False)


def _short_label(ev: Event, tz: ZoneInfo) -> str:
    if ev.all_day:
        when = f"{ev.start_date:%m/%d}"
    else:
        when = f"{ev.start_at.astimezone(tz):%m/%d %H:%M}"
    return f"{when} {ev.title}"[:20]  # quick reply 標籤上限 20 字


def _range_title(first: date, last: date) -> str:
    return cards.fmt_day(first) if first == last else f"{cards.fmt_day(first)}–{cards.fmt_day(last)}"
