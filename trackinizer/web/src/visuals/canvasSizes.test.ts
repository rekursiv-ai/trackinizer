import { afterEach, expect, test, vi } from "vitest";
import { dropLegacySizes, fitSizes, fitWidth, heldWidth, legacyLayout, MIN_CHAT, MIN_COLUMN, MIN_FLOAT, readLegacySizes, toFraction, toPixels } from "./canvasSizes";

afterEach(() => {
  vi.restoreAllMocks();
  localStorage.clear();
});

const KEY = "trackinizer.v2.canvas.sizes";
const NONE = { floating: {}, share: {} };

test("a column is held between the narrowest Chat fits in and seven tenths of the stage", () => {
  expect(heldWidth(100, 1000)).toBe(MIN_COLUMN);
  expect(heldWidth(400.4, 1000)).toBe(400);
  expect(heldWidth(900, 1000)).toBe(700);
  // A stage narrower than a column still gives it the least.
  expect(heldWidth(300, 300)).toBe(MIN_COLUMN);
  // Chat has a least of its own.
  expect(heldWidth(100, 1000, MIN_CHAT)).toBe(MIN_CHAT);
  expect(heldWidth(900, 1000, MIN_CHAT)).toBe(700);
});

test("a stage not yet measured holds only the least", () => {
  expect(fitWidth(512, 0, MIN_CHAT)).toBe(512);
  expect(fitWidth(100, 0, MIN_CHAT)).toBe(MIN_CHAT);
  expect(fitWidth(900, 1000, MIN_CHAT)).toBe(700);
  expect(toFraction(400, 0)).toBeNull();
});

test("a width kept at one stage is the same fraction of another: it round-trips at 1600 and at 1280", () => {
  const fraction = toFraction(400, 1600)!;
  expect(fraction).toBe(0.25);
  expect(toPixels(fraction, 1600)).toBe(400);
  expect(toPixels(fraction, 1280)).toBe(320);
  const layout = { columns: { left: 0.25, side: 0.3125 }, floating: { "trax.artifact": { width: 0.25, height: 0.5 } }, share: { "trax.browse": 1.4 } };
  expect(fitSizes(layout, { width: 1600, height: 800 })).toEqual({
    left: 400, side: 500, floating: { "trax.artifact": { width: 400, height: 400 } }, share: { "trax.browse": 1.4 },
  });
  expect(fitSizes(layout, { width: 1280, height: 640 })).toEqual({
    left: 320, side: 400, floating: { "trax.artifact": { width: 320, height: 320 } }, share: { "trax.browse": 1.4 },
  });
});

test("a stage too narrow for the stored columns shows them clamped, and fitting changes nothing it was given", () => {
  const layout = { columns: { left: 0.3, side: 0.9 }, floating: { "trax.artifact": { width: 0.05, height: 0.05 } } };
  const kept = structuredClone(layout);
  const phone = fitSizes(layout, { width: 390, height: 700 });
  expect(phone.left).toBe(MIN_COLUMN);
  expect(phone.side).toBe(273);
  expect(phone.floating["trax.artifact"]).toEqual({ width: MIN_FLOAT.width, height: MIN_FLOAT.height });
  expect(layout).toEqual(kept);
  // A window never outgrows the stage.
  expect(fitSizes({ floating: { a: { width: 1, height: 1 } } }, { width: 250, height: 200 }).floating.a).toEqual({ width: 250, height: 200 });
});

test("a stage not yet measured fits no column and no window", () => {
  const layout = { columns: { left: 0.3 }, floating: { a: { width: 0.5, height: 0.5 } }, share: { a: 2 } };
  expect(fitSizes(layout, { width: 0, height: 0 })).toEqual({ floating: {}, share: { a: 2 } });
});

test("the sizes an older build kept are read back: a column's width, a window's size, a tile's share and Chat's width", () => {
  expect(readLegacySizes()).toEqual(NONE);
  const sizes = { side: 420, chat: 512, floating: { "trax.artifact": { width: 500, height: 400 } }, share: { "trax.browse": 1.4, "trax.subgraph": 0.6 } };
  localStorage.setItem(KEY, JSON.stringify(sizes));
  expect(readLegacySizes()).toEqual(sizes);
});

test("storage that is off, or holds something else, gives no sizes and fails nothing", () => {
  const junk = { left: "wide", side: -4, chat: "wide", floating: { a: { width: 300 }, b: [1, 2], c: { width: 0, height: 9 } }, share: { a: "half", b: -1, c: null } };
  for (const kept of ["not json", "null", "[1]", JSON.stringify(junk), JSON.stringify({ floating: 3, share: [1] })]) {
    localStorage.setItem(KEY, kept);
    expect(readLegacySizes()).toEqual(NONE);
  }
  vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("denied"); });
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("denied"); });
  expect(readLegacySizes()).toEqual(NONE);
  expect(() => dropLegacySizes("chat")).not.toThrow();
});

test("forgetting the canvas's sizes leaves Chat's width for its own migration, and the key goes when nothing is left", () => {
  localStorage.setItem(KEY, JSON.stringify({ side: 420, chat: 512, floating: {}, share: {} }));
  dropLegacySizes("canvas");
  expect(JSON.parse(localStorage.getItem(KEY)!)).toEqual({ chat: 512 });
  dropLegacySizes("chat");
  expect(localStorage.getItem(KEY)).toBeNull();
  dropLegacySizes("chat");
  expect(localStorage.getItem(KEY)).toBeNull();
});

test("an older build's pixels become fractions of the stage they are moved onto", () => {
  const sizes = { side: 400, floating: { "trax.artifact": { width: 400, height: 200 } }, share: { "trax.browse": 1.4 } };
  const tiles = { "trax.browse": { collapsed: true, place: { left: 160, top: 80 } }, "trax.timeline": { collapsed: false, place: null } };
  expect(legacyLayout(sizes, tiles, { width: 1600, height: 800 })).toEqual({
    columns: { side: 0.25 },
    floating: { "trax.artifact": { width: 0.25, height: 0.25 } },
    tiles: { "trax.browse": { collapsed: true, place: { left: 0.1, top: 0.1 } }, "trax.timeline": { collapsed: false } },
    share: { "trax.browse": 1.4 },
  });
  expect(legacyLayout(NONE, {}, { width: 1600, height: 800 })).toEqual({});
});
