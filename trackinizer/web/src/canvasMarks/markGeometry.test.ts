import { expect, test } from "vitest";
import { boundsOf, pinTo, toFractions, topmostHolding } from "./markGeometry";

const rect = { left: 100, top: 50, width: 400, height: 200 };

test("a pointer is a fraction of the stage at any size, clamped to it", () => {
  expect(toFractions(300, 150, rect)).toEqual({ x: 0.5, y: 0.5 });
  expect(toFractions(300, 150, { left: 0, top: 0, width: 1000, height: 400 })).toEqual({ x: 0.3, y: 0.375 });
  expect(toFractions(0, 9999, rect)).toEqual({ x: 0, y: 1 });
  expect(toFractions(1, 1, { ...rect, width: 0 })).toEqual({ x: 0, y: 0 });
});

test("bounds are the smallest box over every point", () => {
  expect(boundsOf([{ x: 0.6, y: 0.1 }, { x: 0.2, y: 0.9 }, { x: 0.4, y: 0.5 }])).toEqual({ left: 0.2, top: 0.1, right: 0.6, bottom: 0.9 });
});

const stage = { left: 0, top: 0, width: 1000, height: 500 };
const under = { id: "under", rect: { left: 0, top: 0, width: 1000, height: 500 } };
const over = { id: "over", rect: { left: 500, top: 100, width: 400, height: 200 } };

test("the topmost tile holding a point is the last in paint order that does, edges included", () => {
  expect(topmostHolding([under, over], 700, 200)?.id).toBe("over");
  expect(topmostHolding([under, over], 500, 100)?.id).toBe("over");
  expect(topmostHolding([under, over], 100, 200)?.id).toBe("under");
  expect(topmostHolding([over], 100, 200)).toBeUndefined();
});

test("a box over a tile is the tile's, in fractions of its body", () => {
  const corners = [{ x: 0.55, y: 0.3 }, { x: 0.85, y: 0.5 }];
  expect(pinTo(corners, stage, [under, over])).toEqual({ tile: "over", points: [{ x: 0.125, y: 0.25 }, { x: 0.875, y: 0.75 }] });
});

test("a box whose center is over no tile keeps the stage's fractions, and one centered on a tile has its far corner clamped to it", () => {
  const corners = [{ x: 0.1, y: 0.1 }, { x: 0.2, y: 0.2 }];
  expect(pinTo(corners, stage, [over])).toEqual({ tile: null, points: corners });
  expect(pinTo([{ x: 0.52, y: 0.3 }, { x: 0.99, y: 0.4 }], stage, [over]).points).toEqual([{ x: 0.05, y: 0.25 }, { x: 1, y: 0.5 }]);
});
