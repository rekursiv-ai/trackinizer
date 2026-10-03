import { QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import type { Histogram } from "../api/histogram";
import type { FeedFilters } from "../api/sessions";
import { type Sent, stubFetch } from "../api/testing";
import { dateTime } from "../detail/time";
import { LiveContext } from "../live";
import { LiveHub } from "../live/hub";
import { testClient } from "../live/testing";
import { Minimap, type MinimapProps } from "./Minimap";

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;
const NOW = Date.UTC(2026, 9, 2, 21, 15, 7);
/** The 7-day mark: the band reaches back no further, as the server's histogram counts no further. */
const MARK = NOW - 7 * DAY;
/** 100 bars: an hour in 61 one-minute buckets, aligned, from 20:15 to 21:16. */
const WIDTH = 600;
const iso = (time: number) => new Date(time).toISOString();
const minute = (time: number) => Math.floor(time / MINUTE) * MINUTE;

let sent: Sent[];
/** The server's records: how many at each time. */
let counts: Map<number, number>;
/** The band's width, in px. */
let width: number;
/** While set, the server holds its answers to reads of a span until it settles. */
let hold: Promise<void> | null;
const FILTERS: FeedFilters = { actor: ["codex-a", "codex-b"] };

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"], now: NOW });
  vi.stubGlobal(
    "ResizeObserver",
    class {
      readonly #report: (entries: readonly { contentRect: { width: number } }[]) => void;
      constructor(report: (entries: readonly { contentRect: { width: number } }[]) => void) {
        this.#report = report;
      }
      observe() {
        this.#report([{ contentRect: { width } }]);
      }
      disconnect() {}
    },
  );
  Element.prototype.setPointerCapture = () => {};
  counts = new Map([
    [NOW - 60 * MINUTE, 2],
    [NOW - 30 * MINUTE, 4],
    [NOW, 1],
  ].map(([time, count]) => [minute(time!), count!]));
  width = WIDTH;
  hold = null;
  // The server lists every bucket of its grid, counting the records in each. A
  // window's grid holds its end: a read of n aligned buckets and the one its end
  // opens. As the server does, it refuses under 2 buckets or over 1,000, and an
  // end before the 7-day mark.
  sent = stubFetch(async (request) => {
    const query = new URL(request.url).searchParams;
    const [since, until, buckets] = [Date.parse(query.get("since")!), Date.parse(query.get("until")!), Number(query.get("buckets"))];
    if (buckets < 2 || buckets > 1000 || until < MARK) return Response.json({ detail: "Out of bounds." }, { status: 400 });
    const step = (until - since) / (buckets - 1);
    const starts = Array.from({ length: buckets }, (_, k) => since + k * step);
    const count = (start: number) => [...counts].filter(([time]) => time >= start && time < start + step).reduce((sum, [, n]) => sum + n, 0);
    if (hold) await hold;
    return Response.json({
      start: iso(since),
      end: iso(starts.at(-1)! + step),
      bucket_seconds: step / 1000,
      counts: starts.map((start) => ({ start: iso(start), count: count(start) })),
    } satisfies Histogram);
  });
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  Reflect.deleteProperty(Element.prototype, "setPointerCapture");
});

function show({ hub = null, ...props }: Partial<MinimapProps> & { hub?: LiveHub | null } = {}) {
  const client = testClient();
  const handlers = { onSeek: vi.fn<(time: string) => void>(), onWindow: vi.fn<(since: string, until: string) => void>() };
  render(
    <QueryClientProvider client={client}>
      <LiveContext value={hub}>
        <Minimap filters={FILTERS} shown={null} {...handlers} {...props} />
      </LiveContext>
    </QueryClientProvider>,
  );
  return { ...handlers, client };
}

