import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { type Batch, type Later, Serial } from "./serial";

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

/** A query whose updates finish when the test says, recording each batch. */
function heldQuery() {
  const batches: { ids: string[]; gap: boolean }[] = [];
  const finish: ((later: Later | null) => void)[] = [];
  const fail: ((error: Error) => void)[] = [];
  let running = 0;
  let most = 0;
  const query = {
    update: (batch: Batch) => {
      batches.push({ ids: [...batch.ids], gap: batch.gap });
      running += 1;
      most = Math.max(most, running);
      return new Promise<Later | null>((resolve, reject) => {
        finish.push((later) => {
          running -= 1;
          resolve(later);
        });
        fail.push((error) => {
          running -= 1;
          reject(error);
        });
      });
    },
  };
  return { query, batches, finish, fail, most: () => most };
}

/** Let resolved updates run their continuations. */
async function settle() {
  await vi.advanceTimersByTimeAsync(0);
}

test("one update at a time; ids that arrive meanwhile run once more afterwards, none dropped", async () => {
  const { query, batches, finish, most } = heldQuery();
  const serial = new Serial(query);
  serial.push(["a"]);
  serial.push(["b"]);
  serial.push(["c"], true);
  expect(batches).toEqual([{ ids: ["a"], gap: false }]);
  finish[0]!(null);
  await settle();
  expect(batches).toEqual([
    { ids: ["a"], gap: false },
    { ids: ["b", "c"], gap: true },
  ]);
  finish[1]!(null);
  await settle();
  expect(batches).toHaveLength(2);
  expect(most()).toBe(1);
});

test("an update that put work off runs again after the time it asked for, with whatever arrived by then", async () => {
  const { query, batches, finish } = heldQuery();
  const serial = new Serial(query);
  serial.push(["a", "b"]);
  finish[0]!({ afterMs: 2_000 });
  await settle();
  await vi.advanceTimersByTimeAsync(1_999);
  expect(batches).toHaveLength(1);
  serial.push(["c"]);
  finish[1]!(null);
  await vi.advanceTimersByTimeAsync(1);
  expect(batches.map((batch) => batch.ids)).toEqual([["a", "b"], ["c"], []]);
});

test("the work put off runs once, at the earliest time asked", async () => {
  const { query, batches, finish } = heldQuery();
  const serial = new Serial(query);
  serial.push(["a"]);
  finish[0]!({ afterMs: 2_000 });
  await settle();
  serial.push(["b"]);
  finish[1]!({ afterMs: 500 });
  await vi.advanceTimersByTimeAsync(500);
  expect(batches.map((batch) => batch.ids)).toEqual([["a"], ["b"], []]);
  finish[2]!(null);
  await vi.advanceTimersByTimeAsync(5_000);
  expect(batches).toHaveLength(3);
});

test("a failed update keeps its ids, and is tried again after 1, 3 and 10 s, then every 30 s, until it succeeds", async () => {
  const { query, batches, finish, fail } = heldQuery();
  const serial = new Serial(query);
  serial.push(["a"], true);
  const waits: number[] = [];
  for (let n = 0; n < 5; n++) {
    fail[n]!(new Error("503"));
    const before = batches.length;
    let waited = 0;
    while (batches.length === before && waited < 60_000) {
      await vi.advanceTimersByTimeAsync(1_000);
      waited += 1_000;
    }
    waits.push(waited);
  }
  expect(waits).toEqual([1_000, 3_000, 10_000, 30_000, 30_000]);
  expect(batches.at(-1)).toEqual({ ids: ["a"], gap: true });
  finish[5]!(null);
  await vi.advanceTimersByTimeAsync(60_000);
  expect(batches).toHaveLength(6);
});

test("an update that only put work off does not reset the wait between failed tries", async () => {
  const { query, batches, finish, fail } = heldQuery();
  const serial = new Serial(query);
  serial.push(["a"]);
  fail[0]!(new Error("503"));
  await vi.advanceTimersByTimeAsync(1_000);
  // The retry is put off, as a membership check waiting out its 2 s is.
  finish[1]!({ afterMs: 1_000 });
  await vi.advanceTimersByTimeAsync(1_000);
  fail[2]!(new Error("503"));
  await vi.advanceTimersByTimeAsync(2_999);
  expect(batches).toHaveLength(3);
  await vi.advanceTimersByTimeAsync(1);
  expect(batches).toHaveLength(4);
  // An update that finishes its work starts the waits afresh.
  finish[3]!(null);
  await settle();
  serial.push(["b"]);
  fail[4]!(new Error("503"));
  await vi.advanceTimersByTimeAsync(1_000);
  expect(batches).toHaveLength(6);
});

