import { QueryObserver } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { bootQueries } from "../app/boot";
import { LiveHub } from "./hub";
import type { Batch, Later } from "./serial";
import { testClient } from "./testing";
import { AGREED } from "../api/testing";

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

/** A hub with one registered query that records the batches it gets. */
function hubWithQuery() {
  const client = testClient();
  const hub = new LiveHub(client);
  const batches: { ids: string[]; gap: boolean }[] = [];
  hub.register({
    update: async ({ ids, gap }: Batch): Promise<Later | null> => {
      batches.push({ ids: [...ids], gap });
      return null;
    },
  });
  return { client, hub, batches };
}

test("every registered query gets every batch, one second after its first id", async () => {
  const { hub, batches } = hubWithQuery();
  const other: string[][] = [];
  hub.register({ update: async ({ ids }) => (other.push([...ids]), null) });
  hub.change("a");
  hub.change("b");
  hub.change("a");
  await vi.advanceTimersByTimeAsync(999);
  expect(batches).toEqual([]);
  await vi.advanceTimersByTimeAsync(1);
  expect(batches).toEqual([{ ids: ["a", "b"], gap: false }]);
  expect(other).toEqual([["a", "b"]]);
});

test("the first connect recovers the gap, as the stream replays nothing from before it", async () => {
  const { hub, batches } = hubWithQuery();
  hub.open();
  await vi.advanceTimersByTimeAsync(0);
  expect(batches).toEqual([{ ids: [], gap: true }]);
  // A query registered once the stream is open reads after it, and misses nothing.
  const later: boolean[] = [];
  hub.register({ update: async ({ gap }) => (later.push(gap), null) });
  hub.change("a");
  await vi.advanceTimersByTimeAsync(1_000);
  expect(later).toEqual([false]);
});

/** A query that records whether each batch it gets asks for a gap recovery. */
function gaps(): { query: { update: (batch: Batch) => Promise<Later | null> }; gaps: boolean[] } {
  const seen: boolean[] = [];
  return { query: { update: async ({ gap }) => (seen.push(gap), null) }, gaps: seen };
}

test("the first open recovers only queries whose reads started before it, and a late query that holds one", async () => {
  const hub = new LiveHub(testClient());
  const before = gaps();
  hub.register(before.query);
  await vi.advanceTimersByTimeAsync(10);
  hub.open();
  const openedAt = Date.now();
  await vi.advanceTimersByTimeAsync(10);
  const after = gaps();
  hub.register(after.query);
  // Its first page was read before the stream opened, as main.tsx's first reads are.
  const prefetched = gaps();
  hub.register(prefetched.query, { readAt: openedAt - 5 });
  await vi.advanceTimersByTimeAsync(0);
  expect([before.gaps, after.gaps, prefetched.gaps]).toEqual([[true], [], [true]]);
});

test("a stream that opened before the hub heard of it, as main.tsx's does, counts from when it opened", async () => {
  const hub = new LiveHub(testClient());
  const openedAt = Date.now();
  await vi.advanceTimersByTimeAsync(10);
  // Registered after the stream opened, before the hub heard: its read missed nothing.
  const after = gaps();
  hub.register(after.query);
  hub.open(openedAt);
  await vi.advanceTimersByTimeAsync(0);
  expect(after.gaps).toEqual([]);
});

test("a reconnect recovers the gap, with any ids waiting", async () => {
  const { hub, batches } = hubWithQuery();
  hub.open();
  await vi.advanceTimersByTimeAsync(5_000);
  batches.length = 0;
  hub.change("a");
  hub.drop();
  hub.open();
  await vi.advanceTimersByTimeAsync(0);
  expect(batches).toEqual([{ ids: ["a"], gap: true }]);
  // The window "a" opened was closed by the recovery: no second batch.
  await vi.advanceTimersByTimeAsync(2_000);
  expect(batches).toHaveLength(1);
});

test("a hidden tab fetches nothing and only collects; back within 30 s, it catches up once", async () => {
  const { hub, batches } = hubWithQuery();
  hub.hide();
  hub.change("a");
  await vi.advanceTimersByTimeAsync(10_000);
  hub.change("b");
  await vi.advanceTimersByTimeAsync(10_000);
  expect(batches).toEqual([]);
  hub.show();
  await vi.advanceTimersByTimeAsync(0);
  expect(batches).toEqual([{ ids: ["a", "b"], gap: false }]);
});

test("back after more than 30 s hidden, the tab recovers the gap", async () => {
  const { hub, batches } = hubWithQuery();
  hub.hide();
  await vi.advanceTimersByTimeAsync(30_001);
  hub.show();
  await vi.advanceTimersByTimeAsync(0);
  expect(batches).toEqual([{ ids: [], gap: true }]);
});

