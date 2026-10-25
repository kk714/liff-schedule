// 2026-10-26 新功能：每週重複、多天、邀請成員、不參加、取消系列、時間軸共同空檔與詳情。
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { App } from "../App";
import { commonFree } from "../components/DayTimeline";
import { at, CONFIG, fakeLiff, ME, mockApi } from "./helpers";

const MEMBERS = {
  members: [
    { id: "u1", display_name: "Kyle", me: true, teams: [] },
    { id: "u2", display_name: "Carrey", me: false, teams: [] },
    { id: "u3", display_name: "Tiger", me: false, teams: [] },
  ],
};
const BLANK = {
  id: "d1", intent: "create", status: "collecting", draft_version: 1, expires_at: "x",
  fields: { title: null, start_local: null, end_local: null, all_day: false, location: null, notes: null,
            visibility: "team_detail", recurrence: null, attendee_ids: null, scope: null },
  defaults_applied: [], missing_fields: ["title", "start_local"], event: null,
};
const EVENT = { id: "e1", title: "團隊週會", all_day: false, start_local: "2026-10-26T10:00:00",
                end_local: "2026-10-26T11:00:00", start_date: null, end_date: null, location: null, notes: null,
                visibility: "team_detail", status: "active", version: 1, series: true };
const item = (over: object) => ({
  id: "e", owner_user_id: "u1", owner_name: "Kyle", mine: true, projection: "detail", all_day: false,
  start_local: "2026-10-27T11:30:00", end_local: "2026-10-27T14:00:00", start_date: null, end_date: null,
  title: "B216 組織調整及未來規劃說明", location: "宏匯大樓 8樓會議室", notes: null, visibility: "team_detail",
  role: "owner", series: false, attendees: [], ...over,
});

test("commonFree merges everyone's busy time and keeps gaps of 30+ minutes", () => {
  expect(commonFree([[600, 660], [630, 720], [750, 780]], 480, 1200)).toEqual([[480, 600], [720, 750], [780, 1200]]);
  expect(commonFree([[480, 1200]], 480, 1200)).toEqual([]);
  expect(commonFree([[600, 700], [720, 800]], 600, 800)).toEqual([]); // 20 分鐘的空隙不算
});

test("weekly meeting with invited members is sent as recurrence + attendee_ids", async () => {
  at("/drafts/d1");
  const calls = mockApi({
    "GET /api/config": CONFIG, "GET /api/me": ME, "GET /api/members": MEMBERS, "GET /api/drafts/d1": BLANK,
    "PATCH /api/drafts/d1": (body: any) => ({ json: { ...BLANK, draft_version: 2, status: "awaiting_confirmation",
                                                       missing_fields: [], fields: { ...BLANK.fields, ...body.fields } } }),
    "POST /api/drafts/d1/confirm": { intent: "create", already_committed: false, affected: 6, event: EVENT },
  });
  render(<App liffFactory={async () => fakeLiff()} />);
  await userEvent.type(await screen.findByLabelText("行程名稱"), "團隊週會");
  await userEvent.type(screen.getByLabelText("日期"), "2026-10-26");
  await userEvent.type(screen.getByLabelText("開始"), "10:00");
  await userEvent.selectOptions(screen.getByLabelText("重複"), "weekly");
  const until = screen.getByLabelText("重複到");
  await userEvent.clear(until);
  await userEvent.type(until, "2026-11-30");
  await userEvent.click(within(screen.getByRole("group", { name: "邀請成員" })).getByRole("button", { name: "Carrey" }));
  expect(within(screen.getByRole("group", { name: "邀請成員" })).queryByRole("button", { name: "Kyle" })).toBeNull();

  await userEvent.click(screen.getByRole("button", { name: "儲存並確認" }));
  expect(await screen.findByText("已建立（共 6 筆）")).toBeInTheDocument();
  const patch = calls.find((c) => c.method === "PATCH")!.body as any;
  expect(patch.fields.recurrence).toEqual({ freq: "weekly", weekdays: [0], until: "2026-11-30" }); // 預設為開始日的星期
  expect(patch.fields.attendee_ids).toEqual(["u2"]);
});

