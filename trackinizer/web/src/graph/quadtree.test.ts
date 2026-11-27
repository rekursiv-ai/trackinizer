import { expect, test } from "vitest";
import { BarnesHutTree } from "./quadtree";

test("BarnesHutTree applies symmetric repulsion between two separated nodes", () => {
  const tree = new BarnesHutTree();
  const x = new Float32Array([-10, 10]);
  const y = new Float32Array([0, 0]);
  const vx = new Float32Array(2);
  const vy = new Float32Array(2);

  tree.build(2, x, y, () => 0);
  tree.applyRepulsion(2, x, y, vx, vy, -30, 500 * 500, () => 0);

  // Negative strength repels: node 0 (-10) is pushed left (-vx), node 1 (+10) right (+vx).
  expect(vx[0]).toBeLessThan(0);
  expect(vx[1]).toBeGreaterThan(0);
  expect(vx[0]! + vx[1]!).toBeCloseTo(0, 5);
  expect(vy[0]).toBe(0);
  expect(vy[1]).toBe(0);
});

test("BarnesHutTree grows traversal stack beyond 256 nodes without dropping repulsion", () => {
  const count = 400;
  const x = new Float32Array(count);
  const y = new Float32Array(count);
  const vx = new Float32Array(count);
  const vy = new Float32Array(count);

  // Place nodes along a spiral so the quadtree has deep/broad branching.
  for (let i = 0; i < count; i++) {
    x[i] = Math.cos(i * 0.7) * (i + 1) * 0.5;
    y[i] = Math.sin(i * 0.7) * (i + 1) * 0.5;
  }

  const tree = new BarnesHutTree();
  tree.build(count, x, y, () => 0.1);
  tree.applyRepulsion(count, x, y, vx, vy, -30, 1000 * 1000, () => 0.1);

  for (let i = 0; i < count; i++) {
    expect(Number.isFinite(vx[i])).toBe(true);
    expect(Number.isFinite(vy[i])).toBe(true);
    expect(Math.hypot(vx[i]!, vy[i]!)).toBeGreaterThan(0);
  }
});
