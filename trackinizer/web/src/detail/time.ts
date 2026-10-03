import { useEffect, useState } from "react";

/** How long ago `iso` was, from `now` (ms): `now`, `5m ago`, `yesterday`, `3d ago`, then a date. */
export function relativeTime(iso: string, now: number): string {
  // A clock a little behind the server's must not read "in 3s".
  const seconds = Math.max(0, (now - Date.parse(iso)) / 1000);
  for (const [unit, size, below] of UNITS) {
    if (seconds < below) return formats().relative.format(-Math.floor(seconds / size), unit);
  }
  return new Date(iso).toLocaleDateString("en", { month: "short", day: "numeric", year: "numeric" });
}

/** `Sep 24, 2026, 3:20 PM`; a value that is not a time reads `Invalid Date`. */
export function dateTime(iso: string): string {
  const date = new Date(iso);
  // One formatter for every call, as MDN advises for formatting many dates:
  // toLocaleString builds one per call, and a list calls this for each row.
  return Number.isNaN(date.getTime()) ? String(date) : formats().dateTime.format(date);
}

/**
 * `Jan 5, 2024`: the calendar date `iso` stores at midnight UTC, which a local
 * clock west of UTC would show as the day before.
 */
export function calendarDate(iso: string): string {
  return new Date(iso).toLocaleDateString("en", { year: "numeric", month: "short", day: "numeric", timeZone: "UTC" });
}

/** The client clock, updated every minute, so relative times age while a page stays open. */
export function useMinuteClock(): number {
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 60_000);
    return () => clearInterval(timer);
  }, []);
  return now;
}

/**
 * Make the formatters now, in a task of their own, ahead of the first render
 * that formats a time; the app asks as it starts. The first formatter loads the
 * locale's data: made as the app's chunk loaded, that was 31 of the 36 ms its
 * load took with the CPU slowed 4x on a Xeon, and made on first use, the first
 * list's render would pay it.
 */
export function prepareTimes(): void {
  formats();
}

let made: { readonly relative: Intl.RelativeTimeFormat; readonly dateTime: Intl.DateTimeFormat } | undefined;

/** One formatter of each kind for every call, as MDN advises for formatting many dates. */
function formats() {
  return (made ??= {
    relative: new Intl.RelativeTimeFormat("en", { style: "narrow", numeric: "auto" }),
    dateTime: new Intl.DateTimeFormat("en", { year: "numeric", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }),
  });
}

/** Each unit, its length in seconds, and the age below which it is used. */
const UNITS: readonly (readonly [Intl.RelativeTimeFormatUnit, number, number])[] = [
  ["second", 60, 60],
  ["minute", 60, 3600],
  ["hour", 3600, 86_400],
  ["day", 86_400, 7 * 86_400],
];
