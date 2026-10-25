import { api, ApiError, errorMessage } from "../api";
import { mockApi } from "./helpers";

test("mutating requests carry the CSRF cookie value; GET does not", async () => {
  document.cookie = "csrf=abc123";
  const calls = mockApi({ "GET /api/me": { user: {}, teams: [] }, "POST /api/drafts/d1/confirm": { event: {} } });
  await api.me();
  await api.confirmDraft("d1", 3);
  expect(calls[0].headers["X-CSRF-Token"]).toBeUndefined();
  expect(calls[1].headers["X-CSRF-Token"]).toBe("abc123");
  expect(calls[1].body).toEqual({ draft_version: 3 });
});

test("error responses become ApiError with the server code", async () => {
  mockApi({ "POST /api/drafts/d1/confirm": () => ({ status: 409, json: { error: "draft_version_conflict" } }) });
  const err = await api.confirmDraft("d1", 1).catch((e) => e);
  expect(err).toBeInstanceOf(ApiError);
  expect([err.status, err.code]).toEqual([409, "draft_version_conflict"]);
  expect(errorMessage(err)).toContain("重新載入最新版本");
});

test("events query encodes member and team filters", async () => {
  const calls = mockApi({ "GET /api/events": { items: [], has_more: false, next_offset: null } });
  await api.events({ start: "2026-10-19", end: "2026-10-25", memberId: "u1", teamId: "t1", offset: 100 });
  expect(calls[0].path).toBe("/api/events");
  const fetchUrl = String(vi.mocked(fetch).mock.calls[0][0]);
  expect(fetchUrl).toBe("/api/events?start_date=2026-10-19&end_date=2026-10-25&member_id=u1&team_id=t1&offset=100");
});

test("unknown errors get a generic message", () => {
  expect(errorMessage(new TypeError("network"))).toBe("連線發生問題，請稍後再試。");
});