/** The band's reads, as their parameters. */
const bandReads = () => sent.map((request) => [...new URLSearchParams(request.query)]);
/** The read of `step` buckets from `since` to `until`, and the one `until` opens. */
const spanRead = (since: number, until: number, step: number) => [
  ["actor", "codex-a"],
  ["actor", "codex-b"],
  ["since", iso(since)],
  ["until", iso(until)],
  ["buckets", String((until - since) / step + 1)],
];
const LAST_HOUR = spanRead(minute(NOW - HOUR), minute(NOW) + MINUTE, MINUTE);
const bars = () => [...document.querySelectorAll<SVGRectElement>(".minimap-bar")];
const band = () => document.querySelector<HTMLElement>(".minimap-band")!;
const track = () => screen.getByRole("slider", { name: "Place in history" });

/** Run what the band's settling waits on: it reads a span once the view has stayed put. */
async function settled(act_: () => void) {
  vi.useFakeTimers({ toFake: ["Date", "setTimeout", "clearTimeout"], now: Date.now(), shouldClearNativeTimers: true });
  act_();
  await act(() => vi.advanceTimersByTimeAsync(500));
  vi.useFakeTimers({ toFake: ["Date"], now: Date.now() });
}

test("the band reads the last hour under the feed's filters, in bars that fit its width, one drawn per bucket with records", async () => {
  show();
  await waitFor(() => expect(bars()).toHaveLength(3));
  // Only that: the band's history is the last 7 days, whatever the records.
  expect(bandReads()).toEqual([LAST_HOUR]);
  expect(bars().map((bar) => Number(bar.getAttribute("height")))).toEqual([22, 44, 11]);
  // Each bar sits where its minute falls along the hour.
  expect(Number(bars()[1]!.getAttribute("x"))).toBeCloseTo(((minute(NOW - 30 * MINUTE) - (NOW - HOUR)) / HOUR) * WIDTH);
});

test("a click moves the feed to the time under it; a drag shows the window dragged across, whichever way", async () => {
  const { onSeek, onWindow } = show();
  await waitFor(() => expect(bars()).toHaveLength(3));
  fireEvent.pointerDown(band(), { clientX: 300, button: 0, pointerId: 1 });
  fireEvent.pointerMove(band(), { clientX: 302, pointerId: 1 });
  fireEvent.pointerUp(band(), { clientX: 302, pointerId: 1 });
  expect(onSeek.mock.calls).toEqual([[iso(NOW - HOUR + (302 / WIDTH) * HOUR)]]);
  fireEvent.pointerDown(band(), { clientX: 450, button: 0, pointerId: 1 });
  fireEvent.pointerMove(band(), { clientX: 150, pointerId: 1 });
  expect(document.querySelector<HTMLElement>(".minimap-pick")!.style).toMatchObject({ left: "25%", width: "50%" });
  fireEvent.pointerUp(band(), { clientX: 150, pointerId: 1 });
  expect(onWindow.mock.calls).toEqual([[iso(NOW - 45 * MINUTE), iso(NOW - 15 * MINUTE)]]);
  expect(document.querySelector(".minimap-pick")).toBeNull();
  expect(onSeek).toHaveBeenCalledTimes(1);
});

