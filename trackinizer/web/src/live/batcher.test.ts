import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { Batcher } from "./batcher";

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

test("ids are collected for one second from the first, deduplicated, then handed over once", () => {
  const batches: string[][] = [];
  const batcher = new Batcher((ids) => batches.push([...ids]));
  batcher.add("a");
  vi.advanceTimersByTime(400);
  batcher.add("b");
  batcher.add("a");
  vi.advanceTimersByTime(599);
  expect(batches).toEqual([]);
  vi.advanceTimersByTime(1);
  expect(batches).toEqual([["a", "b"]]);
  // A quiet stream makes no batches; the next id opens a new window.
  vi.advanceTimersByTime(5_000);
  batcher.add("c");
  vi.advanceTimersByTime(1_000);
  expect(batches).toEqual([["a", "b"], ["c"]]);
});

test("held, it only collects; released, it hands over what it collected", () => {
  const batches: string[][] = [];
  const batcher = new Batcher((ids) => batches.push([...ids]));
  batcher.add("a");
  batcher.hold();
  batcher.add("b");
  vi.advanceTimersByTime(60_000);
  expect(batches).toEqual([]);
  expect([...batcher.release()]).toEqual(["a", "b"]);
  batcher.add("c");
  vi.advanceTimersByTime(1_000);
  expect(batches).toEqual([["c"]]);
});

test("taking the ids closes the window without a batch", () => {
  const batches: string[][] = [];
  const batcher = new Batcher((ids) => batches.push([...ids]));
  batcher.add("a");
  expect([...batcher.take()]).toEqual(["a"]);
  vi.advanceTimersByTime(1_000);
  expect(batches).toEqual([]);
});
