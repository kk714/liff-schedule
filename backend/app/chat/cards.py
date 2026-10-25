"""LINE 回覆內容：確認卡（Flex）、追問與結果文字（設計文件 §5.1、§11）。查詢結果使用固定模板（§3.4-8）。"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

WEEKDAYS = "一二三四五六日"
VISIBILITY_LABEL = {"private": "私人", "busy_only": "僅顯示忙碌", "team_detail": "團隊可見詳細"}
SCOPE_NOTE = "僅顯示本系統已登錄且您有權限查看的行程"


def text(message: str, quick_reply: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    msg: dict[str, Any] = {"type": "text", "text": message[:5000]}
    if quick_reply:
        msg["quickReply"] = {"items": quick_reply[:13]}
    return msg


def fmt_day(d: date) -> str:
    return f"{d:%Y/%m/%d}（{WEEKDAYS[d.weekday()]}）"


def fmt_span(start: datetime | None, end: datetime | None, all_day: bool,
             start_date: date | None = None, end_date: date | None = None) -> str:
    """當地時間的顯示字串。全天 end_date 為不含當日。"""
    if all_day:
        first = start_date or (start.date() if start else None)
        last = (end_date - timedelta(days=1)) if end_date else first
        if first is None:
            return "日期未定"
        return f"{fmt_day(first)} 整天" if last in (None, first) else f"{fmt_day(first)}–{fmt_day(last)} 整天"
    if start is None:
        return "時間未定"
    if end is None:
        return f"{fmt_day(start.date())} {start:%H:%M}"
    if end.date() == start.date():
        return f"{fmt_day(start.date())} {start:%H:%M}–{end:%H:%M}"
    return f"{fmt_day(start.date())} {start:%H:%M}–{fmt_day(end.date())} {end:%H:%M}"


def _postback(label: str, data: str, *, style: str = "secondary", display: str | None = None) -> dict[str, Any]:
    action: dict[str, Any] = {"type": "postback", "label": label, "data": data}
    if display:
        action["displayText"] = display
    return {"type": "button", "style": style, "height": "sm", "action": action}


def confirm_card(
    *, draft_id: str, draft_version: int, intent: str, title: str, span: str,
    location: str | None, visibility: str | None, notes: list[str], liff_url: str = "",
) -> dict[str, Any]:
    heading = {"create": "請確認新增行程", "update": "請確認修改行程", "cancel": "請確認取消行程"}[intent]
    body: list[dict[str, Any]] = [
        {"type": "text", "text": heading, "size": "sm", "color": "#888888"},
        {"type": "text", "text": title or "（未命名）", "size": "lg", "weight": "bold", "wrap": True},
        {"type": "text", "text": span, "wrap": True},
    ]
    if location:
        body.append({"type": "text", "text": f"地點：{location}", "wrap": True, "size": "sm"})
    if visibility and visibility != "team_detail":  # 只有非預設（例如私人）才需要提醒
        body.append({"type": "text", "text": f"分享：{VISIBILITY_LABEL.get(visibility, visibility)}", "size": "sm"})
    for note in notes:
        body.append({"type": "text", "text": note, "size": "xs", "color": "#B05A00", "wrap": True})

    data = f"draft={draft_id}&v={draft_version}"
    confirm_label = "確認取消" if intent == "cancel" else "確認"
    footer = [
        _postback(confirm_label, f"action=confirm&{data}", style="primary", display=confirm_label),
        _postback("放棄", f"action=abandon&{data}", display="放棄"),
    ]
    if liff_url and intent != "cancel":
        footer.append({"type": "button", "style": "link", "height": "sm",
                       "action": {"type": "uri", "label": "修改", "uri": f"{liff_url.rstrip('/')}/drafts/{draft_id}"}})
    return {
        "type": "flex",
        "altText": f"{heading}：{title}"[:400],
        "contents": {
            "type": "bubble",
            "body": {"type": "box", "layout": "vertical", "spacing": "sm", "contents": body},
            "footer": {"type": "box", "layout": "vertical", "spacing": "sm", "contents": footer},
        },
    }


def with_link(message: str, liff_url: str) -> str:
    return f"{message}\n{liff_url}" if liff_url else message


QUESTIONS = {
    "start_local": "請問幾點開始？（例如 14:00）",
    "title": "請問行程名稱是？",
    "end_local": "結束時間早於開始時間，請重新告訴我時間。",
    "year_unspecified_past": "這個日期今年已經過了，請問是哪一年？（例如 2027/1/5）",
    "this_week_day_passed": "這週的那一天已經過了，是指下週嗎？請告訴我日期。",
    "changes": "請問要改成什麼？（例如：改到下午 3 點）",
}
TIMEZONE_QUESTION = "時間的時區不明確，請再說一次時間。"

UNBOUND = "您尚未完成成員綁定，請先開啟以下連結完成綁定："
INACTIVE = "您的帳號目前無法使用，請聯絡管理員。"
NON_TEXT = "目前只支援文字輸入，例如：明天 10:00 客戶會議"
UNSUPPORTED = "這個需求目前不支援。可以用一句話新增、修改、取消或查詢自己的行程，一次一筆。"
CLARIFY = "我不太確定您想做什麼，可以說得更明確嗎？例如：明天 10:00 客戶會議"
NOT_FOUND = "找不到符合的行程，可以補充日期或標題嗎？"
NO_EVENTS = "本系統沒有已登錄行程。"  # 不保證有空（A14）


def question_for(missing: list[str]) -> str:
    for key in missing:
        if key in QUESTIONS:
            return QUESTIONS[key]
        if key.endswith("timezone_offset_mismatch"):
            return TIMEZONE_QUESTION
    return CLARIFY
