// 團隊行程：一天一天看，以時間軸呈現每位成員的行程；點選要開會的成員，標出他們的共同空檔。
// 權限由後端套用：本人與團隊可見的行程顯示標題，「僅忙碌」只顯示忙碌。
import { useEffect, useState } from "react";

import { useApp } from "../App";
import { api, errorMessage, type EventItem, type Member } from "../api";
import { DayTimeline } from "../components/DayTimeline";
import { SCOPE_NOTE } from "../components/EventList";
import { Status } from "../components/Status";
import { addDays, dayLabel, todayIn } from "../dates";

function nowMinutesIn(timeZone: string): number {
  const parts = new Intl.DateTimeFormat("en-GB", { timeZone, hour: "2-digit", minute: "2-digit", hour12: false })
    .formatToParts(new Date());
  const get = (t: string) => Number(parts.find((p) => p.type === t)!.value);
  return (get("hour") % 24) * 60 + get("minute");
}

export function TeamPage() {
  const { session } = useApp();
  const tz = session.user.timezone;
  const today = todayIn(tz);
  const [day, setDay] = useState(today);
  const [teamId, setTeamId] = useState<string>("");
  const [members, setMembers] = useState<Member[] | null>(null);
  const [items, setItems] = useState<EventItem[] | null>(null);
  const [truncated, setTruncated] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [compareIds, setCompareIds] = useState<string[]>([]);
  const toggle = (id: string) => setCompareIds((ids) => (ids.includes(id) ? ids.filter((x) => x !== id) : [...ids, id]));

  useEffect(() => {
    let cancelled = false;
    setItems(null);
    setError(null);
    Promise.all([api.members(teamId || undefined), api.events({ start: day, end: day, teamId: teamId || undefined })])
      .then(([m, page]) => {
        if (cancelled) return;
        setMembers(m.members);
        setItems(page.items);
        setTruncated(page.has_more);
      })
      .catch((err) => !cancelled && setError(errorMessage(err)));
    return () => {
      cancelled = true;
    };
  }, [day, teamId]);

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
      <div className="day-nav">
        <button aria-label="前一天" onClick={() => setDay(addDays(day, -1))}>‹</button>
        <label className="day-picker">
          <span>{dayLabel(day)}{day === today ? "・今天" : ""}</span>
          <input type="date" aria-label="選擇日期" value={day} onChange={(e) => e.target.value && setDay(e.target.value)} />
        </label>
        <button aria-label="後一天" onClick={() => setDay(addDays(day, 1))}>›</button>
        {day !== today && <button className="small" onClick={() => setDay(today)}>今天</button>}
      </div>

      {session.teams.length === 0 && (
        <Status kind="empty" title="您還沒有加入任何團隊" message="加入團隊後，就能在這裡看到大家的行程。請聯絡管理員。" />
      )}
      {error && <p className="alert" role="alert">{error}</p>}
      {session.teams.length > 0 && (items === null || members === null) && !error && <Status kind="loading" />}
      {items !== null && members !== null && session.teams.length > 0 && (
        <>
          <div className="compare" role="group" aria-label="找共同空檔">
            <span className="compare-label">找共同空檔</span>
            {members.map((m) => (
              <button key={m.id} type="button" className={`chip small ${compareIds.includes(m.id) ? "on" : ""}`}
                      aria-pressed={compareIds.includes(m.id)} onClick={() => toggle(m.id)}>
                {m.display_name}
              </button>
            ))}
            {compareIds.length > 0 && <button type="button" className="link" onClick={() => setCompareIds([])}>清除</button>}
          </div>
          {compareIds.length === 0 && <p className="muted compare-hint">點選要一起開會的成員，時間軸會用綠色標出他們都有空的時段。</p>}
          <DayTimeline day={day} members={members} items={items} compareIds={compareIds}
                       nowMinutes={day === today ? nowMinutesIn(tz) : null} />
          {items.length === 0 && <p className="muted center">這天還沒有人登錄行程。沒有資料不代表大家都有空。</p>}
          {truncated && <p className="muted center">行程超過 100 筆，部分未顯示。</p>}
        </>
      )}
      <p className="scope-note">{SCOPE_NOTE}</p>
    </div>
  );
}
