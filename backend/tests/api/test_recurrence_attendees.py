"""多天行程、每週重複、邀請成員、不參加、取消系列（LIFF 手動建立，2026-10-26）。"""

from __future__ import annotations

WEEK = {"start_date": "2026-10-26", "end_date": "2026-11-30"}


def draft(client, intent="create", event_id=None, **fields):
    resp = client.post("/api/events/drafts", json={"intent": intent, "fields": fields, "event_id": event_id})
    assert resp.status_code == 200, resp.text
    return resp.json()


def confirm(client, d):
    resp = client.post(f"/api/drafts/{d['id']}/confirm", json={"draft_version": d["draft_version"]})
    assert resp.status_code == 200, resp.text
    return resp.json()


def items(client, **params):
    return client.get("/api/events", params={**WEEK, **params}).json()["items"]


def member_id(client, name):
    return next(m["id"] for m in client.get("/api/members").json()["members"] if m["display_name"] == name)


def test_multi_day_trip_is_one_all_day_event(clients):
    me, *_ = clients
    d = draft(me, title="越南出差", start_local="2026-10-26T00:00:00", end_local="2026-10-30T00:00:00", all_day=True)
    assert d["status"] == "awaiting_confirmation"
    ev = confirm(me, d)["event"]
    assert (ev["start_date"], ev["end_date"]) == ("2026-10-26", "2026-10-31")  # end_date 不含當日
    on_wed = me.get("/api/events", params={"start_date": "2026-10-28", "end_date": "2026-10-28"}).json()["items"]
    assert [i["title"] for i in on_wed] == ["越南出差"]


def test_weekly_meeting_expands_into_series(clients):
    me, mate, *_ = clients
    d = draft(me, title="團隊週會", start_local="2026-10-26T10:00:00", end_local="2026-10-26T11:00:00",
              recurrence={"freq": "weekly", "weekdays": [0], "until": "2026-11-30"})
    result = confirm(me, d)
    assert result["affected"] == 6  # 10/26、11/2、9、16、23、30
    mine = items(me)
    assert [i["start_local"][:10] for i in mine] == [
        "2026-10-26", "2026-11-02", "2026-11-09", "2026-11-16", "2026-11-23", "2026-11-30"]
    assert all(i["series"] and i["start_local"][11:16] == "10:00" for i in mine)
    assert len(items(mate)) == 6  # 預設團隊可見


def test_recurrence_limits_reported_as_missing(clients):
    me, *_ = clients
    too_long = draft(me, title="x", start_local="2026-10-26T10:00:00",
                     recurrence={"weekdays": [0], "until": "2027-06-01"})
    backwards = draft(me, title="x", start_local="2026-10-26T10:00:00",
                      recurrence={"weekdays": [0], "until": "2026-10-01"})
    assert too_long["missing_fields"] == ["recurrence"] and backwards["missing_fields"] == ["recurrence"]


def test_invited_member_gets_the_event(clients):
    me, mate, stranger, _ = clients
    mate_id = member_id(me, "王小明")
    ev = confirm(me, draft(me, title="客戶提案", start_local="2026-10-27T14:00:00", visibility="private",
                           attendee_ids=[mate_id]))["event"]

    [seen] = items(mate, member_id=member_id(mate, "王小明"))  # 受邀者的「我的行程」
    assert (seen["id"], seen["title"], seen["role"], seen["owner_name"]) == (ev["id"], "客戶提案", "attendee", "我")
    assert [a["name"] for a in seen["attendees"]] == ["王小明"]
    assert items(stranger) == []  # 私人行程：受邀者以外看不到
    assert [i["role"] for i in items(me)] == ["owner"]


def test_only_teammates_can_be_invited(clients):
    me, _, stranger, _ = clients
    stranger_id = stranger.get("/api/me").json()["user"]["id"]
    d = draft(me, title="x", start_local="2026-10-27T14:00:00", attendee_ids=[stranger_id])
    assert d["missing_fields"] == ["attendees"]


def test_decline_removes_it_for_the_attendee_only(clients):
    me, mate, *_ = clients
    ev = confirm(me, draft(me, title="讀書會", start_local="2026-10-28T19:00:00",
                           attendee_ids=[member_id(me, "王小明")]))["event"]
    mate_self = member_id(mate, "王小明")
    assert len(items(mate, member_id=mate_self)) == 1
    assert mate.post(f"/api/events/{ev['id']}/decline").status_code == 200
    assert items(mate, member_id=mate_self) == []
    assert [i["attendees"] for i in items(me)] == [[]]
    assert mate.post(f"/api/events/{ev['id']}/decline").status_code == 404


def test_attendee_cannot_edit_or_cancel(clients):
    me, mate, *_ = clients
    ev = confirm(me, draft(me, title="會議", start_local="2026-10-28T10:00:00",
                           attendee_ids=[member_id(me, "王小明")]))["event"]
    resp = mate.post("/api/events/drafts", json={"intent": "cancel", "event_id": ev["id"]})
    assert resp.status_code == 404


def test_update_replaces_attendees(clients):
    me, mate, *_ = clients
    ev = confirm(me, draft(me, title="會議", start_local="2026-10-28T10:00:00",
                           attendee_ids=[member_id(me, "王小明")]))["event"]
    confirm(me, draft(me, intent="update", event_id=ev["id"], attendee_ids=[]))
    assert items(mate, member_id=member_id(mate, "王小明")) == []


def test_cancel_this_and_following_in_series(clients):
    me, *_ = clients
    confirm(me, draft(me, title="週會", start_local="2026-10-26T10:00:00",
                      recurrence={"weekdays": [0], "until": "2026-11-16"}))
    series = items(me)
    assert len(series) == 4
    result = confirm(me, draft(me, intent="cancel", event_id=series[1]["id"], scope="following"))
    assert result["affected"] == 3
    assert [i["start_local"][:10] for i in items(me)] == ["2026-10-26"]


def test_cancel_single_occurrence_keeps_the_rest(clients):
    me, *_ = clients
    confirm(me, draft(me, title="週會", start_local="2026-10-26T10:00:00",
                      recurrence={"weekdays": [0, 2], "until": "2026-11-04"}))
    series = items(me)
    assert len(series) == 4  # 一、三、一、三
    confirm(me, draft(me, intent="cancel", event_id=series[0]["id"]))
    assert len(items(me)) == 3


def test_patch_with_explicit_null_clears_recurrence(clients):
    me, *_ = clients
    d = draft(me, title="週會", start_local="2026-10-26T10:00:00", recurrence={"weekdays": [0], "until": "2026-11-16"})
    patched = me.patch(f"/api/drafts/{d['id']}", json={"draft_version": 1, "fields": {"recurrence": None}}).json()
    assert patched["fields"]["recurrence"] is None
    assert confirm(me, patched)["affected"] == 1
