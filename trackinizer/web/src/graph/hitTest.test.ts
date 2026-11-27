import { expect, test } from "vitest";
import { hitTestNode } from "./hitTest";
import type { DrawNode } from "./model";

function drawNode(
  id: string,
  x: number | undefined,
  y: number | undefined,
  radius: number,
  hidden = false,
): DrawNode {
  return {
    id,
    x,
    y,
    hidden,
    look: { radius },
  } as unknown as DrawNode;
}

test("hitTestNode returns topmost visible placed node and ignores hidden/unplaced", () => {
  const bottom = drawNode("bottom", 0, 0, 10);
  const top = drawNode("top", 4, 0, 10);
  const hidden = drawNode("hidden", 4, 0, 20, true);
  const unplaced = drawNode("unplaced", undefined, undefined, 20);

  expect(hitTestNode([bottom, top, hidden, unplaced], 4, 0, 1)?.id).toBe("top");
  expect(hitTestNode([bottom, top], 100, 100, 1)).toBeNull();
});

test("hitTestNode enforces minimum 6px screen hit radius when zoomed out", () => {
  const small = drawNode("small", 0, 0, 2);
  // At scale = 0.2, 6px = 30 graph units.
  expect(hitTestNode([small], 25, 0, 0.2)?.id).toBe("small");
  expect(hitTestNode([small], 35, 0, 0.2)).toBeNull();
});
