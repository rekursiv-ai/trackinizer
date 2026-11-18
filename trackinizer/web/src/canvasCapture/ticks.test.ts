import { expect, test } from "vitest";
import { drawTicks, TICK } from "./ticks";

/** A context that records where lines run and what is written. */
function recorder() {
  const lines: [number, number, number, number][] = [];
  const labels: [string, number, number][] = [];
  let from: [number, number] = [0, 0];
  let to: [number, number] = [0, 0];
  const context = {
    save() {}, restore() {}, beginPath() {},
    moveTo: (x: number, y: number) => { from = [x, y]; },
    lineTo: (x: number, y: number) => { to = [x, y]; },
    stroke: () => lines.push([...from, ...to]),
    strokeText() {},
    fillText: (label: string, x: number, y: number) => labels.push([label, x, y]),
  } as unknown as CanvasRenderingContext2D;
  return { context, lines, labels };
}

test("a tick every tenth of the top and the left edge, labelled 0.1 to 0.9", () => {
  const { context, labels } = recorder();
  drawTicks(context, { width: 600, height: 300 });
  const names = labels.map(([label]) => label);
  expect(names).toEqual(["0.1", "0.1", "0.2", "0.2", "0.3", "0.3", "0.4", "0.4", "0.5", "0.5", "0.6", "0.6", "0.7", "0.7", "0.8", "0.8", "0.9", "0.9"]);
});

test("ticks run along the top at the tenths of the width and the left at the tenths of the height", () => {
  const { context, lines } = recorder();
  drawTicks(context, { width: 600, height: 300 });
  // Each tick is drawn twice, as a halo and as ink.
  const top = lines.filter(([, y1]) => y1 === 0).map(([x]) => x);
  const left = lines.filter(([x1]) => x1 === 0).map(([, y]) => y);
  expect([...new Set(top)]).toEqual([60, 120, 180, 240, 300, 360, 420, 480, 540]);
  expect([...new Set(left)]).toEqual([30, 60, 90, 120, 150, 180, 210, 240, 270]);
});

test("ticks stay in a thin band at the edge, so they hide no content", () => {
  const { context, lines } = recorder();
  drawTicks(context, { width: 600, height: 600 });
  for (const [x1, y1, x2, y2] of lines) {
    expect(Math.max(Math.abs(x2 - x1), Math.abs(y2 - y1))).toBe(TICK);
  }
});

test("ticks grow with a larger picture so they stay readable", () => {
  const { context, lines } = recorder();
  drawTicks(context, { width: 1600, height: 1200 });
  const reach = Math.max(...lines.map(([x1, y1, x2, y2]) => Math.max(x2 - x1, y2 - y1)));
  expect(reach).toBeCloseTo(TICK * 2, 5);
});