test("while closed it runs nothing; resumed, it runs what waited, once", async () => {
  const { query, batches } = heldQuery();
  let open = false;
  const serial = new Serial(query, () => open);
  serial.push(["a"]);
  serial.push(["b"]);
  expect(batches).toEqual([]);
  open = true;
  serial.resume();
  expect(batches).toEqual([{ ids: ["a", "b"], gap: false }]);
});

test("disposed, it runs nothing more, not even what was put off", async () => {
  const { query, batches, finish } = heldQuery();
  const serial = new Serial(query);
  serial.push(["a"]);
  finish[0]!({ afterMs: 2_000 });
  await settle();
  serial.dispose();
  serial.push(["c"]);
  await vi.advanceTimersByTimeAsync(10_000);
  expect(batches).toHaveLength(1);
});

test("an update that succeeds while failed work waits out its backoff does not start the waits afresh (CR-LIVE-R5-D1)", async () => {
  const { query, batches, finish, fail } = heldQuery();
  const serial = new Serial(query);
  serial.push(["a"]);
  fail[0]!(new Error("503"));
  await settle();
  // Other work runs and succeeds while "a" waits for its first retry.
  serial.push(["b"]);
  finish[1]!(null);
  await vi.advanceTimersByTimeAsync(1_000);
  expect(batches.map((batch) => batch.ids)).toEqual([["a"], ["b"], ["a"]]);
  fail[2]!(new Error("503"));
  await vi.advanceTimersByTimeAsync(2_999);
  expect(batches).toHaveLength(3);
  await vi.advanceTimersByTimeAsync(1);
  expect(batches).toHaveLength(4);
});

test("work put off for less time than a failed update's backoff runs then without the failed ids (CR-LIVE-R8-B1)", async () => {
  const { query, batches, finish, fail } = heldQuery();
  const serial = new Serial(query);
  serial.push(["a"]);
  fail[0]!(new Error("503"));
  await settle();
  serial.push(["b"]);
  finish[1]!({ afterMs: 100 });
  await vi.advanceTimersByTimeAsync(100);
  expect(batches.map((batch) => batch.ids)).toEqual([["a"], ["b"], []]);
  finish[2]!(null);
  await vi.advanceTimersByTimeAsync(899);
  expect(batches).toHaveLength(3);
  await vi.advanceTimersByTimeAsync(1);
  expect(batches.map((batch) => batch.ids)).toEqual([["a"], ["b"], [], ["a"]]);
});

test("an update the tab hid under runs again, with its batch, once the tab is back (CR-LIVE-R3-02)", async () => {
  const { query, batches, finish } = heldQuery();
  let open = true;
  const serial = new Serial(query, () => open);
  serial.push(["a"], true);
  open = false;
  finish[0]!(null);
  await vi.advanceTimersByTimeAsync(60_000);
  expect(batches).toHaveLength(1);
  open = true;
  serial.resume();
  expect(batches).toEqual([
    { ids: ["a"], gap: true },
    { ids: ["a"], gap: true },
  ]);
});

test("updates that keep failing say so after the first try and two retries, with a Retry that tries at once; a success clears it (CR-R3-B4)", async () => {
  const { query, batches, finish, fail } = heldQuery();
  const heard: (string | null)[] = [];
  let retry = () => {};
  const serial = new Serial({
    update: query.update,
    failing: (failure) => {
      heard.push(failure?.error.message ?? null);
      if (failure) retry = failure.retry;
    },
  });
  serial.push(["a"]);
  fail[0]!(new Error("first"));
  await vi.advanceTimersByTimeAsync(1_000);
  fail[1]!(new Error("second"));
  await vi.advanceTimersByTimeAsync(3_000);
  expect(heard).toEqual([]);
  fail[2]!(new Error("503 Service Unavailable"));
  await settle();
  expect(heard).toEqual(["503 Service Unavailable"]);
  // Retry runs now, not after the 10 s wait.
  retry();
  expect(batches).toHaveLength(4);
  finish[3]!(null);
  await settle();
  expect(heard).toEqual(["503 Service Unavailable", null]);
});
