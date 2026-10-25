// 團隊週曆：手機以清單為主（§11）。權限由後端套用，這裡只負責顯示「忙碌」或詳情。
import { useEffect, useState } from "react";

import { useApp } from "../App";
import { api, errorMessage, type EventItem } from "../api";
import { EventList, SCOPE_NOTE } from "../components/EventList";
import { Status } from "../components/Status";
import { addDays, dayLabel, todayIn, weekStart } from "../dates";

export function TeamPage() {
  const { session } = useApp();
  const [week, setWeek] = useState(() => weekStart(todayIn(session.user.timezone)));
  const [teamId, setTeamId] = useState<string>("");
  const [items, setItems] = useState<EventItem[] | null>(null);
  const [offset, setOffset] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const end = addDays(week, 6);

  useEffect(() => {
    let cancelled = false;
    setItems(null);
    setError(null);
    api
      .events({ start: week, end, teamId: teamId || undefined })
      .then((page) => {
        if (cancelled) return;
        setItems(page.items);
        setOffset(page.next_offset);
      })
      .catch((err) => !cancelled && setError(errorMessage(err)));
    return () => {
      cancelled = true;
    };
  }, [week, end, teamId]);

  async function loadMore() {
    if (offset === null || items === null) return;
    try {
      const page = await api.events({ start: week, end, teamId: teamId || undefined, offset });
      setItems([...items, ...page.items]);
      setOffset(page.next_offset);
    } catch (err) {
      setError(errorMessage(err));
    }
  }

  return (
    <div>
      <header className="page-header">
        <h1>團隊行程</h1>
        {session.teams.length > 1 && (
          <select aria-label="團隊" value={teamId} onChange={(e) => setTeamId(e.target.value)}>
            <option value="">所有團隊</option>
            {session.teams.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
          </select>
        )}
      </header>
      <div className="week-nav">
        <button aria-label="上一週" onClick={() => setWeek(addDays(week, -7))}>‹</button>
        <span>{dayLabel(week)}–{dayLabel(end)}</span>
        <button aria-label="下一週" onClick={() => setWeek(addDays(week, 7))}>›</button>
      </div>
      {session.teams.length === 0 && (
        <Status kind="empty" title="您還沒有加入任何團隊" message="加入團隊後，就能看到同事的忙碌時段。請聯絡管理員。" />
      )}
      {error && <p className="alert" role="alert">{error}</p>}
      {items === null && !error && <Status kind="loading" />}
      {items !== null && items.length === 0 && session.teams.length > 0 && (
        <Status kind="empty" title="這週沒有已登錄的行程" message="沒有資料不代表大家都有空。" />
      )}
      {items !== null && items.length > 0 && <EventList items={items} showOwner />}
      {offset !== null && <button className="more" onClick={() => void loadMore()}>載入更多</button>}
      <p className="scope-note">{SCOPE_NOTE}</p>
    </div>
  );
}
