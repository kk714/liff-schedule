import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { App } from "../App";
import { at, CONFIG, fakeLiff, ME, mockApi } from "./helpers";

const DRAFT = {
  id: "d1", intent: "create", status: "awaiting_confirmation", draft_version: 1,
  expires_at: "2026-10-21T03:00:00+00:00",
  fields: { title: "國網中心拜訪", start_local: "2026-11-01T14:00:00", end_local: "2026-11-01T15:00:00",
            all_day: false, location: null, notes: null, visibility: "busy_only" },
  defaults_applied: ["duration_minutes=60", "visibility=busy_only"], missing_fields: [], event: null,
};
const CONFIRMED = {
  intent: "create", already_committed: false,
  event: { id: "e1", title: "國網中心拜訪", all_day: false, start_local: "2026-11-01T14:00:00",
           end_local: "2026-11-01T15:00:00", start_date: null, end_date: null, location: null, notes: null,
           visibility: "busy_only", status: "active", version: 1 },
};

describe("boot", () => {
  test("existing session goes straight to my schedule", async () => {
    at("/");
    mockApi({ "GET /api/config": CONFIG, "GET /api/me": ME, "GET /api/events": { items: [], has_more: false, next_offset: null } });
    render(<App liffFactory={async () => fakeLiff()} />);
    expect(await screen.findByText("這段期間沒有已登錄的行程")).toBeInTheDocument();
    expect(screen.getByText("僅顯示本系統已登錄且您有權限查看的行程")).toBeInTheDocument();
  });

  test("no session and not logged in to LINE → liff.login", async () => {
    at("/");
    mockApi({ "GET /api/config": CONFIG, "GET /api/me": () => ({ status: 401, json: { error: "unauthorized" } }) });
    const liff = fakeLiff({ isLoggedIn: () => false });
    render(<App liffFactory={async () => liff} />);
    await waitFor(() => expect(liff.login).toHaveBeenCalledWith(window.location.href));
  });

  test("unbound LINE account sees binding instructions", async () => {
    at("/");
    const calls = mockApi({
      "GET /api/config": CONFIG,
      "GET /api/me": () => ({ status: 401, json: { error: "unauthorized" } }),
      "POST /api/auth/line": { bound: false },
    });
    render(<App liffFactory={async () => fakeLiff()} />);
    expect(await screen.findByText("尚未完成成員綁定")).toBeInTheDocument();
    expect(calls.find((c) => c.path === "/api/auth/line")?.body).toEqual({ id_token: "id-token-123" });
  });

  test("missing LIFF_ID shows an error instead of a blank page", async () => {
    at("/");
    mockApi({ "GET /api/config": { liff_id: "" } });
    render(<App liffFactory={async () => fakeLiff()} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("無法啟動");
  });
});

describe("bind page", () => {
  test("shows the member name and binds only after confirmation", async () => {
    at("/bind?invite=tok-1");
    const calls = mockApi({
      "GET /api/config": CONFIG,
      "POST /api/invitations/preview": { display_name: "王小明", organization: "公司", expires_at: "x" },
      "POST /api/invitations/redeem": { bound: true, user: ME.user },
      "GET /api/me": ME,
      "GET /api/events": { items: [], has_more: false, next_offset: null },
    });
    render(<App liffFactory={async () => fakeLiff()} />);
    expect(await screen.findByText("您是「王小明」嗎？")).toBeInTheDocument();
    expect(calls.some((c) => c.path === "/api/invitations/redeem")).toBe(false);

    await userEvent.click(screen.getByRole("button", { name: "這是我，完成綁定" }));
    expect(await screen.findByRole("heading", { name: "我的行程" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "我的行程" })).toHaveClass("active");
    expect(calls.find((c) => c.path === "/api/invitations/redeem")?.body).toEqual({ token: "tok-1", id_token: "id-token-123" });
    expect(window.location.search).toBe(""); // 邀請碼不留在網址
  });

  test("declining does not use the invitation", async () => {
    at("/bind?invite=tok-1");
    const calls = mockApi({
      "GET /api/config": CONFIG,
      "POST /api/invitations/preview": { display_name: "王小明", organization: "公司", expires_at: "x" },
    });
    render(<App liffFactory={async () => fakeLiff()} />);
    await userEvent.click(await screen.findByRole("button", { name: "不是我" }));
    expect(screen.getByText("已取消")).toBeInTheDocument();
    expect(calls.some((c) => c.path === "/api/invitations/redeem")).toBe(false);
  });

  test("used invitation explains what to do", async () => {
    at("/bind?invite=old");
    mockApi({
      "GET /api/config": CONFIG,
      "POST /api/invitations/preview": () => ({ status: 410, json: { error: "invitation_invalid" } }),
    });
    render(<App liffFactory={async () => fakeLiff()} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("向管理員索取新的連結");
  });
});

describe("draft page", () => {
  test("unchanged draft confirms the current version", async () => {
    at("/drafts/d1");
    const calls = mockApi({
      "GET /api/config": CONFIG, "GET /api/me": ME, "GET /api/drafts/d1": DRAFT,
      "POST /api/drafts/d1/confirm": CONFIRMED,
    });
    render(<App liffFactory={async () => fakeLiff()} />);
    expect(await screen.findByDisplayValue("國網中心拜訪")).toBeInTheDocument();
    expect(screen.getByText("未填結束時間時預設 1 小時。")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "確認" }));
    expect(await screen.findByText("已建立")).toBeInTheDocument();
    expect(calls.filter((c) => c.method === "PATCH")).toHaveLength(0);
    expect(calls.find((c) => c.path === "/api/drafts/d1/confirm")?.body).toEqual({ draft_version: 1 });
  });

  test("edited draft is saved first, then confirmed with the new version", async () => {
    at("/drafts/d1");
    const calls = mockApi({
      "GET /api/config": CONFIG, "GET /api/me": ME, "GET /api/drafts/d1": DRAFT,
      "PATCH /api/drafts/d1": (body: any) => ({ json: { ...DRAFT, draft_version: 2, fields: { ...DRAFT.fields, ...body.fields } } }),
      "POST /api/drafts/d1/confirm": CONFIRMED,
    });
    render(<App liffFactory={async () => fakeLiff()} />);
    const location = await screen.findByLabelText("地點");
    await userEvent.type(location, "國網中心");
    await userEvent.click(screen.getByRole("button", { name: "儲存並確認" }));

    await screen.findByText("已建立");
    const patch = calls.find((c) => c.method === "PATCH")!;
    expect(patch.body).toMatchObject({ draft_version: 1, fields: { location: "國網中心", start_local: "2026-11-01T14:00:00" } });
    expect(calls.find((c) => c.path === "/api/drafts/d1/confirm")?.body).toEqual({ draft_version: 2 });
  });

  test("stale version reloads and explains", async () => {
    at("/drafts/d1");
    let loads = 0;
    mockApi({
      "GET /api/config": CONFIG, "GET /api/me": ME,
      "GET /api/drafts/d1": () => ({ json: { ...DRAFT, draft_version: ++loads } }),
      "POST /api/drafts/d1/confirm": () => ({ status: 409, json: { error: "draft_version_conflict" } }),
    });
    render(<App liffFactory={async () => fakeLiff()} />);
    await userEvent.click(await screen.findByRole("button", { name: "確認" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("重新載入最新版本");
    expect(loads).toBe(2);
  });

  test("cancel draft shows the event and a cancel button", async () => {
    at("/drafts/d1");
    mockApi({
      "GET /api/config": CONFIG, "GET /api/me": ME,
      "GET /api/drafts/d1": { ...DRAFT, intent: "cancel", fields: { ...DRAFT.fields, title: null }, event: CONFIRMED.event },
    });
    render(<App liffFactory={async () => fakeLiff()} />);
    expect(await screen.findByText("取消行程")).toBeInTheDocument();
    expect(screen.getByText("11/1（日） 14:00–15:00")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "確認取消" })).toBeInTheDocument();
  });

  test("expired draft shows a clear message", async () => {
    at("/drafts/d1");
    mockApi({ "GET /api/config": CONFIG, "GET /api/me": ME, "GET /api/drafts/d1": { ...DRAFT, status: "expired" } });
    render(<App liffFactory={async () => fakeLiff()} />);
    expect(await screen.findByText("草稿已失效")).toBeInTheDocument();
  });
});

describe("team page", () => {
  const MEMBERS = {
    members: [
      { id: "u1", display_name: "Kyle", me: true, teams: [] },
      { id: "u2", display_name: "李經理", me: false, teams: [] },
      { id: "u3", display_name: "王小明", me: false, teams: [] },
    ],
  };
  const ev = (over: object) => ({
    id: "e", owner_user_id: "u1", owner_name: "Kyle", mine: true, projection: "detail", all_day: false,
    start_local: "2026-10-27T11:30:00", end_local: "2026-10-27T14:00:00", start_date: null, end_date: null,
    title: "B216 組織調整及未來規劃說明", location: "宏匯大樓 8樓會議室", notes: null, visibility: "team_detail", ...over,
  });

  test("day timeline: one column per member by system name, titles only, busy as 忙碌", async () => {
    at("/team");
    mockApi({
      "GET /api/config": CONFIG, "GET /api/me": ME, "GET /api/members": MEMBERS,
      "GET /api/events": {
        items: [
          ev({ id: "e1" }),
          ev({ id: "e2", owner_user_id: "u2", owner_name: "李經理", mine: false, projection: "busy", title: null,
               location: null, start_local: "2026-10-27T15:00:00", end_local: "2026-10-27T16:00:00" }),
        ],
        has_more: false, next_offset: null,
      },
    });
    render(<App liffFactory={async () => fakeLiff()} />);
    expect(await screen.findByText("B216 組織調整及未來規劃說明")).toBeInTheDocument();
    const headers = screen.getAllByRole("columnheader").map((h) => h.textContent);
    expect(headers).toEqual(["Kyle", "李經理", "王小明"]); // 沒有行程的人也有一欄，空檔一目了然
    expect(screen.queryByText("我")).not.toBeInTheDocument();
    expect(screen.getByText("忙碌")).toBeInTheDocument();
    expect(screen.queryByText("宏匯大樓 8樓會議室")).not.toBeInTheDocument(); // 只顯示會議名稱

    const url = String(vi.mocked(fetch).mock.calls.find((c) => String(c[0]).startsWith("/api/events"))![0]);
    const q = new URLSearchParams(url.split("?")[1]);
    expect(q.get("start_date")).toBe(q.get("end_date")); // 一次只查一天
  });

  test("next day button moves the date", async () => {
    at("/team");
    mockApi({
      "GET /api/config": CONFIG, "GET /api/me": ME, "GET /api/members": MEMBERS,
      "GET /api/events": { items: [], has_more: false, next_offset: null },
    });
    render(<App liffFactory={async () => fakeLiff()} />);
    await screen.findByText(/這天還沒有人登錄行程/);
    await userEvent.click(screen.getByRole("button", { name: "後一天" }));
    expect(await screen.findByRole("button", { name: "今天" })).toBeInTheDocument();
  });
});

describe("new page", () => {
  test("creates one blank draft and opens it", async () => {
    at("/new");
    const blank = { ...DRAFT, status: "collecting", missing_fields: ["title", "start_local"],
                    fields: { ...DRAFT.fields, title: null, start_local: null, end_local: null } };
    const calls = mockApi({
      "GET /api/config": CONFIG, "GET /api/me": ME,
      "POST /api/events/drafts": blank, "GET /api/drafts/d1": blank,
    });
    render(<App liffFactory={async () => fakeLiff()} />);
    expect(await screen.findByRole("heading", { name: "新增行程" })).toBeInTheDocument();
    expect(calls.filter((c) => c.path === "/api/events/drafts")).toHaveLength(1);
    expect(window.location.pathname).toBe("/drafts/d1");
  });
});
