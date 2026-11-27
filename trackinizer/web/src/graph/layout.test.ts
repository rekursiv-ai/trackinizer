import { expect, test } from "vitest";
import { FORCE_ALPHA_MIN } from "./constants";
import { ForceSim2D } from "./layout";

test("ForceSim2D initializes unplaced nodes and converges to settled state", () => {
  const sim = new ForceSim2D();
  const count = 6;
  sim.setGraph({
    count,
    x: new Float32Array(count).fill(Number.NaN),
    y: new Float32Array(count).fill(Number.NaN),
    vx: new Float32Array(count),
    vy: new Float32Array(count),
    linkFrom: new Int32Array([0, 1, 2, 3, 4]),
    linkTo: new Int32Array([1, 2, 3, 4, 5]),
    linkStrength: new Float32Array(5).fill(0.6),
    groupId: new Int32Array([0, 0, 0, 1, 1, 1]),
  });

  let ticks = 0;
  while (sim.tick() && ticks < 500) ticks += 1;

  expect(sim.alpha).toBeLessThan(FORCE_ALPHA_MIN);
  expect(ticks).toBeGreaterThan(0);
  for (let i = 0; i < count; i++) {
    expect(Number.isFinite(sim.x[i])).toBe(true);
    expect(Number.isFinite(sim.y[i])).toBe(true);
  }
});
