import { expect, test } from "vitest";
import { dateTime, relativeTime } from "./time";

test("a date and time reads as toLocaleString gives it, and a value that is not one as Invalid Date", () => {
  const options = { year: "numeric", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" } as const;
  const iso = "2026-09-24T15:20:00Z";
  expect(dateTime(iso)).toBe(new Date(iso).toLocaleString("en", options));
  expect(dateTime("next week")).toBe("Invalid Date");
});

test("relative times count up to a week, then give the date", () => {
  const now = Date.parse("2026-09-24T12:00:00Z");
  const ago = (seconds: number) => relativeTime(new Date(now - seconds * 1000).toISOString(), now);
  expect([ago(-5), ago(30), ago(5 * 60), ago(2 * 3600), ago(86_400), ago(3 * 86_400)]).toEqual([
    "now",
    "now",
    "5m ago",
    "2h ago",
    "yesterday",
    "3d ago",
  ]);
  expect(ago(10 * 86_400)).toBe("Sep 14, 2026");
});
