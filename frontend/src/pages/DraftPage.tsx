// 草稿確認／修改（從 LINE 確認卡的「修改」、圖文選單「新增行程」或清單的修改／取消進入）。
// 確認只提交 draft_id＋draft_version；表單有改動時先 PATCH 取得新版本再確認（§5.3、§9.2）。
// 手動新增另外支援：多天（整天＋結束日期）、每週重複、邀請成員（2026-10-26）。
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { useApp } from "../App";
import {
  api,
  ApiError,
  errorMessage,
  type ConfirmResult,
  type Draft,
  type DraftFields,
  type Member,
} from "../api";
import { Status } from "../components/Status";
import { addDays, dateOf, parseDate, spanText, timeOf } from "../dates";

const WEEKDAY_LABELS = ["一", "二", "三", "四", "五", "六", "日"]; // 0=週一

interface Form {
  title: string;
  date: string;
  allDay: boolean;
  endDate: string; // 整天時的最後一天（含）；空白表示只有一天
  start: string;
  end: string;
  location: string;
  notes: string;
  repeat: "none" | "weekly";
  weekdays: number[];
  until: string;
  attendees: string[];
}

const MISSING_TEXT: Record<string, string> = {
  start_local: "請填寫日期與開始時間。",
  title: "請填寫行程名稱。",
  end_local: "結束時間必須晚於開始時間。",
  year_unspecified_past: "這個日期今年已經過了，請確認年份。",
  this_week_day_passed: "這週的那一天已經過了，請確認日期。",
  changes: "請修改至少一個欄位。",
  recurrence: "重複設定不正確：結束日期要在開始日之後，且最多約 6 個月、60 次。",
  attendees: "只能邀請同團隊、帳號啟用中的成員。",
};

/** 0=週一 … 6=週日 */
function mondayIndex(iso: string): number {
  const { y, m, d } = parseDate(iso);
  return (new Date(Date.UTC(y, m - 1, d)).getUTCDay() + 6) % 7;
}

function formFrom(draft: Draft): Form {
  const ev = draft.event;
  const f = draft.fields;
  const start = f.start_local ?? ev?.start_local ?? (ev?.all_day && ev.start_date ? `${ev.start_date}T00:00:00` : null);
  const end = f.end_local ?? (f.start_local ? null : ev?.end_local ?? null);
  const allDay = f.all_day ?? ev?.all_day ?? false;
  const date = dateOf(start);
  const lastDay = allDay && end && dateOf(end) > date ? dateOf(end) : "";
  return {
    title: f.title ?? ev?.title ?? "",
    date,
    allDay,
    endDate: lastDay,
    start: allDay ? "" : timeOf(start),
    end: allDay ? "" : timeOf(end),
    location: f.location ?? ev?.location ?? "",
    notes: f.notes ?? ev?.notes ?? "",
    repeat: f.recurrence ? "weekly" : "none",
    weekdays: f.recurrence?.weekdays ?? (date ? [mondayIndex(date)] : [0]),
    until: f.recurrence?.until ?? (date ? addDays(date, 7 * 8) : ""),
    attendees: f.attendee_ids ?? [],
  };
}

function fieldsFrom(form: Form, intent: Draft["intent"]): Partial<DraftFields> {
  const startLocal = form.date
    ? form.allDay ? `${form.date}T00:00:00` : form.start ? `${form.date}T${form.start}:00` : null
    : null;
  const endLocal = !form.date
    ? null
    : form.allDay ? (form.endDate && form.endDate > form.date ? `${form.endDate}T00:00:00` : null)
    : form.end ? `${form.date}T${form.end}:00` : null;
  const base: Partial<DraftFields> = {
    title: form.title.trim() || null,
    start_local: startLocal,
    end_local: endLocal,
    all_day: form.allDay,
    location: form.location.trim() || null,
    notes: form.notes.trim() || null,
    // 分享範圍不由表單決定：新增時沿用成員預設（團隊可見），修改時維持原設定
  };
  if (intent !== "create") return base;
  return {
    ...base,
    recurrence: form.repeat === "weekly" ? { freq: "weekly", weekdays: [...form.weekdays].sort(), until: form.until } : null,
    attendee_ids: form.attendees,
  };
}

/** update 草稿只送出和原行程不同的欄位。 */
function changedFields(next: Partial<DraftFields>, base: Partial<DraftFields>): Partial<DraftFields> {
  const out: Partial<DraftFields> = {};
  for (const key of Object.keys(next) as (keyof DraftFields)[]) {
    if (key === "all_day") continue; // 修改時不切換全天／非全天
    if (next[key] !== base[key] && next[key] !== null) (out as Record<string, unknown>)[key] = next[key];
  }
  return out;
}

