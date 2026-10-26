import { addDays, addMonths, dayLabel, monthRange, spanText, todayIn, weekStart } from "../dates";

const timed = (s: string, e: string | null) => ({ all_day: false, start_local: s, end_local: e, start_date: null, end_date: null });

test("weekStart is Monday", () => {
  expect(weekStart("2026-10-21")).toBe("2026-10-19"); // 週三
  expect(weekStart("2026-10-25")).toBe("2026-10-19"); // 週日仍屬本週
  expect(weekStart("2026-10-26")).toBe("2026-10-26");
});

test("addDays crosses month and year", () => {
  expect(addDays("2026-10-31", 1)).toBe("2026-11-01");
  expect(addDays("2026-12-31", 1)).toBe("2027-01-01");
  expect(addDays("2026-03-01", -1)).toBe("2026-02-28");
});

test("monthRange", () => {
  expect(monthRange("2026-02-10")).toEqual(["2026-02-01", "2026-02-28"]);
  expect(monthRange("2028-02-10")).toEqual(["2028-02-01", "2028-02-29"]);
});

test("dayLabel shows weekday", () => {
  expect(dayLabel("2026-11-01")).toBe("11/1（日）");
});

test("spanText for timed, cross-day and all-day", () => {
  expect(spanText(timed("2026-11-01T14:00:00", "2026-11-01T15:00:00"))).toBe("11/1（日） 14:00–15:00");
  expect(spanText(timed("2026-11-01T23:00:00", "2026-11-02T01:00:00"))).toBe("11/1（日） 23:00–11/2（一） 01:00");
  const allDay = { all_day: true, start_local: null, end_local: null, start_date: "2026-11-10", end_date: "2026-11-11" };
  expect(spanText(allDay)).toBe("11/10（二） 整天");
  expect(spanText({ ...allDay, end_date: "2026-11-13" })).toBe("11/10（二）–11/12（四） 整天");
});

test("todayIn uses the user's timezone, not the browser's", () => {
  const instant = new Date("2026-10-21T17:30:00Z"); // 台北已是 10/22 01:30
  expect(todayIn("Asia/Taipei", instant)).toBe("2026-10-22");
  expect(todayIn("America/Los_Angeles", instant)).toBe("2026-10-21");
});

test("addMonths keeps the day or clamps to month end", () => {
  expect(addMonths("2026-10-26", 1)).toBe("2026-11-26");
  expect(addMonths("2026-12-15", 1)).toBe("2027-01-15");
  expect(addMonths("2026-01-31", 1)).toBe("2026-02-28");
  expect(addMonths("2026-01-10", -1)).toBe("2025-12-10");
});
