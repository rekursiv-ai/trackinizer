import { expect, test } from "vitest";
import { at, bucketsFor, fractionOf, type History, markOf, moveTo, spanOf, ticks, trackOf, type View, ZOOMS, zoomTo } from "./timeline";

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;
/** A now that falls on no round time, so alignment shows. */
const NOW = Date.UTC(2026, 9, 2, 21, 15, 7);
/** The band's history: the last 7 days, as far back as the server's histogram counts. */
const HISTORY: History = { first: NOW - 7 * DAY, now: NOW };
const LIVE_HOUR: View = { zoom: "hour", end: null };

test("the zooms are Hour, Day and Week; a live view ends at now and follows it, each zoom its length long, Week the whole history", () => {
  expect(ZOOMS).toEqual(["hour", "day", "week"]);
  expect(spanOf(LIVE_HOUR, HISTORY)).toEqual({ start: NOW - HOUR, end: NOW });
  expect(spanOf(LIVE_HOUR, { first: HISTORY.first + 5 * MINUTE, now: NOW + 5 * MINUTE })).toEqual({ start: NOW - 55 * MINUTE, end: NOW + 5 * MINUTE });
  expect(spanOf({ zoom: "day", end: null }, HISTORY)).toEqual({ start: NOW - DAY, end: NOW });
  expect(spanOf({ zoom: "week", end: null }, HISTORY)).toEqual({ start: HISTORY.first, end: NOW });
});

test("scrolling back leaves now and stops at the 7-day mark; reaching now follows it again", () => {
  const back = moveTo(LIVE_HOUR, NOW - 2 * HOUR, HISTORY);
  expect(back).toEqual({ zoom: "hour", end: NOW - 2 * HOUR });
  // Time passing leaves a view scrolled back where it is.
  expect(spanOf(back, { first: HISTORY.first + HOUR, now: NOW + HOUR })).toEqual({ start: NOW - 3 * HOUR, end: NOW - 2 * HOUR });
  expect(moveTo(back, NOW - 20 * DAY, HISTORY)).toEqual({ zoom: "hour", end: HISTORY.first + HOUR });
  expect(moveTo(back, NOW, HISTORY)).toEqual(LIVE_HOUR);
  expect(moveTo(back, NOW + HOUR, HISTORY)).toEqual(LIVE_HOUR);
  // A band at the mark moves on with it, since the server counts nothing before it.
  const atMark = moveTo(back, NOW - 20 * DAY, HISTORY);
  expect(spanOf(atMark, { first: HISTORY.first + 10 * MINUTE, now: NOW + 10 * MINUTE })).toEqual({
    start: HISTORY.first + 10 * MINUTE,
    end: HISTORY.first + 70 * MINUTE,
  });
  // A history shorter than the span has nowhere to scroll, and nor has Week, which spans the whole history.
  expect(moveTo(LIVE_HOUR, NOW - 2 * MINUTE, { first: NOW - 30 * MINUTE, now: NOW })).toEqual(LIVE_HOUR);
  expect(moveTo({ zoom: "week", end: null }, NOW - DAY, HISTORY)).toEqual({ zoom: "week", end: null });
});

test("zooming keeps the time it is given where it is on the band, else the right edge, so a live band stays live", () => {
  const liveDay: View = { zoom: "day", end: null };
  expect(zoomTo(liveDay, "hour", HISTORY, null)).toEqual(LIVE_HOUR);
  expect(zoomTo(liveDay, "hour", HISTORY, NOW)).toEqual(LIVE_HOUR);
  // The feed's newest record halfway along a day stays halfway along the hour.
  const day: View = { zoom: "day", end: NOW - 2 * DAY };
  const fixed = NOW - 2 * DAY - DAY / 2;
  expect(spanOf(zoomTo(day, "hour", HISTORY, fixed), HISTORY)).toEqual({ start: fixed - HOUR / 2, end: fixed + HOUR / 2 });
  // A time off the band fixes nothing: the right edge stays, within the history.
  expect(zoomTo(day, "hour", HISTORY, NOW - DAY)).toEqual({ zoom: "hour", end: NOW - 2 * DAY });
  expect(zoomTo({ zoom: "hour", end: HISTORY.first + 2 * HOUR }, "day", HISTORY, null)).toEqual({ zoom: "day", end: HISTORY.first + DAY });
  // Week spans the whole history, so it follows now.
  expect(zoomTo(day, "week", HISTORY, fixed)).toEqual({ zoom: "week", end: null });
});

test("a fraction along a span is a time, and a time a fraction, past either end too", () => {
  const span = { start: 1_000, end: 5_000 };
  expect([0, 0.25, 1].map((fraction) => at(span, fraction))).toEqual([1_000, 2_000, 5_000]);
  expect([1_000, 2_000, 5_000, 9_000].map((time) => fractionOf(span, time))).toEqual([0, 0.25, 1, 2]);
});

