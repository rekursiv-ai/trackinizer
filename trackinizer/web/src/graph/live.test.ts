import { type QueryClient, QueryObserver } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import type { Graph } from "../api/graph";
import { type Sent, stubFetch } from "../api/testing";
import { testClient } from "../live/testing";
import { GraphLive, graphQuery } from "./live";
import { node } from "./testing";

let client: QueryClient;
let server: Graph;
let sent: Sent[];

beforeEach(() => {
  // TanStack stamps each answer with `Date.now()`, which the interval counts from.
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(10_000);
  client = testClient();
  server = { nodes: [node(1)], edges: [] };
  sent = stubFetch(() => Response.json(server));
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

/** The graph of `limit` nodes as the view holds it: read now, with its observer mounted. */
async function shown(limit: number): Promise<GraphLive> {
  new QueryObserver(client, graphQuery(limit)).subscribe(() => {});
  await vi.waitFor(() => expect(client.getQueryData(graphQuery(limit).queryKey)).toEqual(server), { interval: 1 });
  // The wait moves the clock it holds on; now is when the answer came.
  vi.setSystemTime(client.getQueryState(graphQuery(limit).queryKey)!.dataUpdatedAt);
  sent.length = 0;
  return new GraphLive(client, limit);
}

const reads = () => sent.map((request) => `${request.path}${request.query}`);
const batch = { ids: new Set(["any"]), gap: false };
const later = (ms: number) => vi.setSystemTime(Date.now() + ms);

test("a batch 2 s after the last read reads the graph again at once; one sooner waits for the rest, then reads", async () => {
  const live = await shown(1000);
  server = { nodes: [node(1), node(2)], edges: [] };
  later(2_000);
  expect(await live.update(batch)).toBeNull();
  expect(reads()).toEqual(["/api/web/graph?limit=1000"]);
  expect(client.getQueryData(graphQuery(1000).queryKey)).toEqual(server);
  later(500);
  expect(await live.update(batch)).toEqual({ afterMs: 1_500 });
  expect(reads()).toHaveLength(1);
  later(1_500);
  // The run put off comes back with whatever ids arrived since, here none.
  expect(await live.update({ ids: new Set(), gap: false })).toBeNull();
  expect(reads()).toHaveLength(2);
});

test("the view's own first read counts: a batch just after it waits for the rest of the interval", async () => {
  const live = await shown(1000);
  later(1);
  expect(await live.update(batch)).toEqual({ afterMs: 1_999 });
  expect(reads()).toEqual([]);
});

test("a bigger graph reads less often: 2 ms a node it holds, at least 2 s, so 10 s at 5,000 nodes and 40 s at 20,000", async () => {
  const live = await shown(50_000);
  const { queryKey } = graphQuery(50_000);
  /** Hold an answer of `count` nodes, read when the last one was; the interval reads only how many. */
  const holding = (count: number) =>
    client.setQueryData(queryKey, { nodes: Array(count).fill(node(1)), edges: [] }, { updatedAt: client.getQueryState(queryKey)!.dataUpdatedAt });
  holding(5_000);
  later(2_000);
  expect(await live.update(batch)).toEqual({ afterMs: 8_000 });
  holding(20_000);
  expect(await live.update(batch)).toEqual({ afterMs: 38_000 });
  holding(300);
  expect(await live.update(batch)).toBeNull();
  expect(reads()).toEqual(["/api/web/graph?limit=50000"]);
});

test("a gap reads at once, inside the interval too", async () => {
  const live = await shown(1000);
  later(100);
  expect(await live.update({ ids: new Set(), gap: true })).toBeNull();
  expect(reads()).toHaveLength(1);
});

test("a read that fails rejects, so the stream layer tries again", async () => {
  const live = await shown(100);
  stubFetch(() => Response.json({ detail: "down" }, { status: 503 }));
  await expect(live.update({ ids: new Set(), gap: true })).rejects.toThrow("down");
});
