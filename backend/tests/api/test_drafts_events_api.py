"""草稿與查詢 API（§9、A06、A07、A08、A09）。"""

from __future__ import annotations


def make_draft(client, **fields):
    body = {"intent": "create", "fields": {"title": "國網中心拜訪", "start_local": "2026-11-01T14:00:00", **fields}}
    resp = client.post("/api/events/drafts", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def confirm(client, draft, version=None):
    return client.post(f"/api/drafts/{draft['id']}/confirm", json={"draft_version": version or draft["draft_version"]})


def test_form_draft_then_confirm(clients):
    me, *_ = clients
    draft = make_draft(me)
    assert draft["status"] == "awaiting_confirmation"
    assert draft["fields"]["end_local"] == "2026-11-01T15:00:00"
    assert draft["defaults_applied"] == ["duration_minutes=60", "visibility=busy_only"]

    resp = confirm(me, draft)
    assert resp.status_code == 200
    ev = resp.json()["event"]
    assert (ev["title"], ev["start_local"], ev["end_local"], ev["version"]) == (
        "國網中心拜訪", "2026-11-01T14:00:00", "2026-11-01T15:00:00", 1)

    again = confirm(me, draft)  # A06：重送同一版本
    assert again.json()["already_committed"] is True and again.json()["event"]["id"] == ev["id"]


def test_patch_bumps_version_and_old_version_rejected(clients):
    me, *_ = clients
    draft = make_draft(me)
    patched = me.patch(f"/api/drafts/{draft['id']}",
                       json={"draft_version": 1, "fields": {"start_local": "2026-11-01T16:00:00",
                                                            "end_local": "2026-11-01T17:30:00",
                                                            "visibility": "team_detail"}})
    assert patched.status_code == 200
    assert patched.json()["draft_version"] == 2 and patched.json()["fields"]["visibility"] == "team_detail"
    stale = confirm(me, draft, version=1)
    assert stale.status_code == 409 and stale.json() == {"error": "draft_version_conflict"}  # A07
    assert confirm(me, draft, version=2).json()["event"]["end_local"] == "2026-11-01T17:30:00"


def test_missing_fields_reported_and_confirm_blocked(clients):
    me, *_ = clients
    draft = make_draft(me, start_local=None)
    assert draft["status"] == "collecting" and draft["missing_fields"] == ["start_local"]
    assert confirm(me, draft).status_code == 422


def test_abandon(clients):
    me, *_ = clients
    draft = make_draft(me)
    assert me.post(f"/api/drafts/{draft['id']}/abandon", json={"draft_version": 1}).status_code == 200
    assert confirm(me, draft).status_code == 410


def test_cancel_via_form_shows_event(clients):
    me, *_ = clients
    ev = confirm(me, make_draft(me)).json()["event"]
    draft = me.post("/api/events/drafts", json={"intent": "cancel", "event_id": ev["id"]}).json()
    assert draft["event"]["title"] == "國網中心拜訪"
    assert confirm(me, draft).json()["event"]["status"] == "cancelled"


def test_cannot_touch_someone_elses_draft_or_event(clients):
    me, mate, *_ = clients
    draft = make_draft(mate)
    assert me.get(f"/api/drafts/{draft['id']}").status_code == 404
    assert confirm(me, draft).status_code == 404
    ev = confirm(mate, draft).json()["event"]
    resp = me.post("/api/events/drafts", json={"intent": "cancel", "event_id": ev["id"]})
    assert resp.status_code == 404


def test_writes_require_csrf_and_session(clients):
    me, _, _, anon = clients
    no_csrf = me.post("/api/events/drafts", json={"intent": "create", "fields": {}},
                      headers={"x-csrf-token": "nope"})
    assert no_csrf.status_code == 403
    assert anon.get("/api/events", params={"start_date": "2026-10-19", "end_date": "2026-10-25"}).status_code == 401


def test_events_visibility_through_api(clients):
    me, mate, stranger, _ = clients
    confirm(mate, make_draft(mate, title="同事拜訪", visibility="busy_only"))
    confirm(mate, make_draft(mate, title="團隊週會", start_local="2026-11-02T10:00:00", visibility="team_detail"))
    confirm(mate, make_draft(mate, title="私事", visibility="private"))
    confirm(me, make_draft(me, title="我的私事", visibility="private"))

    params = {"start_date": "2026-10-26", "end_date": "2026-11-08"}
    items = me.get("/api/events", params=params).json()["items"]
    seen = sorted(((i["owner_name"], i["title"], i["projection"], i["mine"]) for i in items),
                  key=lambda t: (t[0], t[1] or ""))
    assert seen == [("我", "我的私事", "detail", True), ("王小明", None, "busy", False),
                    ("王小明", "團隊週會", "detail", False)]
    busy = next(i for i in items if i["projection"] == "busy")
    assert busy["location"] is None and busy["visibility"] is None and busy["start_local"] == "2026-11-01T14:00:00"

    assert stranger.get("/api/events", params=params).json()["items"] == []  # A08：無共同團隊
    hidden = next(i for i in items if i["owner_name"] == "王小明" and i["projection"] == "busy")
    assert stranger.get(f"/api/events/{hidden['id']}").status_code == 404  # A09


def test_events_filters_and_range_limit(clients):
    me, mate, *_ = clients
    confirm(me, make_draft(me, title="我的會", visibility="team_detail"))
    confirm(mate, make_draft(mate, title="同事的會", visibility="team_detail"))
    params = {"start_date": "2026-11-01", "end_date": "2026-11-01"}
    my_id = me.get("/api/me").json()["user"]["id"]
    mine = me.get("/api/events", params={**params, "member_id": my_id}).json()["items"]
    assert [i["title"] for i in mine] == ["我的會"]
    assert me.get("/api/events", params={**params, "keyword": "同事"}).json()["items"][0]["title"] == "同事的會"
    too_big = me.get("/api/events", params={"start_date": "2026-01-01", "end_date": "2026-12-31"})
    assert too_big.status_code == 422


def test_members_lists_only_teammates(clients):
    me, *_ = clients
    members = me.get("/api/members").json()["members"]
    assert [(m["display_name"], m["me"]) for m in members] == [("我", True), ("王小明", False)]
    assert members[1]["teams"][0]["name"] == "業務部"


def test_config_exposes_liff_id_only(clients):
    *_, anon = clients
    assert anon.get("/api/config").json() == {"liff_id": "1650000000-test"}
