import { expect, test } from "vitest";
import {
  boundsOf,
  clampZoom,
  fitBounds,
  GraphCamera,
  MAX_ZOOM,
  MIN_ZOOM,
} from "./camera";
import type { DrawNode } from "./model";

function node(x: number | undefined, y: number | undefined, hidden = false): DrawNode {
  return {
    id: `n-${x}-${y}`,
    x,
    y,
    hidden,
  } as unknown as DrawNode;
}

test("GraphCamera converts between screen and graph coordinates around center", () => {
  const cam = new GraphCamera();
  cam.resize(800, 600);
  cam.x = 10;
  cam.y = -20;
  cam.zoom = 2;

  expect(cam.graphToScreen(10, -20)).toEqual({ x: 400, y: 300 });
  expect(cam.screenToGraph(400, 300)).toEqual({ x: 10, y: -20 });
  expect(cam.screenToGraph(500, 340)).toEqual({ x: 60, y: 0 });
});

test("zoomAtScreen keeps cursor anchor fixed and respects MIN_ZOOM / MAX_ZOOM", () => {
  const cam = new GraphCamera();
  cam.resize(800, 600);
  const anchorBefore = cam.screenToGraph(250, 150);
  cam.zoomAtScreen(250, 150, 2);
  const anchorAfter = cam.screenToGraph(250, 150);

  expect(anchorAfter.x).toBeCloseTo(anchorBefore.x, 6);
  expect(anchorAfter.y).toBeCloseTo(anchorBefore.y, 6);

  cam.zoomAtScreen(250, 150, 1000);
  expect(cam.zoom).toBe(MAX_ZOOM);

  cam.zoomAtScreen(250, 150, 0.0001);
  expect(cam.zoom).toBe(MIN_ZOOM);
});

test("animateTo clamps target zoom for immediate and tweened moves", () => {
  const cam = new GraphCamera();
  cam.animateTo(5, 6, 999, 0);
  expect(cam.zoom).toBe(MAX_ZOOM);
  expect(cam.x).toBe(5);
  expect(cam.y).toBe(6);

  cam.animateTo(0, 0, 0.0001, 200);
  expect(cam.animating).toBe(true);
  cam.step(performance.now() + 500);
  expect(cam.animating).toBe(false);
  expect(cam.zoom).toBe(MIN_ZOOM);
  expect(clampZoom(0)).toBe(MIN_ZOOM);
});

test("boundsOf ignores hidden and unplaced nodes; fitBounds offsets for coveredRight", () => {
  const nodes = [
    node(0, 0),
    node(100, 50),
    node(999, 999, true),
    node(undefined, undefined),
  ];
  const bounds = boundsOf(nodes, () => true);
  expect(bounds).toEqual({ left: 0, right: 100, top: 0, bottom: 50 });

  const fitted = fitBounds(bounds!, 1000, 600, 200, 4, 40);
  expect(fitted.zoom).toBe(4);
  expect(fitted.x).toBeCloseTo(50 + 200 / 2 / 4, 6);
  expect(fitted.y).toBe(25);
});