test("multi-day trip sends the last day as end_local", async () => {
  at("/drafts/d1");
  const calls = mockApi({
    "GET /api/config": CONFIG, "GET /api/me": ME, "GET /api/members": MEMBERS, "GET /api/drafts/d1": BLANK,
    "PATCH /api/drafts/d1": (body: any) => ({ json: { ...BLANK, status: "awaiting_confirmation", missing_fields: [],
                                                       draft_version: 2, fields: { ...BLANK.fields, ...body.fields } } }),
    "POST /api/drafts/d1/confirm": { intent: "create", already_committed: false, affected: 1, event: EVENT },
  });
  render(<App liffFactory={async () => fakeLiff()} />);
  await userEvent.type(await screen.findByLabelText("行程名稱"), "越南出差");
  await userEvent.click(screen.getByLabelText(/整天/));
  await userEvent.type(screen.getByLabelText("開始日期"), "2026-10-26");
  await userEvent.type(screen.getByLabelText(/結束日期/), "2026-10-30");
  await userEvent.click(screen.getByRole("button", { name: "儲存並確認" }));
  await screen.findByText("已建立");
  const patch = calls.find((c) => c.method === "PATCH")!.body as any;
  expect(patch.fields).toMatchObject({ all_day: true, start_local: "2026-10-26T00:00:00", end_local: "2026-10-30T00:00:00" });
});

test("cancelling a series occurrence can cancel this and following", async () => {
  at("/drafts/d1");
  const cancelDraft = { ...BLANK, intent: "cancel", status: "awaiting_confirmation", missing_fields: [], event: EVENT };
  const calls = mockApi({
    "GET /api/config": CONFIG, "GET /api/me": ME, "GET /api/drafts/d1": cancelDraft,
    "PATCH /api/drafts/d1": { ...cancelDraft, draft_version: 2 },
    "POST /api/drafts/d1/confirm": { intent: "cancel", already_committed: false, affected: 4, event: EVENT },
  });
  render(<App liffFactory={async () => fakeLiff()} />);
  await userEvent.click(await screen.findByLabelText("取消這次與之後全部"));
  await userEvent.click(screen.getByRole("button", { name: "確認取消" }));
  expect(await screen.findByText("已取消（共 4 筆）")).toBeInTheDocument();
  expect(calls.find((c) => c.method === "PATCH")!.body).toEqual({ draft_version: 1, fields: { scope: "following" } });
  expect(calls.find((c) => c.path.endsWith("/confirm"))!.body).toEqual({ draft_version: 2 });
});

test("invited event shows organizer and a decline button on my page", async () => {
  at("/");
  vi.spyOn(window, "confirm").mockReturnValue(true);
  const calls = mockApi({
    "GET /api/config": CONFIG, "GET /api/me": ME,
    "GET /api/events": { items: [item({ id: "e5", owner_user_id: "u2", owner_name: "Carrey", mine: false, role: "attendee",
                                        attendees: [{ id: "u1", name: "Kyle" }], start_local: todayAt("10:00"),
                                        end_local: todayAt("11:00") })], has_more: false, next_offset: null },
    "POST /api/events/e5/decline": { status: "declined" },
  });
  render(<App liffFactory={async () => fakeLiff()} />);
  expect(await screen.findByText("由 Carrey 邀請")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "修改" })).toBeNull();
  await userEvent.click(screen.getByRole("button", { name: "不參加" }));
  expect(calls.some((c) => c.path === "/api/events/e5/decline")).toBe(true);
  expect(screen.queryByText("由 Carrey 邀請")).toBeNull();
});

test("timeline puts a shared meeting in every attendee's column and shows details on tap", async () => {
  at("/team");
  mockApi({
    "GET /api/config": CONFIG, "GET /api/me": ME, "GET /api/members": MEMBERS,
    "GET /api/events": { items: [item({ id: "m1", attendees: [{ id: "u3", name: "Tiger" }],
                                        start_local: todayAt("11:30"), end_local: todayAt("14:00") }),
                                  item({ id: "x9", start_local: "2020-01-01T09:00:00", end_local: "2020-01-01T10:00:00",
                                         title: "別天的行程" })],
                         has_more: false, next_offset: null },
  });
  render(<App liffFactory={async () => fakeLiff()} />);
  const blocks = await screen.findAllByRole("button", { name: /B216 組織調整及未來規劃說明/ });
  expect(blocks).toHaveLength(2); // Kyle 與 Tiger 兩欄
  expect(screen.queryByText("別天的行程")).toBeNull(); // 不在這一天的不畫
  expect(screen.getByText("大家都有空的時段")).toBeInTheDocument();

  await userEvent.click(blocks[0]);
  const sheet = screen.getByRole("dialog", { name: "行程詳情" });
  expect(within(sheet).getByText("主辦：Kyle")).toBeInTheDocument();
  expect(within(sheet).getByText("參與：Tiger")).toBeInTheDocument();
  expect(within(sheet).getByText("地點：宏匯大樓 8樓會議室")).toBeInTheDocument();
  await userEvent.click(within(sheet).getByRole("button", { name: "關閉" }));
  expect(screen.queryByRole("dialog")).toBeNull();
});

function todayAt(hhmm: string): string {
  const today = new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Taipei" }).format(new Date());
  return `${today}T${hhmm}:00`;
}
