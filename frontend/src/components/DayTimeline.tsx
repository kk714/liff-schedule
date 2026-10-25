// 團隊單日時間軸：每位成員一欄、縱向為時間，空白處就是可以開會的空檔。
// 成員一律以系統名字顯示；欄位多時在時間軸內左右捲動（時間欄固定）。
import type { EventItem, Member } from "../api";
import { dateOf, timeOf } from "../dates";

const HOUR_PX = 48;
const MIN_BLOCK_PX = 22;
const DEFAULT_FIRST = 8;
const DEFAULT_LAST = 20;

interface Block {
  item: EventItem;
  startMin: number;
  endMin: number;
  lane: number;
  lanes: number;
}

const minutes = (hhmm: string) => Number(hhmm.slice(0, 2)) * 60 + Number(hhmm.slice(3, 5));

/** 把行程裁切到這一天的 0–24 時（跨日行程只畫當天的部分）。 */
function clip(item: EventItem, day: string): [number, number] | null {
  if (item.all_day || !item.start_local) return null;
  const start = dateOf(item.start_local) === day ? minutes(timeOf(item.start_local)) : 0;
  const end = item.end_local && dateOf(item.end_local) === day ? minutes(timeOf(item.end_local)) : 24 * 60;
  return end > start ? [start, end] : null;
}

/** 同一人重疊的行程並排顯示。 */
function layout(items: EventItem[], day: string): Block[] {
  const blocks = items
    .map((item) => ({ item, span: clip(item, day) }))
    .filter((b): b is { item: EventItem; span: [number, number] } => b.span !== null)
    .sort((a, b) => a.span[0] - b.span[0] || b.span[1] - a.span[1])
    .map(({ item, span }) => ({ item, startMin: span[0], endMin: span[1], lane: 0, lanes: 1 }));
  const laneEnds: number[] = [];
  for (const b of blocks) {
    let lane = laneEnds.findIndex((end) => end <= b.startMin);
    if (lane === -1) lane = laneEnds.push(0) - 1;
    laneEnds[lane] = b.endMin;
    b.lane = lane;
  }
  for (const b of blocks) b.lanes = laneEnds.length;
  return blocks;
}

function hourRange(blocks: Block[]): [number, number] {
  if (!blocks.length) return [DEFAULT_FIRST, DEFAULT_LAST];
  const first = Math.min(DEFAULT_FIRST, ...blocks.map((b) => Math.floor(b.startMin / 60)));
  const last = Math.max(DEFAULT_LAST, ...blocks.map((b) => Math.ceil(b.endMin / 60)));
  return [first, Math.min(24, last)];
}

export function DayTimeline({
  day,
  members,
  items,
  nowMinutes,
}: {
  day: string;
  members: Member[];
  items: EventItem[];
  nowMinutes?: number | null; // 今天才傳入，用來畫現在時間線
}) {
  // 成員欄位以名單為準；名單外但看得到的行程（例如剛離開團隊）也補一欄
  const columns = [...members.map((m) => ({ id: m.id, name: m.display_name }))];
  for (const e of items) {
    if (!columns.some((c) => c.id === e.owner_user_id)) columns.push({ id: e.owner_user_id, name: e.owner_name });
  }
  const byOwner = new Map(columns.map((c) => [c.id, items.filter((e) => e.owner_user_id === c.id)]));
  const blocksByOwner = new Map(columns.map((c) => [c.id, layout(byOwner.get(c.id) ?? [], day)]));
  const [first, last] = hourRange([...blocksByOwner.values()].flat());
  const height = (last - first) * HOUR_PX;
  const hours = Array.from({ length: last - first + 1 }, (_, i) => first + i);
  const allDay = items.filter((e) => e.all_day);
  const gridCols = { gridTemplateColumns: `44px repeat(${columns.length}, minmax(92px, 1fr))` };

  return (
    <div className="timeline-scroll">
      <div className="timeline" style={gridCols} role="grid" aria-label="團隊單日行程">
        <div className="tl-corner" />
        {columns.map((c) => (
          <div key={c.id} className="tl-name" role="columnheader">{c.name}</div>
        ))}

        {allDay.length > 0 && (
          <>
            <div className="tl-allday-label">整天</div>
            {columns.map((c) => (
              <div key={c.id} className="tl-allday">
                {(byOwner.get(c.id) ?? []).filter((e) => e.all_day).map((e) => (
                  <span key={e.id} className={`tl-chip ${e.projection === "busy" ? "busy" : ""}`}>
                    {e.projection === "busy" ? "忙碌" : e.title}
                  </span>
                ))}
              </div>
            ))}
          </>
        )}

        <div className="tl-hours" style={{ height }}>
          {hours.slice(0, -1).map((h) => (
            <span key={h} style={{ top: (h - first) * HOUR_PX }}>{String(h).padStart(2, "0")}:00</span>
          ))}
        </div>
        {columns.map((c) => (
          <div key={c.id} className="tl-col" style={{ height, backgroundSize: `100% ${HOUR_PX}px` }}>
            {(blocksByOwner.get(c.id) ?? []).map((b) => {
              const top = ((b.startMin - first * 60) / 60) * HOUR_PX;
              const blockHeight = Math.max(MIN_BLOCK_PX, ((b.endMin - b.startMin) / 60) * HOUR_PX - 2);
              const label = b.item.projection === "busy" ? "忙碌" : b.item.title;
              const time = `${timeOf(b.item.start_local)}–${timeOf(b.item.end_local)}`;
              return (
                <div
                  key={b.item.id}
                  className={`tl-event ${b.item.projection === "busy" ? "busy" : ""}`}
                  style={{ top, height: blockHeight, left: `${(b.lane / b.lanes) * 100}%`, width: `${100 / b.lanes}%` }}
                  title={`${label} ${time}`}
                >
                  <span className="tl-event-title">{label}</span>
                  {blockHeight > 36 && <span className="tl-event-time">{time}</span>}
                </div>
              );
            })}
            {nowMinutes != null && nowMinutes >= first * 60 && nowMinutes <= last * 60 && (
              <div className="tl-now" style={{ top: ((nowMinutes - first * 60) / 60) * HOUR_PX }} aria-hidden />
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