test("the feed's window is marked, the bars in it lit; the zoom buttons, Hour, Day and Week, change the span read", async () => {
  show({ shown: { since: iso(NOW - 30 * MINUTE), until: iso(NOW - 15 * MINUTE) } });
  await waitFor(() => expect(bars()).toHaveLength(3));
  expect(document.querySelector<HTMLElement>(".minimap-mark")!.style).toMatchObject({ left: "50%", width: "25%" });
  expect(bars().map((bar) => bar.classList.contains("on"))).toEqual([false, true, false]);
  expect(within(screen.getByRole("group", { name: "Zoom" })).getAllByRole("button").map((button) => button.textContent)).toEqual(["Hour", "Day", "Week"]);
  const day = screen.getByRole("button", { name: "Day" });
  await settled(() => fireEvent.click(day));
  expect(day.getAttribute("aria-pressed")).toBe("true");
  // 100 bars of a day want 15 min each.
  const quarter = 15 * MINUTE;
  expect(bandReads().at(-1)).toEqual(spanRead(Math.floor((NOW - DAY) / quarter) * quarter, Math.ceil(NOW / quarter) * quarter, quarter));
  // Week, the 7 days, in 2 h bars from the first that starts after the mark.
  const twoHours = 2 * HOUR;
  let release = () => {};
  hold = new Promise((resolve) => (release = resolve));
  await settled(() => fireEvent.click(screen.getByRole("button", { name: "Week" })));
  expect(bandReads().at(-1)).toEqual(spanRead(Math.ceil(MARK / twoHours) * twoHours, Math.ceil(NOW / twoHours) * twoHours, twoHours));
  release();
  // The hour's records fall in one 2 h bar, 20:00 to 22:00.
  await waitFor(() => expect(bars().map((bar) => Number(bar.getAttribute("height")))).toEqual([44]));
});

test("while the next counts load, a scroll keeps the bars shown, but a zoom in stretches no coarser ones across the band", async () => {
  show();
  await waitFor(() => expect(bars()).toHaveLength(3));
  let release = () => {};
  hold = new Promise((resolve) => (release = resolve));
  await settled(() => fireEvent.wheel(band(), { deltaY: -WIDTH / 3 }));
  // Twenty minutes back, the newest bar has left the band; the others stay.
  expect(bars()).toHaveLength(2);
  release();
  hold = new Promise((resolve) => (release = resolve));
  await settled(() => fireEvent.click(screen.getByRole("button", { name: "Week" })));
  release();
  await waitFor(() => expect(bars()).toHaveLength(1));
  hold = new Promise((resolve) => (release = resolve));
  await settled(() => fireEvent.click(screen.getByRole("button", { name: "Day" })));
  expect(bars()).toEqual([]);
  release();
  await waitFor(() => expect(bars()).toHaveLength(3));
});

test("a feed that follows now is marked to now, and zooming leaves a live band live", async () => {
  show({ shown: { since: iso(NOW - 3 * HOUR), until: null } });
  await waitFor(() => expect(bars()).toHaveLength(3));
  expect(document.querySelector<HTMLElement>(".minimap-mark")!.style).toMatchObject({ left: "0%", width: "100%" });
  expect(bars().map((bar) => bar.classList.contains("on"))).toEqual([true, true, true]);
  await settled(() => fireEvent.click(screen.getByRole("button", { name: "Week" })));
  await settled(() => fireEvent.click(screen.getByRole("button", { name: "Hour" })));
  expect(track().getAttribute("aria-valuetext")).toBe("Now");
});

test("the wheel scrolls back through the history, read once it settles; Now returns to live; the band stops at the 7-day mark", async () => {
  show();
  await waitFor(() => expect(bars()).toHaveLength(3));
  expect(screen.queryByRole("button", { name: "Now" })).toBeNull();
  // Three notches of a third of the band each: an hour back, read once. Each is
  // cancelled, or a horizontal swipe would also take the browser back a page.
  const notches: boolean[] = [];
  await settled(() => {
    for (let k = 0; k < 3; k++) notches.push(fireEvent.wheel(band(), { deltaY: -WIDTH / 3 }));
  });
  expect(notches).toEqual([false, false, false]);
  expect(bandReads()).toEqual([LAST_HOUR, spanRead(minute(NOW - 2 * HOUR), minute(NOW - HOUR) + MINUTE, MINUTE)]);
  await settled(() => fireEvent.click(screen.getByRole("button", { name: "Now" })));
  expect(screen.queryByRole("button", { name: "Now" })).toBeNull();
  // The last hour was read already.
  expect(bandReads()).toHaveLength(2);
  // Its read starts at the first bar after the mark, since the server counts nothing before it.
  await settled(() => fireEvent.wheel(band(), { deltaX: -1000 * WIDTH }));
  expect(bandReads().at(-1)).toEqual(spanRead(minute(MARK) + MINUTE, minute(MARK + HOUR) + MINUTE, MINUTE));
});

