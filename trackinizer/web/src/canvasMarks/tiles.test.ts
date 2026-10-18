import { afterEach, expect, test } from "vitest";
import { tilesInPaintOrder } from "./tiles";

afterEach(() => document.body.replaceChildren());

type Options = { readonly lift?: number; readonly size?: readonly [number, number]; readonly bare?: boolean };

/** A visual of `stage` whose body stands at (left, 0) and is 100 x 50 unless it says otherwise; `lift` makes it float at that z-index. */
function visual(stage: HTMLElement, id: string, left: number, options: Options = {}) {
  const tile = document.createElement("div");
  tile.dataset.visualInstance = id;
  if (options.lift !== undefined) {
    tile.className = "visual-tile-floating";
    tile.style.zIndex = String(options.lift);
  }
  if (!options.bare) {
    const body = document.createElement("div");
    body.className = "visual-tile-body";
    const [width, height] = options.size ?? [100, 50];
    body.getBoundingClientRect = () => ({ left, top: 0, width, height, right: left + width, bottom: height, x: left, y: 0, toJSON: () => ({}) });
    tile.append(body);
  }
  stage.append(tile);
}

test("the docked tiles come first in page order, then the floating ones by z-index, the topmost last", () => {
  const stage = document.createElement("div");
  visual(stage, "float-high", 0, { lift: 12 });
  visual(stage, "docked-b", 100);
  visual(stage, "float-low", 200, { lift: 10 });
  visual(stage, "docked-a", 300);

  expect(tilesInPaintOrder(stage).map((tile) => tile.id)).toEqual(["docked-b", "docked-a", "float-low", "float-high"]);
});

test("a tile carries its body's rect, and one with no body or no size is left out", () => {
  const stage = document.createElement("div");
  visual(stage, "a", 40);
  visual(stage, "folded", 0, { size: [0, 0] });
  visual(stage, "no-body", 0, { bare: true });

  expect(tilesInPaintOrder(stage)).toEqual([{ id: "a", rect: { left: 40, top: 0, width: 100, height: 50 } }]);
});
