import { expect, test } from "vitest";
import { matching, renamed, stepStation } from "./cycle";

const station = (id: string, name: string, created: number) => ({ id, name, created_at: `2026-10-0${created}T10:00:00Z` });
const A = station("a", "unnamed-workstream#1", 1);
const B = station("b", "alpha", 2);
const C = station("c", "unnamed-workstream#12", 3);
const D = station("d", "Beta", 4);

test("the cycle holds the renamed stations in the order they were made, whatever order they come in", () => {
  expect(renamed([D, C, B, A]).map((each) => each.id)).toEqual(["b", "d"]);
  // A name that only looks like the default one is a name.
  expect(renamed([station("e", "unnamed-workstream#x", 5), station("f", "my unnamed-workstream#2", 6)]).map((each) => each.id)).toEqual(["e", "f"]);
});

test("stations made at the same moment keep one order, by id, whatever order they come in", () => {
  const twinA = { id: "a2", name: "twin", created_at: "2026-10-05T10:00:00Z" };
  const twinB = { id: "b2", name: "other twin", created_at: "2026-10-05T10:00:00Z" };
  expect(renamed([twinB, twinA]).map((each) => each.id)).toEqual(["a2", "b2"]);
  expect(renamed([twinA, twinB]).map((each) => each.id)).toEqual(["a2", "b2"]);
});

test("a step goes to the next renamed station and wraps round at either end", () => {
  const all = [D, B, A, C];
  expect(stepStation(all, "b", 1)?.id).toBe("d");
  expect(stepStation(all, "d", 1)?.id).toBe("b");
  expect(stepStation(all, "b", -1)?.id).toBe("d");
  expect(stepStation(all, "d", -1)?.id).toBe("b");
});

test("from a station outside the cycle a step forward goes to the first and a step back to the last; with none renamed, nowhere", () => {
  expect(stepStation([A, B, D], "a", 1)?.id).toBe("b");
  expect(stepStation([A, B, D], "a", -1)?.id).toBe("d");
  expect(stepStation([A, C], "a", 1)).toBeNull();
});

test("a search holds the stations whose name has the text, in any case, and a blank one holds them all", () => {
  expect(matching([A, B, C, D], "ALP").map((each) => each.id)).toEqual(["b"]);
  expect(matching([A, B, C, D], "eta").map((each) => each.id)).toEqual(["d"]);
  expect(matching([A, B, C, D], "  ").map((each) => each.id)).toEqual(["a", "b", "c", "d"]);
  expect(matching([A, B], "zzz")).toEqual([]);
});