function emptyFields(): DraftFields {
  return { title: null, start_local: null, end_local: null, all_day: null, location: null, notes: null,
           visibility: null, recurrence: null, attendee_ids: null, scope: null };
}

export function DraftPage() {
  const { id = "" } = useParams();
  const { liff } = useApp();
  const [draft, setDraft] = useState<Draft | null>(null);
  const [form, setForm] = useState<Form | null>(null);
  const [initial, setInitial] = useState<Form | null>(null);
  const [teammates, setTeammates] = useState<Member[]>([]);
  const [scope, setScope] = useState<"single" | "following">("single");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<ConfirmResult | null>(null);

  const load = useCallback(async () => {
    try {
      const d = await api.draft(id);
      const f = formFrom(d);
      setDraft(d);
      setForm(f);
      setInitial(f);
      setError(null);
      if (d.intent === "create") {
        api.members().then((m) => setTeammates(m.members.filter((x) => !x.me))).catch(() => setTeammates([]));
      }
    } catch (err) {
      setError(errorMessage(err));
    }
  }, [id]);

  useEffect(() => {
    void load();
  }, [load]);

  const dirty = useMemo(() => JSON.stringify(form) !== JSON.stringify(initial), [form, initial]);

  if (error && !draft) return <Status kind="error" message={error} action={<Link to="/">回我的行程</Link>} />;
  if (!draft || !form) return <Status kind="loading" />;

  if (result || draft.status === "committed") {
    const verb = { create: "已建立", update: "已更新", cancel: "已取消" }[result?.intent ?? draft.intent];
    const count = result && result.affected > 1 ? `（共 ${result.affected} 筆）` : "";
    return (
      <Status
        kind="empty"
        title={`${verb}${count}`}
        message={result ? `${result.event.title}｜${spanText(result.event)}` : "這份草稿已經確認過了。"}
        action={
          <div className="actions">
            {liff.isInClient() && <button className="primary" onClick={() => liff.closeWindow()}>回到 LINE</button>}
            <Link to="/">查看我的行程</Link>
          </div>
        }
      />
    );
  }
  if (draft.status === "abandoned" || draft.status === "expired") {
    return <Status kind="empty" title="草稿已失效" message="這份草稿已過期或已放棄，請重新建立。" action={<Link to="/">回我的行程</Link>} />;
  }

  const set = <K extends keyof Form>(key: K, value: Form[K]) => setForm({ ...form, [key]: value });
  const toggle = (list: number[] | string[], value: number | string) =>
    (list as (number | string)[]).includes(value) ? list.filter((x) => x !== value) : [...list, value];

  async function confirm() {
    if (!draft || !form) return;
    setBusy(true);
    setNotice(null);
    setError(null);
    try {
      let current = draft;
      const isSeriesCancel = draft.intent === "cancel" && draft.event?.series && scope === "following";
      if ((dirty && draft.intent !== "cancel") || isSeriesCancel) {
        const next = draft.intent === "cancel" ? { scope } : fieldsFrom(form, draft.intent);
        const base = draft.event ? fieldsFrom(formFrom({ ...draft, fields: emptyFields() }), draft.intent) : {};
        current = await api.patchDraft(draft.id, draft.draft_version,
                                       draft.intent === "update" ? changedFields(next, base) : next);
        const f = formFrom(current);
        setDraft(current);
        setForm(f);
        setInitial(f);
      }
      if (current.status !== "awaiting_confirmation") {
        setNotice(current.missing_fields.map((m) => MISSING_TEXT[m] ?? "資料還不完整。").join(" "));
        return;
      }
      setResult(await api.confirmDraft(current.id, current.draft_version));
    } catch (err) {
      if (err instanceof ApiError && err.code === "draft_version_conflict") await load(); // 另一端改過：載入最新版
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  async function abandon() {
    if (!draft) return;
    setBusy(true);
    try {
      await api.abandonDraft(draft.id, draft.draft_version);
      await load();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  const heading = { create: "新增行程", update: "修改行程", cancel: "取消行程" }[draft.intent];
  const isCreate = draft.intent === "create";

  return (
    <div className="card draft">
      <h1>{heading}</h1>
      {error && <p className="alert" role="alert">{error}</p>}
      {notice && <p className="notice" role="status">{notice}</p>}

      {draft.intent === "cancel" && draft.event ? (
        <div className="summary">
          <p className="summary-title">{draft.event.title}</p>
          <p>{spanText(draft.event)}</p>
          {draft.event.location && <p className="muted">地點：{draft.event.location}</p>}
          {draft.event.series && (
            <fieldset className="choice">
              <legend>這是每週重複的行程</legend>
              <label className="inline">
                <input type="radio" name="scope" checked={scope === "single"} onChange={() => setScope("single")} />
                只取消這一次
              </label>
              <label className="inline">
                <input type="radio" name="scope" checked={scope === "following"} onChange={() => setScope("following")} />
                取消這次與之後全部
              </label>
            </fieldset>
          )}
          <p className="muted">取消後，團隊成員將看不到這筆行程。</p>
        </div>
      ) : (
        <form className="fields" onSubmit={(e) => { e.preventDefault(); void confirm(); }}>
          <label>
            行程名稱
            <input value={form.title} maxLength={100} onChange={(e) => set("title", e.target.value)} required />
          </label>
          <label>
            {form.allDay ? "開始日期" : "日期"}
            <input type="date" value={form.date} onChange={(e) => set("date", e.target.value)} required />
          </label>
          {isCreate && (
            <label className="inline">
              <input type="checkbox" checked={form.allDay} onChange={(e) => set("allDay", e.target.checked)} />
              整天（出差、休假可以跨多天）
            </label>
          )}
          {form.allDay ? (
            isCreate && (
              <label>
                結束日期（選填，跨多天時填最後一天）
                <input type="date" value={form.endDate} min={form.date} onChange={(e) => set("endDate", e.target.value)} />
              </label>
            )
          ) : (
            <div className="row">
              <label>
                開始
                <input type="time" value={form.start} onChange={(e) => set("start", e.target.value)} required />
              </label>
              <label>
                結束
                <input type="time" value={form.end} onChange={(e) => set("end", e.target.value)} />
              </label>
            </div>
          )}
          {!form.allDay && draft.defaults_applied.includes("duration_minutes=60") && (
            <p className="hint">未填結束時間時預設 1 小時。</p>
          )}

          {isCreate && (
            <>
              <label>
                重複
                <select value={form.repeat} onChange={(e) => set("repeat", e.target.value as Form["repeat"])}>
                  <option value="none">不重複</option>
                  <option value="weekly">每週</option>
                </select>
              </label>
              {form.repeat === "weekly" && (
                <div className="repeat">
                  <div className="chips" role="group" aria-label="重複的星期">
                    {WEEKDAY_LABELS.map((label, i) => (
                      <button type="button" key={label} aria-pressed={form.weekdays.includes(i)}
                              className={form.weekdays.includes(i) ? "chip on" : "chip"}
                              onClick={() => set("weekdays", toggle(form.weekdays, i) as number[])}>
                        {label}
                      </button>
                    ))}
                  </div>
                  <label>
                    重複到
                    <input type="date" value={form.until} min={form.date} onChange={(e) => set("until", e.target.value)} />
                  </label>
                  <p className="hint">最多約 6 個月、60 次；確認後會一次建立所有日期。</p>
                </div>
              )}
              {teammates.length > 0 && (
                <div>
                  <p className="field-label">邀請成員</p>
                  <div className="chips" role="group" aria-label="邀請成員">
                    {teammates.map((m) => (
                      <button type="button" key={m.id} aria-pressed={form.attendees.includes(m.id)}
                              className={form.attendees.includes(m.id) ? "chip on" : "chip"}
                              onClick={() => set("attendees", toggle(form.attendees, m.id) as string[])}>
                        {m.display_name}
                      </button>
                    ))}
                  </div>
                  <p className="hint">受邀的人「我的行程」也會出現這筆；只有您能修改或取消。</p>
                </div>
              )}
            </>
          )}

          <label>
            地點
            <input value={form.location} maxLength={200} onChange={(e) => set("location", e.target.value)} />
          </label>
          <label>
            備註
            <textarea value={form.notes} maxLength={1000} rows={3} onChange={(e) => set("notes", e.target.value)} />
          </label>
          <p className="hint">行程會讓團隊成員看到（標題、時間、地點），方便大家安排會議。</p>
        </form>
      )}

      <div className="actions sticky">
        <button className="primary" disabled={busy} onClick={() => void confirm()}>
          {busy ? "處理中…" : draft.intent === "cancel" ? "確認取消" : dirty ? "儲存並確認" : "確認"}
        </button>
        <button disabled={busy} onClick={() => void abandon()}>放棄</button>
      </div>
    </div>
  );
}
