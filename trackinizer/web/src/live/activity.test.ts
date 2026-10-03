import { type QueryClient, QueryObserver } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import type { ChangeKind, LoggedChange } from "../api/changes";
import { activityQueries } from "../activity/queries";
import { ActivityLive } from "./activity";
import { change, mount, serveChanges, testClient } from "./testing";

let client: QueryClient;
let server: LoggedChange[];
let clock: number;

beforeEach(() => {
  client = testClient();
  server = [];
  clock = 10_000;
});

afterEach(() => {
  vi.unstubAllGlobals();
});

/** A feed of `kinds`, one stream, whose first page shows `shown` (newest first), with an empty head. */
function feed(kinds: readonly ChangeKind[], shown: readonly LoggedChange[]) {
  const page = activityQueries.page(kinds, null);
  client.setQueryData(page.queryKey, [...shown]);
  // The feed's own observer, so a reload fetches the page as the view would.
  new QueryObserver(client, { ...page, staleTime: Infinity }).subscribe(() => {});
  mount(client, activityQueries.head(kinds).queryKey, []);
  return new ActivityLive(client, kinds, () => clock);
}

const idsOf = (rows: readonly LoggedChange[] | undefined) => rows?.map((row) => row.id);
const head = (kinds: readonly ChangeKind[]) => client.getQueryData(activityQueries.head(kinds).queryKey);
const firstPage = (kinds: readonly ChangeKind[]) => client.getQueryData(activityQueries.page(kinds, null).queryKey);
const batch = { ids: new Set(["any"]), gap: false };
const CREATED = ["created"] as const;

test("a tab asks once for all its kinds' changes since the newest it shows; new ones join the top, ahead of the first page (PA2)", async () => {
  const kinds = ["created", "status"] as const;
  const shown = [change(3), change(2, { kind: "status" }), change(1)];
  const live = feed(kinds, shown);
  server = [...shown, change(5), change(4), change(6, { kind: "status" }), change(7, { kind: "title" })];
  const sent = serveChanges(() => server);
  expect(await live.update(batch)).toBeNull();
  expect(sent.map((s) => [...new URLSearchParams(s.query)])).toEqual([
    [
      ["kind", "created"],
      ["kind", "status"],
      ["since", change(3).created],
      ["limit", "50"],
      ["brief", "true"],
    ],
  ]);
  expect(idsOf(head(kinds))).toEqual(idsOf([change(6), change(5), change(4)]));
  expect(idsOf(firstPage(kinds))).toEqual(idsOf(shown));
});

test("at most one ask every 2 s: a batch sooner waits for the rest, then asks from the newest shown", async () => {
  const live = feed(["created"], [change(1)]);
  server = [change(1), change(2)];
  const sent = serveChanges(() => server);
  await live.update(batch);
  clock += 500;
  server.push(change(3));
  expect(await live.update(batch)).toEqual({ afterMs: 1_500 });
  expect(sent).toHaveLength(1);
  clock += 1_500;
  expect(await live.update({ ids: new Set(), gap: false })).toBeNull();
  expect(new URLSearchParams(sent[1]!.query).get("since")).toBe(change(2).created);
  expect(idsOf(head(CREATED))).toEqual(idsOf([change(3), change(2)]));
});

test("a tab showing no changes yet takes the newest there are", async () => {
  const live = feed(["status"], []);
  server = [change(1, { kind: "status" })];
  const sent = serveChanges(() => server);
  await live.update(batch);
  expect(new URLSearchParams(sent[0]!.query).has("since")).toBe(false);
  expect(idsOf(head(["status"]))).toEqual(idsOf([change(1, { kind: "status" })]));
});

test("a full answer may have skipped changes, so the tab reloads its first page and starts a new head", async () => {
  const live = feed(["created"], [change(1)]);
  server = Array.from({ length: 60 }, (_, n) => change(n + 1));
  serveChanges(() => server);
  await live.update(batch);
  expect(firstPage(CREATED)).toHaveLength(50);
  expect(firstPage(CREATED)![0]!.id).toBe(change(60).id);
  expect(head(CREATED)).toEqual([]);
});

test("a gap reloads the tab's first page and empties its head", async () => {
  const kinds = ["created", "status"] as const;
  const live = feed(kinds, [change(1)]);
  server = [change(1)];
  serveChanges(() => server);
  await live.update(batch);
  server.push(change(2), change(3, { kind: "status" }));
  clock += 60_000;
  expect(await live.update({ ids: new Set(), gap: true })).toBeNull();
  expect(idsOf(firstPage(kinds))).toEqual(idsOf([change(3, { kind: "status" }), change(2), change(1)]));
  expect(head(kinds)).toEqual([]);
});

test("a batch that comes while a first page is being read waits for it, then asks", async () => {
  let answer: (rows: LoggedChange[]) => void = () => {};
  new QueryObserver(client, {
    queryKey: activityQueries.page(CREATED, null).queryKey,
    queryFn: () => new Promise<LoggedChange[]>((resolve) => (answer = resolve)),
  }).subscribe(() => {});
  mount(client, activityQueries.head(CREATED).queryKey, []);
  const live = new ActivityLive(client, ["created"], () => clock);
  const sent = serveChanges(() => server);
  // The page's read may predate the change.
  expect(await live.update(batch)).toEqual({ afterMs: 500 });
  expect(sent).toEqual([]);
  answer([change(1)]);
  await new Promise((resolve) => setTimeout(resolve, 0));
  server = [change(1), change(2)];
  expect(await live.update({ ids: new Set(), gap: false })).toBeNull();
  expect(idsOf(head(CREATED))).toEqual(idsOf([change(2)]));
});

test("the live head never holds more than a page: past that, the tab reads its first page afresh (CR-LIVE-01)", async () => {
  const live = feed(["created"], [change(1)]);
  server = [change(1)];
  serveChanges(() => server);
  for (let n = 2; n <= 101; n++) {
    server.push(change(n));
    clock += 2_000;
    await live.update(batch);
  }
  expect(head(CREATED)!.length).toBeLessThanOrEqual(50);
  expect([...head(CREATED)!, ...firstPage(CREATED)!][0]!.id).toBe(change(101).id);
});

test("the fake change log orders and filters as the server does, to the microsecond and then by id (CR-LIVE-R9-B1)", async () => {
  const at = (micros: string, id: number) => change(id, { created: `2026-09-20T00:00:00.000${micros}+00:00` });
  serveChanges(() => [at("100", 1), at("900", 2), at("900", 3), at("500", 4)]);
  const read = async (since: string) =>
    ((await (await fetch(new Request(`http://t/api/change_log?kind=created&since=${encodeURIComponent(since)}&limit=50`))).json()) as LoggedChange[]).map(
      (row) => row.id,
    );
  expect(await read("2026-09-20T00:00:00.000500+00:00")).toEqual([change(3).id, change(2).id, change(4).id]);
  expect(await read("2026-09-20T00:00:00.000900+00:00")).toEqual([change(3).id, change(2).id]);
});
