import { type QueryClient, QueryObserver } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import type { Histogram, HistogramRead } from "../api/histogram";
import { type Sent, stubFetch } from "../api/testing";
import { testClient } from "../live/testing";
import { HistogramLive, histogramQuery, iso, spanRead } from "./histogram";

const T0 = Date.UTC(2026, 9, 2, 21, 0);
const BUCKET = 30_000;
/** Four 30 s buckets of agent `a`'s records, from T0, the 7-day mark. */
const READ = spanRead({ actor: ["a"] }, { since: T0, until: T0 + 4 * BUCKET, seconds: 30, count: 4 }, T0);

let client: QueryClient;
let clock: number;
let sent: Sent[];
/** The server's counts, by bucket start; a bucket not here holds none. */
let counts: Map<number, number>;

beforeEach(() => {
  client = testClient();
  clock = T0 + 3.5 * BUCKET;
  counts = new Map([0, 1, 2, 3].map((k) => [T0 + k * BUCKET, k + 1]));
  // The server's grid: 30 s buckets from `since`, every one listed.
  sent = stubFetch((request) => {
    const query = new URL(request.url).searchParams;
    const since = Date.parse(query.get("since")!);
    const starts = Array.from({ length: Number(query.get("buckets")) }, (_, k) => since + k * BUCKET);
    return Response.json({
      start: iso(since),
      end: iso(starts.at(-1)! + BUCKET),
      bucket_seconds: 30,
      counts: starts.map((start) => ({ start: iso(start), count: counts.get(start) ?? 0 })),
    } satisfies Histogram);
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

/** The band's counts as the minimap holds them: loaded, with its observer mounted. */
async function shown(read: HistogramRead = READ): Promise<HistogramLive> {
  const query = histogramQuery(read);
  new QueryObserver(client, query).subscribe(() => {});
  await vi.waitFor(() => expect(client.getQueryData(query.queryKey)).toBeDefined(), { interval: 1 });
  sent.length = 0;
  const live = new HistogramLive(client, () => clock);
  live.follow(read);
  return live;
}

const held = (read: HistogramRead = READ) => client.getQueryData(histogramQuery(read).queryKey)?.counts.map(({ count }) => count);
const reads = () => sent.map((request) => Object.fromEntries(new URLSearchParams(request.query)));
const batch = { ids: new Set(["any"]), gap: false };

test("a span's read asks for its buckets and the one its aligned end opens, since the server's window holds its end", () => {
  expect(READ).toEqual({ actor: ["a"], since: iso(T0), until: iso(T0 + 4 * BUCKET), buckets: 5 });
});

test("a span's read starts at no bar before the 7-day mark: the one the mark falls in is left out, and 2 buckets are still asked for", () => {
  const four = { since: T0, until: T0 + 4 * BUCKET, seconds: 30, count: 4 };
  expect(spanRead({}, four, T0 + 10_000)).toEqual({ since: iso(T0 + BUCKET), until: iso(T0 + 4 * BUCKET), buckets: 4 });
  // The narrowest band, two bars, the mark in the first: the other, and the one its end opens.
  expect(spanRead({}, { since: T0, until: T0 + 2 * BUCKET, seconds: 30, count: 2 }, T0 + 10_000)).toEqual({
    since: iso(T0 + BUCKET),
    until: iso(T0 + 2 * BUCKET),
    buckets: 2,
  });
});

test("a batch reads again the bucket now falls in and the one before, on the band's grid; another within 2 s waits for the rest", async () => {
  const live = await shown();
  expect(held()).toEqual([1, 2, 3, 4, 0]);
  counts.set(T0 + 3 * BUCKET, 9);
  expect(await live.update(batch)).toBeNull();
  expect(reads()).toEqual([{ actor: "a", since: iso(T0 + 2 * BUCKET), until: iso(T0 + 4 * BUCKET), buckets: "3" }]);
  expect(held()).toEqual([1, 2, 3, 9, 0]);
  clock += 500;
  expect(await live.update(batch)).toEqual({ afterMs: 1_500 });
  expect(reads()).toHaveLength(1);
  clock += 1_500;
  counts.set(T0 + 3 * BUCKET, 12);
  expect(await live.update({ ids: new Set(), gap: false })).toBeNull();
  expect(held()).toEqual([1, 2, 3, 12, 0]);
});

test("as now passes the band's last bucket, the newer ones join it", async () => {
  const whole = { actor: ["a"], since: iso(T0), buckets: 4 };
  const live = await shown(whole);
  expect(held(whole)).toEqual([1, 2, 3, 4]);
  clock = T0 + 5.2 * BUCKET;
  counts.set(T0 + 5 * BUCKET, 2);
  await live.update(batch);
  expect(held(whole)).toEqual([1, 2, 3, 4, 0, 2, 0]);
});

test("scrolled back, a batch reads nothing; a gap reads the whole band again at once", async () => {
  const live = await shown();
  live.follow(null);
  expect(await live.update(batch)).toBeNull();
  expect(reads()).toEqual([]);
  live.follow(READ);
  await live.update(batch);
  counts.set(T0, 7);
  expect(await live.update({ ids: new Set(), gap: true })).toBeNull();
  expect(reads().map(({ buckets }) => buckets)).toEqual(["3", "5"]);
  expect(held()).toEqual([7, 2, 3, 4, 0]);
});

test("a batch while the band's first read is under way waits for it, since that read may predate the change", async () => {
  let answer = (_: Response) => {};
  vi.stubGlobal("fetch", () => new Promise<Response>((resolve) => (answer = resolve)));
  new QueryObserver(client, histogramQuery(READ)).subscribe(() => {});
  const live = new HistogramLive(client, () => clock);
  live.follow(READ);
  expect(await live.update(batch)).toEqual({ afterMs: 500 });
  answer(Response.json({ start: iso(T0), end: iso(T0), bucket_seconds: 30, counts: [] }));
});
