import { afterEach, expect, test, vi } from "vitest";
import { heldWidth, MIN_CHAT, MIN_COLUMN, readCanvasSizes, writeCanvasSizes, writeChatWidth } from "./canvasSizes";

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
  // Chat's column holds its own least.
  expect(heldWidth(100, 1000, MIN_CHAT)).toBe(MIN_CHAT);
  expect(heldWidth(900, 1000, MIN_CHAT)).toBe(700);
});

test("the Chat column's width is kept beside the canvas's sizes, which a canvas write leaves in place", () => {
  writeCanvasSizes({ side: 420, floating: {}, share: {} });
  writeChatWidth(512);
  expect(readCanvasSizes()).toEqual({ side: 420, chat: 512, floating: {}, share: {} });
  // The canvas holds the sizes it read at mount, without a width Chat gave since.
  writeCanvasSizes({ side: 380, floating: {}, share: {} });
  expect(readCanvasSizes()).toEqual({ side: 380, chat: 512, floating: {}, share: {} });
});

test("the sizes kept are read back: a column's width, a window's size and a tile's share", () => {
  expect(readCanvasSizes()).toEqual(NONE);
  const sizes = { side: 420, floating: { "trax.artifact": { width: 500, height: 400 } }, share: { "trax.browse": 1.4, "trax.subgraph": 0.6 } };
  writeCanvasSizes(sizes);
  expect(readCanvasSizes()).toEqual(sizes);
  writeCanvasSizes({ ...sizes, left: 300 });
  expect(readCanvasSizes()).toEqual({ ...sizes, left: 300 });
});

test("storage that is off, or holds something else, gives no sizes and fails nothing", () => {
  const junk = { left: "wide", side: -4, chat: "wide", floating: { a: { width: 300 }, b: [1, 2], c: { width: 0, height: 9 } }, share: { a: "half", b: -1, c: null } };
  for (const kept of ["not json", "null", "[1]", JSON.stringify(junk), JSON.stringify({ floating: 3, share: [1] })]) {
    localStorage.setItem("trackinizer.v2.canvas.sizes", kept);
    expect(readCanvasSizes()).toEqual(NONE);
  }
  vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("denied"); });
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("denied"); });
  expect(readCanvasSizes()).toEqual(NONE);
  expect(() => writeCanvasSizes({ ...NONE, side: 400 })).not.toThrow();
  expect(() => writeChatWidth(400)).not.toThrow();
});
