import { expect, test } from "vitest";
import { boundsOf, toFractions } from "./markGeometry";

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