test("the track spans the history, and the band's span sits in it where it is", () => {
  const back = moveTo(LIVE_HOUR, NOW - 3.5 * DAY, HISTORY);
  const track = trackOf(back, HISTORY);
  expect(track).toEqual({ start: HISTORY.first, end: NOW });
  expect(fractionOf(track, spanOf(back, HISTORY).end)).toBe(0.5);
  // A history shorter than the span: the track is the span, the thumb all of it.
  const young = { first: NOW - MINUTE, now: NOW };
  expect(trackOf(LIVE_HOUR, young)).toEqual(spanOf(LIVE_HOUR, young));
});

test("the mark is the feed's window clipped to the band, as fractions; none when it is off the band or unknown", () => {
  const span = { start: 1_000, end: 5_000 };
  expect(markOf(span, { start: 2_000, end: 3_000 })).toEqual({ left: 0.25, right: 0.5 });
  expect(markOf(span, { start: 0, end: 2_000 })).toEqual({ left: 0, right: 0.25 });
  expect(markOf(span, { start: 4_000, end: 9_000 })).toEqual({ left: 0.75, right: 1 });
  expect(markOf(span, { start: 3_000, end: 3_000 })).toEqual({ left: 0.5, right: 0.5 });
  expect(markOf(span, { start: 0, end: 500 })).toBeNull();
  expect(markOf(span, { start: 6_000, end: 7_000 })).toBeNull();
  expect(markOf(span, null)).toBeNull();
});

test("buckets fit the width at a round length, aligned to it, cover the span, and stay under the server's cap", () => {
  const hour = spanOf(LIVE_HOUR, HISTORY);
  // 900 px holds 150 bars: 24 s each would do for an hour, so 30 s.
  expect(bucketsFor(hour, 900)).toEqual({ since: Date.UTC(2026, 9, 2, 20, 15), until: Date.UTC(2026, 9, 2, 21, 15, 30), seconds: 30, count: 121 });
  expect(bucketsFor(spanOf({ zoom: "day", end: null }, HISTORY), 900)).toMatchObject({ seconds: 600, count: 145 });
  expect(bucketsFor(spanOf({ zoom: "week", end: null }, HISTORY), 900)).toMatchObject({ seconds: 7_200, count: 85 });
  expect(bucketsFor(hour, 10_000)).toMatchObject({ seconds: 5, count: 721 });
  expect(bucketsFor(hour, 0)).toMatchObject({ seconds: 3_600, count: 2 });
  // Past the longest round length, whole days.
  const decade = { start: NOW - 3_653 * DAY, end: NOW };
  const long = bucketsFor(decade, 900);
  expect(long.seconds % 86_400).toBe(0);
  expect(long.count).toBeLessThanOrEqual(150);
  // A thousand 1 s bars would fit, but its ragged ends need a thousand and one.
  const tight = { start: NOW + 500, end: NOW + 999_500 };
  for (const [span, width] of [[hour, 900], [hour, 1e6], [decade, 333], [tight, 1e6]] as const) {
    const { since, until, seconds, count } = bucketsFor(span, width);
    expect(since % (seconds * 1000)).toBe(0);
    expect(since).toBeLessThanOrEqual(span.start);
    expect(until).toBeGreaterThanOrEqual(span.end);
    expect(count).toBe((until - since) / (seconds * 1000));
    // A read asks for one bucket more, the one its end opens (`spanRead`).
    expect(count + 1).toBeLessThanOrEqual(1000);
  }
});

test("ticks fall on round local times across the band, each labelled by its clock, or by its date at midnight", () => {
  const hour = spanOf(LIVE_HOUR, HISTORY);
  const every15 = ticks(hour, 900);
  expect(every15.length).toBeGreaterThanOrEqual(3);
  expect(every15[0]!.at - hour.start).toBeLessThan(15 * MINUTE);
  expect(hour.end - every15.at(-1)!.at).toBeLessThan(15 * MINUTE);
  for (const [k, { at: time, label }] of every15.entries()) {
    const local = new Date(time);
    if (k > 0) expect(time - every15[k - 1]!.at).toBe(15 * MINUTE);
    expect([local.getMinutes() % 15, local.getSeconds(), local.getMilliseconds()]).toEqual([0, 0, 0]);
    expect(label).toBe(local.getHours() || local.getMinutes() ? clock(local) : date(local));
  }
  const week = spanOf({ zoom: "week", end: null }, HISTORY);
  const days = ticks(week, 900);
  expect(days.length).toBeGreaterThanOrEqual(3);
  for (const { at: time, label } of days) {
    const local = new Date(time);
    expect([local.getHours(), local.getMinutes()]).toEqual([0, 0]);
    expect(label).toBe(date(local));
    expect(time).toBeGreaterThanOrEqual(week.start);
    expect(time).toBeLessThanOrEqual(week.end);
  }
});

function clock(local: Date): string {
  return `${String(local.getHours()).padStart(2, "0")}:${String(local.getMinutes()).padStart(2, "0")}`;
}

function date(local: Date): string {
  return `${local.toLocaleString("en", { month: "short" })} ${local.getDate()}`;
}
