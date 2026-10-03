import { expect, test } from "vitest";
import { type Body, clusterForce } from "./clusters";

type Placed = Body & { group?: string };

function at(x: number, y: number, group?: string): Placed {
  return { x, y, vx: 0, vy: 0, group };
}

/** One tick of the force over `nodes` at heat `alpha`; returns their velocities. */
function tick(nodes: Placed[], alpha = 1): [number, number][] {
  const force = clusterForce<Placed>((node) => node.group);
  force.initialize(nodes);
  force(alpha);
  return nodes.map(({ vx, vy }) => [vx!, vy!]);
}

test("each node is pulled toward the middle of its group, harder the farther it is and the hotter the layout", () => {
  const [near, nearMate] = tick([at(0, 0, "a"), at(10, 0, "a"), at(5000, 0, "b")]);
  expect(near![0]).toBeGreaterThan(0);
  expect(nearMate).toEqual([-near![0], 0]);
  const [far] = tick([at(0, 0, "a"), at(20, 0, "a"), at(5000, 0, "b")]);
  expect(far![0]).toBeCloseTo(2 * near![0]);
  const [cool] = tick([at(0, 0, "a"), at(10, 0, "a"), at(5000, 0, "b")], 0.5);
  expect(cool![0]).toBeCloseTo(near![0] / 2);
});

test("a node in no group, and a group far from the others with its nodes at its middle, are left alone", () => {
  const free = at(3, 4);
  expect(tick([at(0, 0, "a"), at(0, 0, "a"), at(5000, 0, "b"), free])).toEqual([
    [0, 0],
    [0, 0],
    [0, 0],
    [0, 0],
  ]);
});

test("groups that overlap are pushed apart along the line between their middles, the bigger one less", () => {
  const moved = tick([at(0, 0, "a"), at(0, 0, "a"), at(0, 0, "a"), at(0, 0, "a"), at(1, 0, "b")]);
  const [big, small] = [moved[0]!, moved[4]!];
  expect(big[0]).toBeLessThan(0);
  expect(small[0]).toBeGreaterThan(0);
  expect([big[1], small[1]]).toEqual([0, 0]);
  // Each group moves whole, and the push moves their common middle nowhere.
  expect(moved.slice(0, 4)).toEqual([big, big, big, big]);
  expect(4 * big[0] + small[0]).toBeCloseTo(0);
});

test("groups in the same place part, the same way every time", () => {
  const nodes = () => [at(0, 0, "a"), at(0, 0, "b"), at(0, 0, "c")];
  const moved = tick(nodes());
  expect(new Set(moved.map(String)).size).toBe(3);
  for (const [vx, vy] of moved) expect(Math.hypot(vx, vy)).toBeGreaterThan(0);
  expect(tick(nodes())).toEqual(moved);
});

test("two groups that overlap are pushed apart once, whatever stretch of the plane they share", () => {
  // Discs of radius 30, 10 apart: 50 overlap, half of it undone, shared by two equal groups.
  for (const [x, y] of [
    [0, 0],
    [-1_000, 2_345],
  ]) {
    expect(tick([at(x, y, "a"), at(x + 10, y, "b")])).toEqual([
      [-12.5, 0],
      [12.5, 0],
    ]);
  }
});

test("a big group pushes a small one at its rim, far from its middle", () => {
  // 400 nodes count as a disc of radius 600, which reaches 50 past the small group at 580.
  const big = Array.from({ length: 400 }, () => at(-3_000, -3_000, "big"));
  const moved = tick([...big, at(-2_420, -3_000, "small")]);
  const [bigShift, smallShift] = [moved[0]!, moved.at(-1)!];
  expect(smallShift[0]).toBeCloseTo((0.5 * 50 * 400) / 401);
  expect(bigShift[0]).toBeCloseTo((-0.5 * 50) / 401);
  expect([bigShift[1], smallShift[1]]).toEqual([0, 0]);
});
