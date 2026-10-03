import { type QueryClient, QueryObserver } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import type { InquiryRow } from "../api/inquiries";
import { stubFetch } from "../api/testing";
import type { Filter } from "../query/query";
import { ListLive } from "./list";
import { idFilter } from "./rows";
import { issue, listParams, mount, serveRows, testClient, uuid } from "./testing";

const ACTIVE: readonly Filter[] = [{ field: "status", op: "is", value: "active" }];
const REQUEST = { kinds: ["Issue"], filters: ACTIVE };

let client: QueryClient;
let server: InquiryRow[];
let clock: number;

beforeEach(() => {
  client = testClient();
  server = [];
  clock = 10_000;
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

/** A list over `REQUEST` holding `pages` of rows (pages of `pageSize`), with `shown` on screen. */
function list(pages: InquiryRow[][], { shown = [] as number[], pageSize = 50 } = {}) {
  pages.forEach((rows, page) => mount(client, pageKey(pageSize, page), rows));
  const live = new ListLive(client, REQUEST, pageSize, () => clock);
  const onScreen = new Set(shown.map(uuid));
  live.watch({ onScreen: (ids) => new Set([...ids].filter((id) => onScreen.has(id))) });
  return { live, onScreen };
}

function pageKey(pageSize: number, page: number) {
  return ["inquiries", "list", ACTIVE, "Issue", pageSize, page * pageSize];
}

/** A cached page's rows, by seq and title, as the server sent them and live updates changed them in place. */
function cached(pageSize = 50, page = 0): string[] {
  return (client.getQueryData<InquiryRow[]>(pageKey(pageSize, page)) ?? []).map((row) => `${row.seq} ${row.title}`);
}

/** What the list shows: its loaded pages, as `ListLive.rows` makes them, by seq and title. */
function shown(live: ListLive, pageSize = 50): string[] {
  const loaded = client
    .getQueryCache()
    .findAll({ queryKey: ["inquiries", "list", ACTIVE, "Issue", pageSize] })
    .toSorted((a, b) => Number(a.queryKey[5]) - Number(b.queryKey[5]))
    .flatMap((query) => (query.state.data as InquiryRow[] | undefined) ?? []);
  return live.rows(loaded).map((row) => `${row.seq} ${row.title}`);
}

const batch = (seqs: number[], gap = false) => ({ ids: new Set(seqs.map(uuid)), gap });
const rows = (...seqs: number[]) => seqs.map((seq) => issue(seq));

test("held rows on screen refetch with the list's kinds and filters plus a seq_range of just those rows", async () => {
  server = rows(5, 4, 3, 2, 1);
  const { live } = list([rows(5, 4, 3, 2, 1)], { shown: [5, 4, 3] });
  server[1] = issue(4, { title: "Renamed" });
  server[2] = issue(3, { title: "Also renamed" });
  const sent = serveRows(() => server);
  await live.update(batch([4, 3, 2]));
  expect(sent.map(listParams)).toEqual([
    { kinds: ["Issue"], filters: ACTIVE, seqRanges: ["3..4"], limit: 2, offset: null },
  ]);
  // Updated in place: same rows, same order.
  expect(cached()).toEqual(["5 Issue 5", "4 Renamed", "3 Also renamed", "2 Issue 2", "1 Issue 1"]);
});

test("a held row that does not come back no longer matches: it stays, marked left, until merged away", async () => {
  server = rows(3, 2, 1);
  const { live } = list([rows(3, 2, 1)], { shown: [3, 2, 1] });
  server[1] = issue(2, { status: "complete" });
  serveRows(() => server);
  await live.update(batch([2]));
  expect(shown(live)).toEqual(["3 Issue 3", "2 Issue 2", "1 Issue 1"]);
  expect([...live.getSnapshot().left]).toEqual([uuid(2)]);
  live.merge(true);
  expect(shown(live)).toEqual(["3 Issue 3", "1 Issue 1"]);
  expect(live.getSnapshot().left.size).toBe(0);
});

test("held rows off screen are marked stale, and refetch once they come on screen", async () => {
  server = rows(3, 2, 1);
  const { live, onScreen } = list([rows(3, 2, 1)], { shown: [3] });
  server[2] = issue(1, { title: "Changed off screen" });
  const sent = serveRows(() => server);
  await live.update(batch([1]));
  expect(sent).toEqual([]);
  expect(live.isStale(uuid(1))).toBe(true);
  onScreen.add(uuid(1));
  await live.update(batch([1]));
  expect(sent.map((s) => listParams(s).seqRanges)).toEqual([["1..1"]]);
  expect(cached()).toContain("1 Changed off screen");
  expect(live.isStale(uuid(1))).toBe(false);
});

test("a stale row on screen refetches with the next batch, whatever it holds", async () => {
  server = rows(3, 2, 1);
  const { live, onScreen } = list([rows(3, 2, 1)], { shown: [3] });
  serveRows(() => server);
  await live.update(batch([1]));
  onScreen.add(uuid(1));
  const sent = serveRows(() => server);
  await live.update(batch([3]));
  expect(sent.map((s) => listParams(s).seqRanges)).toEqual([["1..1", "3..3"]]);
});

test("ids it does not hold are checked together, 63 at a time, at most one check every 2 s", async () => {
  const { live } = list([rows(1)], { shown: [1] });
  const sent = serveRows(() => server);
  const outsiders = Array.from({ length: 70 }, (_, n) => 100 + n);
  expect(await live.update(batch(outsiders))).toEqual({ afterMs: 2_000 });
  expect(sent.map(listParams)).toEqual([
    {
      kinds: ["Issue"],
      filters: [...ACTIVE, idFilter(outsiders.slice(0, 63).map(uuid))],
      seqRanges: [],
      limit: 126,
      offset: 0,
    },
  ]);
  // A batch a second later waits for the rest of the 2 s, with no request.
  clock += 1_000;
  expect(await live.update(batch([200]))).toEqual({ afterMs: 1_000 });
  expect(sent).toHaveLength(1);
  // The next check takes every id still waiting, oldest first, whichever batch brought it.
  clock += 1_000;
  expect(await live.update(batch([]))).toBeNull();
  expect(listParams(sent[1]!).filters.at(-1)).toEqual(idFilter([...outsiders.slice(63), 200].map(uuid)));
});

test("a new matching row among 62 unrelated ids enters with the first check", async () => {
  const { live } = list([rows(1)], { shown: [1] });
  server = [issue(1), issue(500)];
  serveRows(() => server);
  const unrelated = Array.from({ length: 61 }, (_, n) => 100 + n);
  await live.update(batch([...unrelated, 500]));
  expect(live.getSnapshot().arrivals).toBe(1);
});

test("ids a failed check asked for wait for the next check", async () => {
  const { live } = list([rows(1)], { shown: [1] });
  server = [issue(1), issue(2)];
  stubFetch(() => Response.json({ detail: "busy" }, { status: 503 }));
  await expect(live.update(batch([2]))).rejects.toThrow();
  const sent = serveRows(() => server);
  clock += 2_000;
  await live.update(batch([]));
  expect(sent.map((s) => listParams(s).filters.at(-1))).toEqual([idFilter([uuid(2)])]);
  expect(live.getSnapshot().arrivals).toBe(1);
});

test("a row whose id ends like an asked one's comes back too, and is not taken for it", async () => {
  const { live } = list([rows(1)], { shown: [1] });
  const twin = { ...issue(9), id: `11111111-0000-4000-8000-${uuid(2).slice(-12)}` };
  server = [issue(1), twin];
  serveRows(() => server);
  await live.update(batch([2]));
  expect(live.getSnapshot().arrivals).toBe(0);
});

test("a row a check finds entered the list: it waits as an arrival, then joins in server order", async () => {
  server = rows(5, 3, 1);
  const { live } = list([rows(5, 3, 1)], { shown: [5, 3, 1] });
  server.push(issue(6), issue(4), issue(2, { status: "complete" }));
  serveRows(() => server);
  await live.update(batch([6, 4, 2]));
  expect(live.getSnapshot().arrivals).toBe(2);
  expect(shown(live)).toEqual(["5 Issue 5", "3 Issue 3", "1 Issue 1"]);
  live.merge(false);
  expect(shown(live).map((row) => Number.parseInt(row))).toEqual([6, 5, 4, 3, 1]);
  expect(live.getSnapshot().arrivals).toBe(0);
});

test("joining or merging away rows leaves the cached pages as the server sent them, so a full page still means more", async () => {
  server = rows(9, 8, 7, 6, 5, 4);
  const { live } = list([rows(8, 7, 6)], { shown: [8, 7, 6], pageSize: 3 });
  server[0] = issue(9);
  server[2] = issue(7, { status: "complete" });
  serveRows(() => server);
  await live.update(batch([9, 7]));
  live.merge(true);
  expect(shown(live, 3)).toEqual(["9 Issue 9", "8 Issue 8", "6 Issue 6"]);
  expect(cached(3)).toEqual(["8 Issue 8", "7 Issue 7", "6 Issue 6"]);
});

test("a joined row is held: later changes update it, and it can leave", async () => {
  server = rows(2, 1);
  const { live } = list([rows(1)], { shown: [1, 2] });
  const sent = serveRows(() => server);
  await live.update(batch([2]));
  live.merge(false);
  server[0] = issue(2, { title: "Joined, then renamed" });
  await live.update(batch([2]));
  expect(listParams(sent.at(-1)!).seqRanges).toEqual(["2..2"]);
  expect(shown(live)).toEqual(["2 Joined, then renamed", "1 Issue 1"]);
  server[0] = issue(2, { status: "complete" });
  await live.update(batch([2]));
  expect([...live.getSnapshot().left]).toEqual([uuid(2)]);
  live.merge(true);
  expect(shown(live)).toEqual(["1 Issue 1"]);
});

test("a list holds only its own pages, not another view's with the same filters and another page size", async () => {
  server = [issue(1, { title: "Renamed" })];
  mount(client, pageKey(20, 0), rows(1));
  const { live } = list([rows(1)], { shown: [1] });
  serveRows(() => server);
  await live.update(batch([1]));
  expect(cached(50)).toEqual(["1 Renamed"]);
  expect(cached(20)).toEqual(["1 Issue 1"]);
});

test("a row that entered beyond the loaded rows is left for Load more, not counted", async () => {
  server = rows(9, 8, 7, 6);
  const { live } = list([rows(9, 8, 7)], { pageSize: 3 });
  server.push(issue(2));
  serveRows(() => server);
  await live.update(batch([2]));
  expect(live.getSnapshot().arrivals).toBe(0);
});

test("an arrival that stops matching before it is merged is no longer counted", async () => {
  const { live } = list([rows(1)]);
  server = [issue(2)];
  serveRows(() => server);
  await live.update(batch([2]));
  expect(live.getSnapshot().arrivals).toBe(1);
  server = [issue(2, { status: "complete" })];
  clock += 2_000;
  await live.update(batch([2]));
  expect(live.getSnapshot().arrivals).toBe(0);
});

test("an empty filtered list takes a new matching row", async () => {
  const { live } = list([[]]);
  server = [issue(1)];
  serveRows(() => server);
  await live.update(batch([1]));
  live.merge(false);
  expect(shown(live)).toEqual(["1 Issue 1"]);
});

test("over 100 ids it does not hold, the list reads every row its pages span instead of checking them", async () => {
  server = rows(2, 1);
  const { live } = list([rows(2, 1)]);
  const outsiders = Array.from({ length: 101 }, (_, n) => 100 + n);
  server.push(issue(150));
  const sent = serveRows(() => server);
  expect(await live.update(batch(outsiders))).toBeNull();
  // Its one page is short, so it holds every row there is: the read takes them all.
  expect(sent.map(listParams)).toEqual([{ kinds: ["Issue"], filters: ACTIVE, seqRanges: [], limit: 1_000, offset: 0 }]);
  expect(live.getSnapshot().arrivals).toBe(1);
});

test("gap recovery reads every row the pages span: held rows update in place, on screen or not, and new ones arrive", async () => {
  // Two pages of two; rows 4 and 3 on page one, 2 and 1 on page two.
  server = rows(5, 4, 3, 2, 1);
  server[3] = issue(2, { title: "Changed in the gap" });
  server[4] = issue(1, { title: "Also changed" });
  const { live } = list([rows(4, 3), rows(2, 1)], { pageSize: 2, shown: [3, 2] });
  const sent = serveRows(() => server);
  await live.update({ ids: new Set(), gap: true });
  // From the newest down to row 1, the oldest loaded; 5 was created in the gap.
  expect(sent.map(listParams)).toEqual([
    {
      kinds: ["Issue"],
      filters: [...ACTIVE, { field: "created", op: "ge", value: "2026-09-20 00:01:00+00:00" }],
      seqRanges: [],
      limit: 1_000,
      offset: 0,
    },
  ]);
  expect(live.getSnapshot().arrivals).toBe(1);
  expect(cached(2, 1)).toEqual(["2 Changed in the gap", "1 Also changed"]);
  expect(live.isStale(uuid(1))).toBe(false);
});

test("gap recovery drops an arrival that stopped matching while the stream was down", async () => {
  server = [issue(2), issue(1)];
  const { live } = list([rows(1)], { shown: [1] });
  serveRows(() => server);
  await live.update(batch([2]));
  expect(live.getSnapshot().arrivals).toBe(1);
  server = [issue(2, { status: "complete" }), issue(1)];
  expect(await live.update({ ids: new Set(), gap: true })).toBeNull();
  expect(live.getSnapshot().arrivals).toBe(0);
});

test("a gap recovery answer that comes back full may not span the pages: held rows it left out count as changed, arrivals are checked again", async () => {
  const newer = Array.from({ length: 1_000 }, (_, n) => issue(1_000 + n));
  server = [issue(3), issue(2), issue(1)];
  const { live } = list([rows(2, 1)], { shown: [2, 1] });
  serveRows(() => server);
  await live.update(batch([3]));
  server = [...newer, issue(3), issue(2, { title: "Changed in the gap" }), issue(1)];
  const sent = serveRows(() => server);
  expect(await live.update({ ids: new Set(), gap: true })).toEqual({ afterMs: 2_000 });
  expect(sent.map((s) => listParams(s).seqRanges)).toEqual([[], ["1..2"]]);
  expect(cached()).toEqual(["2 Changed in the gap", "1 Issue 1"]);
  clock += 2_000;
  await live.update(batch([]));
  expect(listParams(sent.at(-1)!).filters.at(-1)).toEqual(idFilter([uuid(3)]));
});

test("a batch that comes while the first page is being read waits for it", async () => {
  const answer = loading(50, 0);
  const live = new ListLive(client, REQUEST, 50, () => clock);
  live.watch({ onScreen: (ids) => new Set(ids) });
  const sent = serveRows(() => server);
  expect(await live.update(batch([1]))).toEqual({ afterMs: 500 });
  expect(sent).toEqual([]);
  // The page's read may predate the change, so the row refetches once it has loaded.
  answer(rows(1));
  await tick();
  server = [issue(1, { title: "Changed while loading" })];
  expect(await live.update(batch([]))).toBeNull();
  expect(cached()).toEqual(["1 Changed while loading"]);
});

test("a list with no pages asks for nothing", async () => {
  const live = new ListLive(client, REQUEST, 50, () => clock);
  const sent = serveRows(() => server);
  expect(await live.update(batch([1], true))).toBeNull();
  expect(sent).toEqual([]);
});

test("a change to the list's state reaches its subscribers once per change", async () => {
  server = rows(1);
  const { live } = list([rows(1)], { shown: [1] });
  const heard = vi.fn();
  live.subscribe(heard);
  server = [issue(1, { status: "complete" }), issue(2)];
  serveRows(() => server);
  await live.update(batch([1]));
  expect(heard).toHaveBeenCalledTimes(1);
  await live.update(batch([1]));
  expect(heard).toHaveBeenCalledTimes(1);
  clock += 2_000;
  await live.update(batch([2]));
  expect(heard).toHaveBeenCalledTimes(2);
  expect(live.getSnapshot()).toMatchObject({ arrivals: 1, left: new Set([uuid(1)]) });
});

/** A page of `pageSize` rows at place `page` whose read is under way; call the result to answer it. */
function loading(pageSize: number, page: number): (rows: InquiryRow[]) => void {
  let answer: (rows: InquiryRow[]) => void = () => {};
  new QueryObserver(client, {
    queryKey: pageKey(pageSize, page),
    queryFn: () => new Promise<InquiryRow[]>((resolve) => (answer = resolve)),
  }).subscribe(() => {});
  return (rows) => answer(rows);
}

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

test("over 100 ids it does not hold, a row entering a later loaded page still arrives (CR-LIVE-06)", async () => {
  server = rows(12, 11, 10, 9, 8, 7, 6);
  const { live } = list([rows(12, 11, 10), rows(9, 8, 6)], { pageSize: 3 });
  const outsiders = Array.from({ length: 101 }, (_, n) => 100 + n);
  const sent = serveRows(() => server);
  await live.update(batch([...outsiders, 7]));
  expect(live.getSnapshot().arrivals).toBe(1);
  expect(sent.map(listParams)[0]).toEqual({
    kinds: ["Issue"],
    filters: [...ACTIVE, { field: "created", op: "ge", value: "2026-09-20 00:06:00+00:00" }],
    seqRanges: [],
    limit: 1_000,
    offset: 0,
  });
});

test("gap recovery finds a row that entered a later loaded page while the stream was down (CR-LIVE-D2)", async () => {
  server = rows(12, 11, 10, 9, 8, 7, 6);
  const { live } = list([rows(12, 11, 10), rows(9, 8, 6)], { pageSize: 3 });
  serveRows(() => server);
  await live.update({ ids: new Set(), gap: true });
  expect(live.getSnapshot().arrivals).toBe(1);
  live.merge(false);
  expect(shown(live, 3).map((row) => Number.parseInt(row))).toEqual([12, 11, 10, 9, 8, 7, 6]);
});

test("a change for a page still loading waits for that page, as its read may predate it (CR-LIVE-R2-01)", async () => {
  mount(client, pageKey(3, 0), rows(12, 11, 10));
  const answer = loading(3, 1);
  const live = new ListLive(client, REQUEST, 3, () => clock);
  live.watch({ onScreen: (ids) => new Set(ids) });
  // Row 7 entered the list; the page it belongs on is still being read, from before.
  server = rows(12, 11, 10, 9, 8, 7);
  const sent = serveRows(() => server);
  expect(await live.update(batch([7]))).toEqual({ afterMs: 500 });
  expect(sent).toEqual([]);
  answer(rows(9, 8, 6));
  await tick();
  await live.update(batch([]));
  expect(live.getSnapshot().arrivals).toBe(1);
});

test("a row merged away that matches again comes back, even as the last row of a full page (CR-LIVE-R2-D1)", async () => {
  server = rows(3, 2, 1);
  const { live } = list([rows(3, 2, 1)], { pageSize: 3, shown: [3, 2, 1] });
  serveRows(() => server);
  server[2] = issue(1, { status: "complete" });
  await live.update(batch([1]));
  live.merge(true);
  expect(shown(live, 3)).toEqual(["3 Issue 3", "2 Issue 2"]);
  server[2] = issue(1);
  clock += 2_000;
  await live.update(batch([1]));
  expect(live.getSnapshot().arrivals).toBe(1);
  live.merge(false);
  expect(shown(live, 3)).toEqual(["3 Issue 3", "2 Issue 2", "1 Issue 1"]);
});

test("a row whose id ends like others' still enters when they fill a check's answer (CR-LIVE-R5-B1)", async () => {
  const { live } = list([rows(1)], { shown: [1] });
  const twins = (count: number) =>
    Array.from({ length: count }, (_, n) => ({ ...issue(1_000 + n), id: `${String(n + 1).padStart(8, "1")}-0000-4000-8000-${uuid(500).slice(-12)}` }));
  // One asked id is named whole: two newer rows sharing its last 12 digits cannot crowd it out of an answer with room for two.
  server = [issue(1), issue(500), ...twins(2)];
  serveRows(() => server);
  await live.update(batch([500]));
  expect(live.getSnapshot().arrivals).toBe(1);
  // 63 asked ids, named by their last seven digits: 126 rows ending like one of them fill the answer.
  const { live: busy } = list([rows(1)], { shown: [1] });
  server = [issue(1), issue(500), ...twins(126)];
  const sent = serveRows(() => server);
  await busy.update(batch([500, ...Array.from({ length: 62 }, (_, n) => 100 + n)]));
  clock += 2_000;
  await busy.update(batch([]));
  // Every id the full answer left out is checked again, whole, 13 at a time.
  expect(listParams(sent[1]!).filters.at(-1)).toEqual(idFilter([500, ...Array.from({ length: 12 }, (_, n) => 100 + n)].map(uuid)));
  expect(busy.getSnapshot().arrivals).toBe(1);
});

test("a row merged in and then away leaves nothing behind (CR-LIVE-R2-02)", async () => {
  server = rows(3, 2, 1);
  const { live } = list([rows(2, 1)], { shown: [3, 2, 1] });
  serveRows(() => server);
  await live.update(batch([3]));
  live.merge(false);
  server[0] = issue(3, { status: "complete" });
  await live.update(batch([3]));
  live.merge(true);
  expect(shown(live)).toEqual(["2 Issue 2", "1 Issue 1"]);
  expect(live.getSnapshot()).toMatchObject({ joined: new Map(), gone: new Set() });
});

test("more new rows than a page: the pill counts them all, and merging reads the pages afresh instead of holding them (CR-LIVE-R4-B2)", async () => {
  server = rows(16, 15, 14, 13, 12, 11, 10, 9, 8, 7);
  const { live } = list([rows(9, 8, 7)], { pageSize: 3 });
  serveRows(() => server);
  await live.update(batch([16, 15, 14, 13, 12, 11, 10]));
  expect(live.getSnapshot().arrivals).toBe(7);
  expect(live.merge(true)).toEqual([]);
  await tick();
  expect(live.getSnapshot()).toMatchObject({ arrivals: 0, joined: new Map() });
  expect(client.getQueryState(pageKey(3, 0))!.isInvalidated).toBe(true);
});

test("an arrival a page read has since brought in is no longer counted (CR-LIVE-R9-A1)", async () => {
  server = rows(2, 1);
  const { live } = list([rows(1)]);
  serveRows(() => server);
  await live.update(batch([2]));
  expect(live.getSnapshot().arrivals).toBe(1);
  client.setQueryData(pageKey(50, 0), rows(2, 1));
  live.settle(rows(2, 1));
  expect(live.getSnapshot().arrivals).toBe(0);
});

test("a page read under way when a row is refreshed does not put the older row back (CR-LIVE-R8-B3)", async () => {
  client.setQueryData(pageKey(50, 0), rows(2, 1));
  let answer: (rows: InquiryRow[]) => void = () => {};
  new QueryObserver(client, {
    queryKey: pageKey(50, 0),
    queryFn: () => new Promise<InquiryRow[]>((resolve) => (answer = resolve)),
    staleTime: Infinity,
  }).subscribe(() => {});
  const live = new ListLive(client, REQUEST, 50, () => clock);
  live.watch({ onScreen: (ids) => new Set(ids) });
  vi.useFakeTimers();
  // A read of the page starts, then the row changes and its live refresh runs.
  void client.refetchQueries({ queryKey: pageKey(50, 0) });
  server = [issue(2), issue(1, { title: "Renamed", modified: "2026-09-21T00:00:00+00:00" })];
  serveRows(() => server);
  const update = live.update(batch([1]));
  // The refresh has taken its answer; the page's older read answers only now.
  await vi.advanceTimersByTimeAsync(20);
  answer(rows(2, 1));
  await vi.advanceTimersByTimeAsync(20);
  await update;
  expect(cached()).toEqual(["2 Issue 2", "1 Renamed"]);
});

test("once the tab hides, an update starts no further read (CR-LIVE-R3-02)", async () => {
  server = rows(3, 2, 1);
  const { live } = list([rows(3, 2, 1)], { shown: [3, 2, 1] });
  const sent = serveRows(() => server);
  await live.update(batch([3, 100]), () => sent.length === 0);
  expect(sent.map((s) => listParams(s).seqRanges)).toEqual([["3..3"]]);
});
