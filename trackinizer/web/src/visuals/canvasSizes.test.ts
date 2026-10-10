import { afterEach, expect, test, vi } from "vitest";
import { heldWidth, MIN_COLUMN, readCanvasSizes, writeCanvasSizes } from "./canvasSizes";

afterEach(() => {
  vi.restoreAllMocks();
  localStorage.clear();
});

const NONE = { floating: {}, share: {} };

test("a column is held between the narrowest Chat fits in and seven tenths of the stage", () => {
  expect(heldWidth(100, 1000)).toBe(MIN_COLUMN);
  expect(heldWidth(400.4, 1000)).toBe(400);
  expect(heldWidth(900, 1000)).toBe(700);
  // A stage too narrow for both: the column keeps what Chat needs.
  expect(heldWidth(300, 300)).toBe(MIN_COLUMN);
});

test("the sizes kept are read back: a column's width, a window's size and a tile's share", () => {
  expect(readCanvasSizes()).toEqual(NONE);
  const sizes = { side: 420, floating: { "trax.chat": { width: 500, height: 400 } }, share: { "trax.browse": 1.4, "trax.subgraph": 0.6 } };
  writeCanvasSizes(sizes);
  expect(readCanvasSizes()).toEqual(sizes);
  writeCanvasSizes({ ...sizes, left: 300 });
  expect(readCanvasSizes()).toEqual({ ...sizes, left: 300 });
});

test("storage that is off, or holds something else, gives no sizes and fails nothing", () => {
  const junk = { left: "wide", side: -4, floating: { a: { width: 300 }, b: [1, 2], c: { width: 0, height: 9 } }, share: { a: "half", b: -1, c: null } };
  for (const kept of ["not json", "null", "[1]", JSON.stringify(junk), JSON.stringify({ floating: 3, share: [1] })]) {
    localStorage.setItem("trackinizer.v2.canvas.sizes", kept);
    expect(readCanvasSizes()).toEqual(NONE);
  }
  vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("denied"); });
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("denied"); });
  expect(readCanvasSizes()).toEqual(NONE);
  expect(() => writeCanvasSizes({ ...NONE, side: 400 })).not.toThrow();
});
