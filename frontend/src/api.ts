// 後端 API 用戶端。Session 在 HttpOnly Cookie；變更請求以 double-submit 帶 CSRF（設計文件 §4.2）。

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
  ) {
    super(`${status} ${code}`);
  }
}

export type Visibility = "private" | "busy_only" | "team_detail";

export interface User {
  id: string;
  display_name: string;
  timezone: string;
  default_visibility: Visibility;
}

export interface Team {
  id: string;
  name: string;
}

export interface Member {
  id: string;
  display_name: string;
  me: boolean;
  teams: Team[];
}

export interface Recurrence {
  freq: "weekly";
  weekdays: number[]; // 0=週一 … 6=週日
  until: string; // YYYY-MM-DD，含當日
}

export interface DraftFields {
  title: string | null;
  start_local: string | null;
  end_local: string | null;
  all_day: boolean | null;
  location: string | null;
  notes: string | null;
  visibility: Visibility | null;
  recurrence: Recurrence | null;
  attendee_ids: string[] | null;
  scope: "single" | "following" | null;
}

export interface EventDetail {
  id: string;
  title: string;
  all_day: boolean;
  start_local: string | null;
  end_local: string | null;
  start_date: string | null;
  end_date: string | null; // 不含當日
  location: string | null;
  notes: string | null;
  visibility: Visibility;
  status: "active" | "cancelled";
  version: number;
  series: boolean;
}

export interface Draft {
  id: string;
  intent: "create" | "update" | "cancel";
  status: "collecting" | "awaiting_confirmation" | "committed" | "abandoned" | "expired";
  draft_version: number;
  expires_at: string;
  fields: DraftFields;
  defaults_applied: string[];
  missing_fields: string[];
  event: EventDetail | null;
}

export interface EventItem {
  id: string;
  owner_user_id: string;
  owner_name: string;
  mine: boolean;
  projection: "detail" | "busy";
  all_day: boolean;
  start_local: string | null;
  end_local: string | null;
  start_date: string | null;
  end_date: string | null;
  title: string | null;
  location: string | null;
  notes: string | null;
  visibility: Visibility | null;
  role: "owner" | "attendee" | "viewer";
  series: boolean;
  attendees: { id: string; name: string }[];
}

export interface EventPage {
  items: EventItem[];
  has_more: boolean;
  next_offset: number | null;
}

export interface ConfirmResult {
  intent: Draft["intent"];
  already_committed: boolean;
  affected: number;
  event: EventDetail;
}

function csrfToken(): string {
  const match = document.cookie.match(/(?:^|;\s*)csrf=([^;]+)/);
  return match ? decodeURIComponent(match[1]) : "";
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET") headers["X-CSRF-Token"] = csrfToken();
  const resp = await fetch(path, {
    method,
    headers,
    credentials: "same-origin",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!resp.ok) {
    let code = "http_error";
    try {
      const data = await resp.json();
      code = typeof data.error === "string" ? data.error : code;
    } catch {
      /* 非 JSON 錯誤 */
    }
    throw new ApiError(resp.status, code);
  }
  return (await resp.json()) as T;
}

export const api = {
  config: () => request<{ liff_id: string }>("GET", "/api/config"),
  me: () => request<{ user: User; teams: Team[] }>("GET", "/api/me"),
  loginWithLine: (idToken: string) =>
    request<{ bound: boolean; user?: User }>("POST", "/api/auth/line", { id_token: idToken }),
  previewInvitation: (token: string) =>
    request<{ display_name: string; organization: string; expires_at: string }>(
      "POST", "/api/invitations/preview", { token }),
  redeemInvitation: (token: string, idToken: string) =>
    request<{ bound: boolean; user: User }>("POST", "/api/invitations/redeem", { token, id_token: idToken }),
  members: (teamId?: string) =>
    request<{ members: Member[] }>("GET", teamId ? `/api/members?team_id=${teamId}` : "/api/members"),
  draft: (id: string) => request<Draft>("GET", `/api/drafts/${id}`),
  patchDraft: (id: string, version: number, fields: Partial<DraftFields>) =>
    request<Draft>("PATCH", `/api/drafts/${id}`, { draft_version: version, fields }),
  confirmDraft: (id: string, version: number) =>
    request<ConfirmResult>("POST", `/api/drafts/${id}/confirm`, { draft_version: version }),
  abandonDraft: (id: string, version: number) =>
    request<{ status: string }>("POST", `/api/drafts/${id}/abandon`, { draft_version: version }),
  newDraft: (intent: Draft["intent"], eventId?: string, fields: Partial<DraftFields> = {}) =>
    request<Draft>("POST", "/api/events/drafts", { intent, fields, event_id: eventId ?? null }),
  decline: (eventId: string) => request<{ status: string }>("POST", `/api/events/${eventId}/decline`),
  events: (params: { start: string; end: string; memberId?: string; teamId?: string; offset?: number }) => {
    const q = new URLSearchParams({ start_date: params.start, end_date: params.end });
    if (params.memberId) q.append("member_id", params.memberId);
    if (params.teamId) q.set("team_id", params.teamId);
    if (params.offset) q.set("offset", String(params.offset));
    return request<EventPage>("GET", `/api/events?${q}`);
  },
};

// 錯誤代碼 → 使用者看得懂的訊息（後端只回代碼，不回內部訊息）
export function errorMessage(err: unknown): string {
  if (!(err instanceof ApiError)) return "連線發生問題，請稍後再試。";
  switch (err.code) {
    case "draft_version_conflict":
      return "內容已經更新過，已重新載入最新版本，請再確認一次。";
    case "event_version_conflict":
      return "這筆行程在這期間已被修改，請重新操作。";
    case "draft_gone":
      return "這份草稿已過期或已放棄，請重新建立。";
    case "invalid_draft":
      return "資料還不完整，請補齊後再確認。";
    case "not_found":
      return "找不到這筆資料，或您沒有權限查看。";
    case "unauthorized":
      return "登入已過期，請重新開啟。";
    case "member_inactive":
      return "您的帳號目前無法使用，請聯絡管理員。";
    case "invitation_invalid":
      return "這個邀請連結已失效（已使用、已撤銷或已過期），請向管理員索取新的連結。";
    case "already_bound":
      return "這個 LINE 帳號或成員已經綁定過了。若要更換帳號，請聯絡管理員。";
    case "range_too_large":
      return "查詢範圍太大，請縮小到 90 天以內。";
    case "csrf_failed":
      return "安全驗證失敗，請重新開啟頁面。";
    default:
      return "發生錯誤，請稍後再試。";
  }
}