test("a reconnect while hidden waits for the tab to come back", async () => {
  const { hub, batches } = hubWithQuery();
  hub.open();
  await vi.advanceTimersByTimeAsync(0);
  batches.length = 0;
  hub.hide();
  hub.drop();
  hub.open();
  await vi.advanceTimersByTimeAsync(1_000);
  expect(batches).toEqual([]);
  hub.show();
  await vi.advanceTimersByTimeAsync(0);
  expect(batches).toEqual([{ ids: [], gap: true }]);
});

test("updates a query put off wait while the tab is hidden", async () => {
  const client = testClient();
  const hub = new LiveHub(client);
  const batches: string[][] = [];
  hub.register({
    update: async ({ ids }) => {
      batches.push([...ids]);
      return batches.length === 1 ? { afterMs: 2_000 } : null;
    },
  });
  hub.change("a");
  await vi.advanceTimersByTimeAsync(1_000);
  hub.hide();
  await vi.advanceTimersByTimeAsync(5_000);
  expect(batches).toEqual([["a"]]);
  hub.show();
  await vi.advanceTimersByTimeAsync(0);
  expect(batches).toEqual([["a"], []]);
});

test("live updates count as paused once the stream has been down for 10 s, and resume when it reopens", async () => {
  const { hub } = hubWithQuery();
  const heard = vi.fn();
  hub.subscribeStatus(heard);
  hub.open();
  hub.drop();
  await vi.advanceTimersByTimeAsync(5_000);
  // Every failed reconnect reports another drop; the 10 s still count from the first.
  hub.drop();
  await vi.advanceTimersByTimeAsync(4_999);
  expect(hub.status()).toBe("connected");
  await vi.advanceTimersByTimeAsync(1);
  expect(hub.status()).toBe("paused");
  hub.open();
  expect(hub.status()).toBe("connected");
  expect(heard).toHaveBeenCalledTimes(2);
  // A drop shorter than 10 s never shows the bar.
  hub.drop();
  await vi.advanceTimersByTimeAsync(3_000);
  hub.open();
  await vi.advanceTimersByTimeAsync(20_000);
  expect(hub.status()).toBe("connected");
});

test("a refused stream refetches the profile, whose 401 would end the session", async () => {
  const { client, hub } = hubWithQuery();
  const profile = vi.fn(async () => ({ user_id: "u", email: "a@b", name: "A", role: "viewer", last_login: null, visual_workspace_enabled: false, ...AGREED }));
  client.setQueryData(bootQueries.profile.queryKey, await profile());
  new QueryObserver(client, { ...bootQueries.profile, queryFn: profile, staleTime: Infinity }).subscribe(() => {});
  hub.refuse();
  await vi.advanceTimersByTimeAsync(0);
  expect(profile).toHaveBeenCalledTimes(2);
});

test("idle time runs from the user's last input", async () => {
  const { hub } = hubWithQuery();
  hub.input();
  await vi.advanceTimersByTimeAsync(1_500);
  expect(hub.idleMs()).toBe(1_500);
});

test("queries registered under one name are one query until the last leaves", async () => {
  const client = testClient();
  const hub = new LiveHub(client);
  const first: string[][] = [];
  const second: string[][] = [];
  const a = hub.register({ update: async ({ ids }) => (first.push([...ids]), null) }, { shareAs: "detail x" });
  const b = hub.register({ update: async ({ ids }) => (second.push([...ids]), null) }, { shareAs: "detail x" });
  hub.change("x");
  await vi.advanceTimersByTimeAsync(1_000);
  expect([first, second]).toEqual([[["x"]], []]);
  a.dispose();
  a.dispose();
  hub.change("y");
  await vi.advanceTimersByTimeAsync(1_000);
  expect(first).toEqual([["x"], ["y"]]);
  b.dispose();
  hub.change("z");
  await vi.advanceTimersByTimeAsync(1_000);
  expect(first).toHaveLength(2);
});

test("stopped and opened again, as a remount does, the hub recovers the gap its stream missed (CR-LIVE-02)", async () => {
  const { hub, batches } = hubWithQuery();
  hub.open();
  await vi.advanceTimersByTimeAsync(0);
  batches.length = 0;
  hub.change("a");
  hub.stop();
  hub.open();
  await vi.advanceTimersByTimeAsync(2_000);
  expect(batches).toEqual([{ ids: [], gap: true }]);
});

test("a hidden tab holds at most 100 ids; past that it recovers the gap on return instead (CR-LIVE-R2-04)", async () => {
  const { hub, batches } = hubWithQuery();
  hub.hide();
  for (let n = 0; n < 250; n++) hub.change(`id-${n}`);
  hub.show();
  await vi.advanceTimersByTimeAsync(0);
  expect(batches).toHaveLength(1);
  expect(batches[0]!.gap).toBe(true);
  expect(batches[0]!.ids.length).toBeLessThanOrEqual(100);
});
