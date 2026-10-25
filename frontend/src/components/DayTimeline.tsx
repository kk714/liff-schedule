// 團隊單日時間軸：每位成員一欄、縱向為時間。
// - 受邀的行程同時出現在主辦人與每位參與者的欄位
// - 淺綠色帶＝這段時間所有人都沒有行程（≥30 分鐘；有人整天行程時當天沒有共同空檔）
// - 標題最多兩行，點一下看完整內容
import { useState } from "react";

import type { EventItem, Member } from "../api";
import { dateOf, spanText, timeOf } from "../dates";

const HOUR_PX = 48;
const MIN_BLOCK_PX = 22;
const DEFAULT_FIRST = 8;
const DEFAULT_LAST = 20;
const MIN_FREE_MINUTES = 30;

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
  if (dateOf(item.start_local) > day || (item.end_local && dateOf(item.end_local) < day)) return null; // 不在這一天
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

/** 所有欄位都沒有行程的時段（分鐘）。 */
export function commonFree(busy: [number, number][], from: number, to: number): [number, number][] {
  const merged: [number, number][] = [];
  for (const [s, e] of [...busy].sort((a, b) => a[0] - b[0])) {
    const last = merged[merged.length - 1];
    if (last && s <= last[1]) last[1] = Math.max(last[1], e);
    else merged.push([s, e]);
  }
  const free: [number, number][] = [];
  let cursor = from;
  for (const [s, e] of merged) {
    if (s > cursor) free.push([cursor, Math.min(s, to)]);
    cursor = Math.max(cursor, e);
  }
  if (cursor < to) free.push([cursor, to]);
  return free.filter(([s, e]) => e - s >= MIN_FREE_MINUTES);
}

/** 行程屬於哪些成員的欄位：主辦人與每位參與者。 */
function columnsOf(e: EventItem): string[] {
  return [e.owner_user_id, ...(e.attendees ?? []).map((a) => a.id)];
}

const label = (e: EventItem) => (e.projection === "busy" ? "忙碌" : e.title ?? "");

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
  const [selected, setSelected] = useState<EventItem | null>(null);

  // 成員欄位以名單為準；名單外但看得到的行程（例如剛離開團隊）也補一欄
  const columns = members.map((m) => ({ id: m.id, name: m.display_name }));
  for (const e of items) {
    if (!columns.some((c) => c.id === e.owner_user_id)) columns.push({ id: e.owner_user_id, name: e.owner_name });
  }
  const byColumn = new Map(columns.map((c) => [c.id, items.filter((e) => columnsOf(e).includes(c.id))]));
  const blocksByColumn = new Map(columns.map((c) => [c.id, layout(byColumn.get(c.id) ?? [], day)]));
  const allBlocks = [...blocksByColumn.values()].flat();
  const [first, last] = hourRange(allBlocks);
  const height = (last - first) * HOUR_PX;
  const hours = Array.from({ length: last - first + 1 }, (_, i) => first + i);
  const allDay = items.filter((e) => e.all_day);
  const someoneAllDay = columns.some((c) => (byColumn.get(c.id) ?? []).some((e) => e.all_day));
  const free = someoneAllDay
    ? []
    : commonFree(allBlocks.map((b) => [b.startMin, b.endMin]), first * 60, last * 60);
  const gridCols = { gridTemplateColumns: `40px repeat(${columns.length}, minmax(80px, 1fr))` };
  const y = (min: number) => ((min - first * 60) / 60) * HOUR_PX;

  return (
    <>
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
                  {(byColumn.get(c.id) ?? []).filter((e) => e.all_day).map((e) => (
                    <button type="button" key={e.id} className={`tl-chip ${e.projection === "busy" ? "busy" : ""}`}
                            onClick={() => setSelected(e)}>
                      {label(e)}
                    </button>
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
              {free.map(([s, e]) => (
                <div key={`free-${s}`} className="tl-free" style={{ top: y(s), height: y(e) - y(s) }} aria-hidden />
              ))}
              {(blocksByColumn.get(c.id) ?? []).map((b) => {
                const blockHeight = Math.max(MIN_BLOCK_PX, ((b.endMin - b.startMin) / 60) * HOUR_PX - 2);
                const time = `${timeOf(b.item.start_local)}–${timeOf(b.item.end_local)}`;
                return (
                  <button
                    type="button"
                    key={b.item.id}
                    className={`tl-event ${b.item.projection === "busy" ? "busy" : ""} ${blockHeight < 40 ? "short" : ""}`}
                    style={{ top: y(b.startMin), height: blockHeight,
                             left: `${(b.lane / b.lanes) * 100}%`, width: `${100 / b.lanes}%` }}
                    aria-label={`${label(b.item)} ${time}`}
                    onClick={() => setSelected(b.item)}
                  >
                    <span className="tl-event-title">{label(b.item)}</span>
                  </button>
                );
              })}
              {nowMinutes != null && nowMinutes >= first * 60 && nowMinutes <= last * 60 && (
                <div className="tl-now" style={{ top: y(nowMinutes) }} aria-hidden />
              )}
            </div>
          ))}
        </div>
      </div>
      {free.length > 0 && <p className="tl-legend"><span className="tl-legend-swatch" />大家都有空的時段</p>}

      {selected && (
        <div className="sheet-backdrop" onClick={() => setSelected(null)}>
          <div className="sheet" role="dialog" aria-modal="true" aria-label="行程詳情" onClick={(e) => e.stopPropagation()}>
            <h2>{label(selected)}</h2>
            <p>{spanText(selected)}</p>
            {selected.location && <p className="muted">地點：{selected.location}</p>}
            <p className="muted">主辦：{selected.owner_name}</p>
            {selected.attendees?.length > 0 && (
              <p className="muted">參與：{selected.attendees.map((a) => a.name).join("、")}</p>
            )}
            {selected.notes && <p className="sheet-notes">{selected.notes}</p>}
            <button className="primary" onClick={() => setSelected(null)}>關閉</button>
          </div>
        </div>
      )}
    </>
  );
}
