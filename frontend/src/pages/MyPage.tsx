// 我的行程：今天／本週／本月；可從這裡新增、修改、取消（都會先建草稿，再到確認頁確認）。
import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";

import { useApp } from "../App";
import { api, errorMessage, type EventItem } from "../api";
import { EventList, SCOPE_NOTE } from "../components/EventList";
import { Status } from "../components/Status";
import { addDays, monthRange, todayIn, weekStart } from "../dates";

type Range = "today" | "week" | "month";

function bounds(range: Range, today: string): [string, string] {
  if (range === "today") return [today, today];
  if (range === "week") return [weekStart(today), addDays(weekStart(today), 6)];
  return monthRange(today);
}

export function MyPage() {
  const { session } = useApp();
  const navigate = useNavigate();
  const [range, setRange] = useState<Range>("week");
  const [items, setItems] = useState<EventItem[] | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const today = todayIn(session.user.timezone);
  const [start, end] = bounds(range, today);

  useEffect(() => {
    let cancelled = false;
    setItems(null);
    setError(null);
    api
      .events({ start, end, memberId: session.user.id })
      .then((page) => {
        if (cancelled) return;
        setItems(page.items);
        setHasMore(page.has_more);
      })
      .catch((err) => !cancelled && setError(errorMessage(err)));
    return () => {
      cancelled = true;
    };
  }, [start, end, session.user.id]);

  async function openDraft(intent: "create" | "update" | "cancel", eventId?: string) {
    setBusy(true);
    try {
      const draft = await api.newDraft(intent, eventId);
      navigate(`/drafts/${draft.id}`);
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  }

  async function decline(eventId: string) {
    if (!window.confirm("確定不參加這個行程？")) return;
    setBusy(true);
    try {
      await api.decline(eventId);
      setItems((list) => list?.filter((e) => e.id !== eventId) ?? null);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <header className="page-header">
        <h1>我的行程</h1>
        <button className="primary small" disabled={busy} onClick={() => void openDraft("create")}>＋ 新增</button>
      </header>
      <div className="segmented" role="tablist">
        {(["today", "week", "month"] as Range[]).map((r) => (
          <button key={r} role="tab" aria-selected={range === r} className={range === r ? "active" : ""} onClick={() => setRange(r)}>
            {{ today: "今天", week: "本週", month: "本月" }[r]}
          </button>
        ))}
      </div>
      {error && <p className="alert" role="alert">{error}</p>}
      {items === null && !error && <Status kind="loading" />}
      {items !== null && items.length === 0 && (
        <Status
          kind="empty"
          title="這段期間沒有已登錄的行程"
          message="在 LINE 跟我說一句話就能新增，例如：「明天 10:00 客戶會議」。"
        />
      )}
      {items !== null && items.length > 0 && (
        <EventList
          items={items}
          showOwner={false}
          actions={(e) =>
            e.role === "attendee" ? (
              <button className="link danger" disabled={busy} onClick={() => void decline(e.id)}>不參加</button>
            ) : (
              <>
                <button className="link" disabled={busy} onClick={() => void openDraft("update", e.id)}>修改</button>
                <button className="link danger" disabled={busy} onClick={() => void openDraft("cancel", e.id)}>取消</button>
              </>
            )
          }
        />
      )}
      {hasMore && <p className="muted">行程較多，只顯示前 100 筆，請切換到較短的範圍。</p>}
      <p className="scope-note">{SCOPE_NOTE}</p>
    </div>
  );
}
