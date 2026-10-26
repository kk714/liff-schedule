// 日期工具。後端回傳的 *_local 已是使用者時區的當地時間字串，前端只做字串與日曆運算，
// 不經過瀏覽器時區換算（手機時區可能和使用者設定不同）。

const WEEKDAYS = "日一二三四五六";

export interface YMD {
  y: number;
  m: number;
  d: number;
}

export function parseDate(iso: string): YMD {
  const [y, m, d] = iso.slice(0, 10).split("-").map(Number);
  return { y, m, d };
}

export function formatISODate({ y, m, d }: YMD): string {
  return `${y}-${String(m).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
}

function toUTC({ y, m, d }: YMD): Date {
  return new Date(Date.UTC(y, m - 1, d));
}

export function addDays(iso: string, days: number): string {
  const t = toUTC(parseDate(iso));
  t.setUTCDate(t.getUTCDate() + days);
  return formatISODate({ y: t.getUTCFullYear(), m: t.getUTCMonth() + 1, d: t.getUTCDate() });
}

export function weekday(iso: string): string {
  return WEEKDAYS[toUTC(parseDate(iso)).getUTCDay()];
}

/** 一週為週一至週日（§6）。 */
export function weekStart(iso: string): string {
  const day = toUTC(parseDate(iso)).getUTCDay(); // 0=日
  return addDays(iso, day === 0 ? -6 : 1 - day);
}

export function monthRange(iso: string): [string, string] {
  const { y, m } = parseDate(iso);
  const last = new Date(Date.UTC(y, m, 0)).getUTCDate();
  return [formatISODate({ y, m, d: 1 }), formatISODate({ y, m, d: last })];
}

/** 移到前後 n 個月的同一天；該月沒有這天就取月底（1/31 → 2/28）。 */
export function addMonths(iso: string, months: number): string {
  const { y, m, d } = parseDate(iso);
  const index = y * 12 + (m - 1) + months;
  const ny = Math.floor(index / 12);
  const nm = (index % 12) + 1;
  const last = new Date(Date.UTC(ny, nm, 0)).getUTCDate();
  return formatISODate({ y: ny, m: nm, d: Math.min(d, last) });
}

/** 使用者時區的「今天」。 */
export function todayIn(timeZone: string, now: Date = new Date()): string {
  const parts = new Intl.DateTimeFormat("en-CA", { timeZone, year: "numeric", month: "2-digit", day: "2-digit" })
    .formatToParts(now);
  const get = (t: string) => parts.find((p) => p.type === t)!.value;
  return `${get("year")}-${get("month")}-${get("day")}`;
}

export function dayLabel(iso: string): string {
  const { m, d } = parseDate(iso);
  return `${m}/${d}（${weekday(iso)}）`;
}

export const timeOf = (local: string | null): string => (local ? local.slice(11, 16) : "");
export const dateOf = (local: string | null): string => (local ? local.slice(0, 10) : "");

/** 行程顯示用的時間字串；全天的 endDate 不含當日。 */
export function spanText(e: {
  all_day: boolean;
  start_local: string | null;
  end_local: string | null;
  start_date: string | null;
  end_date: string | null;
}): string {
  if (e.all_day && e.start_date) {
    const last = e.end_date ? addDays(e.end_date, -1) : e.start_date;
    return last === e.start_date ? `${dayLabel(e.start_date)} 整天` : `${dayLabel(e.start_date)}–${dayLabel(last)} 整天`;
  }
  if (!e.start_local) return "時間未定";
  const start = `${dayLabel(dateOf(e.start_local))} ${timeOf(e.start_local)}`;
  if (!e.end_local) return start;
  return dateOf(e.end_local) === dateOf(e.start_local)
    ? `${start}–${timeOf(e.end_local)}`
    : `${start}–${dayLabel(dateOf(e.end_local))} ${timeOf(e.end_local)}`;
}

/** 行程在清單裡歸到哪一天（全天行程歸到第一天）。 */
export function eventDay(e: { all_day: boolean; start_local: string | null; start_date: string | null }): string {
  return e.all_day ? e.start_date ?? "" : dateOf(e.start_local);
}
