import { expect, test } from "vitest";
import { clickSelect } from "./selection";

const ORDER = ["a", "b", "c", "d", "e"];
const sorted = (ids: ReadonlySet<string>) => [...ids].sort();

test("a click toggles one row", () => {
  const once = clickSelect(new Set(), ORDER, null, "b");
  expect(sorted(once)).toEqual(["b"]);
  expect(sorted(clickSelect(once, ORDER, null, "b"))).toEqual([]);
});

test("a shift-click selects every row shown from the anchor to it, either way, and keeps the rest", () => {
  expect(sorted(clickSelect(new Set(["a"]), ORDER, "b", "d"))).toEqual(["a", "b", "c", "d"]);
  expect(sorted(clickSelect(new Set(), ORDER, "d", "b"))).toEqual(["b", "c", "d"]);
  // A range only adds: a selected row inside it stays selected.
  expect(sorted(clickSelect(new Set(["c"]), ORDER, "b", "d"))).toEqual(["b", "c", "d"]);
});

test("a shift-click whose anchor is no longer shown toggles the one row", () => {
  expect(sorted(clickSelect(new Set(["a"]), ORDER, "gone", "d"))).toEqual(["a", "d"]);
});