test("the track scrolls across the 7 days: a press off the thumb centres the band there, the thumb drags it, keys step", async () => {
  show();
  await waitFor(() => expect(bars()).toHaveLength(3));
  expect(track().getAttribute("aria-valuetext")).toBe("Now");
  // 600 px of track hold the 7 days: a px is 16.8 min.
  const middle = MARK + 3.5 * DAY;
  fireEvent.pointerDown(track(), { clientX: 300, button: 0, pointerId: 2 });
  fireEvent.pointerUp(track(), { clientX: 300, pointerId: 2 });
  expect(track().getAttribute("aria-valuetext")).toBe(dateTime(iso(middle + HOUR / 2)));
  // Held 16.8 min left of its middle, the thumb moves as the pointer does, without first jumping to centre there.
  await settled(() => {
    fireEvent.pointerDown(track(), { clientX: 299, button: 0, pointerId: 2 });
    expect(track().getAttribute("aria-valuetext")).toBe(dateTime(iso(middle + HOUR / 2)));
    fireEvent.pointerMove(track(), { clientX: 349, pointerId: 2 });
    fireEvent.pointerUp(track(), { clientX: 349, pointerId: 2 });
  });
  const rest = middle + 14 * HOUR;
  expect(track().getAttribute("aria-valuetext")).toBe(dateTime(iso(rest + HOUR / 2)));
  // Read once, where the band came to rest.
  expect(bandReads()).toEqual([LAST_HOUR, spanRead(minute(rest - HOUR / 2), minute(rest + HOUR / 2) + MINUTE, MINUTE)]);
  await settled(() => fireEvent.keyDown(track(), { key: "End" }));
  expect(track().getAttribute("aria-valuetext")).toBe("Now");
  await settled(() => fireEvent.keyDown(track(), { key: "ArrowLeft" }));
  expect(bandReads().at(-1)).toEqual(spanRead(minute(NOW - HOUR - 15 * MINUTE), minute(NOW - 15 * MINUTE) + MINUTE, MINUTE));
});

test("live, the stream's first open reads the band again; scrolled back, it reads nothing", async () => {
  const hub = new LiveHub(testClient());
  show({ hub });
  await waitFor(() => expect(bars()).toHaveLength(3));
  counts.set(minute(NOW), 5);
  act(() => hub.open());
  await waitFor(() => expect(bars().map((bar) => Number(bar.getAttribute("height")))).toEqual([17.6, 35.2, 44]));
  expect(bandReads()).toEqual([LAST_HOUR, LAST_HOUR]);
  await settled(() => fireEvent.wheel(band(), { deltaY: -WIDTH }));
  hub.drop();
  act(() => hub.open());
  await act(() => Promise.resolve());
  expect(bandReads()).toHaveLength(3);
  hub.stop();
});

test("a band read that fails says why, with Retry", async () => {
  vi.stubGlobal("fetch", async () => Response.json({ detail: "The database is down." }, { status: 503 }));
  show();
  expect((await screen.findByRole("alert")).textContent).toContain("The database is down.");
  expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
});

test("Home takes a narrow band to the 7-day mark; its read starts after the mark and still asks for 2 buckets", async () => {
  // 6 px holds 2 bars: an hour wants 1 h each.
  width = 6;
  show();
  await waitFor(() => expect(bandReads()).toHaveLength(1));
  await settled(() => fireEvent.keyDown(track(), { key: "Home" }));
  expect(track().getAttribute("aria-valuetext")).toBe(dateTime(iso(MARK + HOUR)));
  const hour = (time: number) => Math.ceil(time / HOUR) * HOUR;
  expect(bandReads().at(-1)).toEqual(spanRead(hour(MARK), hour(MARK + HOUR), HOUR));
  expect(screen.queryByRole("alert")).toBeNull();
});
