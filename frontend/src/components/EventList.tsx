import type { EventItem } from "../api";
import { addDays, dateOf, dayLabel, eventDay, timeOf } from "../dates";

export const SCOPE_NOTE = "僅顯示本系統已登錄且您有權限查看的行程";

const VISIBILITY_LABEL = { private: "私人", busy_only: "僅忙碌", team_detail: "團隊可見" } as const;

function timeText(e: EventItem): string {
  if (e.all_day) return "整天";
  if (!e.end_local) return timeOf(e.start_local);
  const crossDay = dateOf(e.end_local) !== dateOf(e.start_local);
  return `${timeOf(e.start_local)}–${crossDay ? `${dayLabel(dateOf(e.end_local))} ` : ""}${timeOf(e.end_local)}`;
}

/** 多日全天行程只列在第一天，另註明到哪一天。 */
function multiDayNote(e: EventItem): string | null {
  if (!e.all_day || !e.start_date || !e.end_date) return null;
  const last = addDays(e.end_date, -1);
  return last === e.start_date ? null : `至 ${dayLabel(last)}`;
}

export function EventList({
  items,
  showOwner,
  actions,
}: {
  items: EventItem[];
  showOwner: boolean;
  actions?: (e: EventItem) => React.ReactNode;
}) {
  const days = new Map<string, EventItem[]>();
  for (const e of items) {
    const day = eventDay(e);
    days.set(day, [...(days.get(day) ?? []), e]);
  }
  return (
    <div className="event-list">
      {[...days.entries()]
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([day, list]) => (
          <section key={day} className="day">
            <h3 className="day-label">{dayLabel(day)}</h3>
            <ul>
              {list.map((e) => (
                <li key={e.id} className={`event ${e.projection === "busy" ? "event-busy" : ""}`}>
                  <span className="event-time">{timeText(e)}</span>
                  <span className="event-main">
                    {showOwner && <span className="event-owner">{e.owner_name}</span>}
                    <span className="event-title">{e.projection === "busy" ? "忙碌" : e.title}</span>
                    {multiDayNote(e) && <span className="event-meta">{multiDayNote(e)}</span>}
                    {e.location && <span className="event-meta">{e.location}</span>}
                    {e.role === "attendee" && <span className="event-meta">由 {e.owner_name} 邀請</span>}
                    {e.role === "owner" && e.attendees?.length > 0 && (
                      <span className="event-meta">與 {e.attendees.map((a) => a.name).join("、")}</span>
                    )}
                    {e.series && <span className="event-meta">每週重複</span>}
                    {e.mine && e.visibility && e.visibility !== "team_detail" && (
                      <span className="event-meta">{VISIBILITY_LABEL[e.visibility]}</span>
                    )}
                  </span>
                  {actions && <span className="event-actions">{actions(e)}</span>}
                </li>
              ))}
            </ul>
          </section>
        ))}
    </div>
  );
}
