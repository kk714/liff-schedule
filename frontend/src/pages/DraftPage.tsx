// 草稿確認／修改（從 LINE 確認卡的「修改」或清單的修改／取消進入）。
// 確認只提交 draft_id＋draft_version；表單有改動時先 PATCH 取得新版本再確認（§5.3、§9.2）。
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { useApp } from "../App";
import { api, ApiError, errorMessage, type ConfirmResult, type Draft, type DraftFields, type Visibility } from "../api";
import { Status } from "../components/Status";
import { dateOf, spanText, timeOf } from "../dates";

interface Form {
  title: string;
  date: string;
  allDay: boolean;
  start: string;
  end: string;
  location: string;
  notes: string;
  visibility: Visibility;
}

const MISSING_TEXT: Record<string, string> = {
  start_local: "請填寫日期與開始時間。",
  title: "請填寫行程名稱。",
  end_local: "結束時間必須晚於開始時間。",
  year_unspecified_past: "這個日期今年已經過了，請確認年份。",
  this_week_day_passed: "這週的那一天已經過了，請確認日期。",
  changes: "請修改至少一個欄位。",
};

function formFrom(draft: Draft, defaultVisibility: Visibility): Form {
  const ev = draft.event;
  const f = draft.fields;
  const start = f.start_local ?? ev?.start_local ?? (ev?.all_day && ev.start_date ? `${ev.start_date}T00:00:00` : null);
  const end = f.end_local ?? (f.start_local ? null : ev?.end_local ?? null);
  return {
    title: f.title ?? ev?.title ?? "",
    date: dateOf(start),
    allDay: f.all_day ?? ev?.all_day ?? false,
    start: f.all_day ? "" : timeOf(start),
    end: timeOf(end),
    location: f.location ?? ev?.location ?? "",
    notes: f.notes ?? ev?.notes ?? "",
    visibility: f.visibility ?? ev?.visibility ?? defaultVisibility,
  };
}

function fieldsFrom(form: Form): Partial<DraftFields> {
  const startLocal = form.date ? (form.allDay ? `${form.date}T00:00:00` : form.start ? `${form.date}T${form.start}:00` : null) : null;
  return {
    title: form.title.trim() || null,
    start_local: startLocal,
    end_local: !form.allDay && form.date && form.end ? `${form.date}T${form.end}:00` : null,
    all_day: form.allDay,
    location: form.location.trim() || null,
    notes: form.notes.trim() || null,
    visibility: form.visibility,
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

export function DraftPage() {
  const { id = "" } = useParams();
  const { session, liff } = useApp();
  const [draft, setDraft] = useState<Draft | null>(null);
  const [form, setForm] = useState<Form | null>(null);
  const [initial, setInitial] = useState<Form | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<ConfirmResult | null>(null);

  const load = useCallback(async () => {
    try {
      const d = await api.draft(id);
      const f = formFrom(d, session.user.default_visibility);
      setDraft(d);
      setForm(f);
      setInitial(f);
      setError(null);
    } catch (err) {
      setError(errorMessage(err));
    }
  }, [id, session.user.default_visibility]);

  useEffect(() => {
    void load();
  }, [load]);

  const dirty = useMemo(() => JSON.stringify(form) !== JSON.stringify(initial), [form, initial]);

  if (error && !draft) return <Status kind="error" message={error} action={<Link to="/">回我的行程</Link>} />;
  if (!draft || !form) return <Status kind="loading" />;

  if (result || draft.status === "committed") {
    const verb = { create: "已建立", update: "已更新", cancel: "已取消" }[result?.intent ?? draft.intent];
    return (
      <Status
        kind="empty"
        title={verb}
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

  async function confirm() {
    if (!draft || !form) return;
    setBusy(true);
    setNotice(null);
    setError(null);
    try {
      let current = draft;
      if (dirty && draft.intent !== "cancel") {
        const next = fieldsFrom(form);
        const base = draft.event ? fieldsFrom(formFrom({ ...draft, fields: emptyFields() }, form.visibility)) : {};
        current = await api.patchDraft(draft.id, draft.draft_version, draft.intent === "update" ? changedFields(next, base) : next);
        const f = formFrom(current, session.user.default_visibility);
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
  const defaults = draft.defaults_applied;

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
          <p className="muted">取消後，團隊成員將看不到這筆行程。</p>
        </div>
      ) : (
        <form className="fields" onSubmit={(e) => { e.preventDefault(); void confirm(); }}>
          <label>
            行程名稱
            <input value={form.title} maxLength={100} onChange={(e) => set("title", e.target.value)} required />
          </label>
          <label>
            日期
            <input type="date" value={form.date} onChange={(e) => set("date", e.target.value)} required />
          </label>
          {draft.intent === "create" && (
            <label className="inline">
              <input type="checkbox" checked={form.allDay} onChange={(e) => set("allDay", e.target.checked)} />
              整天
            </label>
          )}
          {!form.allDay && (
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
          {defaults.includes("duration_minutes=60") && <p className="hint">未填結束時間時預設 1 小時。</p>}
          <label>
            地點
            <input value={form.location} maxLength={200} onChange={(e) => set("location", e.target.value)} />
          </label>
          <label>
            備註
            <textarea value={form.notes} maxLength={1000} rows={3} onChange={(e) => set("notes", e.target.value)} />
          </label>
          <label>
            分享範圍
            <select value={form.visibility} onChange={(e) => set("visibility", e.target.value as Visibility)}>
              <option value="private">私人（只有自己看得到）</option>
              <option value="busy_only">僅顯示忙碌（團隊看得到時段）</option>
              <option value="team_detail">團隊可見詳細（標題、地點、備註）</option>
            </select>
          </label>
          <p className="hint">分享對象為目前的團隊成員，包含日後加入的人。</p>
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

function emptyFields(): DraftFields {
  return { title: null, start_local: null, end_local: null, all_day: null, location: null, notes: null, visibility: null };
}
