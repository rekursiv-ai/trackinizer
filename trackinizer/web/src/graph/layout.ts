import {
  CLUSTER_PULL,
  CLUSTER_PUSH,
  CLUSTER_SPACING,
  FORCE_ALPHA_DECAY,
  FORCE_ALPHA_MIN,
  FORCE_CHARGE,
  FORCE_DISTANCE_MAX,
  FORCE_GRAVITY,
  FORCE_LINK_DISTANCE,
  FORCE_VELOCITY_DECAY,
  GOLDEN_ANGLE,
} from "./constants";
import { BarnesHutTree } from "./quadtree";

/**
 * Typed-array 2D force simulation matching `forceGraphRenderer`'s physics.
 */
export type SimInput = {
  readonly count: number;
  readonly x: Float32Array;
  readonly y: Float32Array;
  readonly vx: Float32Array;
  readonly vy: Float32Array;
  readonly linkFrom: Int32Array;
  readonly linkTo: Int32Array;
  readonly linkStrength: Float32Array;
  readonly groupId: Int32Array;
};

const DISTANCE_MAX2 = FORCE_DISTANCE_MAX * FORCE_DISTANCE_MAX;
const VELOCITY_KEEP = 1 - FORCE_VELOCITY_DECAY;
const CELL_PX = 2 * CLUSTER_SPACING;
const CELL_KEY_STRIDE = 2 ** 26;

export class ForceSim2D {
  count = 0;
  x: Float32Array = new Float32Array(0);
  y: Float32Array = new Float32Array(0);
  vx: Float32Array = new Float32Array(0);
  vy: Float32Array = new Float32Array(0);
  linkFrom: Int32Array = new Int32Array(0);
  linkTo: Int32Array = new Int32Array(0);
  linkStrength: Float32Array = new Float32Array(0);
  linkBias: Float32Array = new Float32Array(0);
  linkWeight: Float32Array = new Float32Array(0);
  groupId: Int32Array = new Int32Array(0);
  #groups: number[][] = [];
  #tree = new BarnesHutTree();

  alpha = 0;
  #seed = 1;

  setGraph(input: SimInput, reheat = true): void {
    this.count = input.count;
    this.x = input.x;
    this.y = input.y;
    this.vx = input.vx;
    this.vy = input.vy;
    this.linkFrom = input.linkFrom;
    this.linkTo = input.linkTo;
    this.linkStrength = input.linkStrength;
    this.groupId = input.groupId;

    for (let i = 0; i < this.count; i++) {
      if (!Number.isFinite(this.x[i]) || !Number.isFinite(this.y[i])) {
        const r = 10 * Math.sqrt(0.5 + i);
        const a = i * GOLDEN_ANGLE;
        this.x[i] = r * Math.cos(a);
        this.y[i] = r * Math.sin(a);
        this.vx[i] = 0;
        this.vy[i] = 0;
      }
    }

    this.#initLinks();
    this.#initGroups();
    if (reheat) this.alpha = 1;
  }

  setGroups(groupId: Int32Array, linkStrength: Float32Array, reheat = true): void {
    this.groupId = groupId;
    this.linkStrength = linkStrength;
    this.#initLinks();
    this.#initGroups();
    if (reheat) this.alpha = 1;
  }

  #initLinks(): void {
    const m = this.linkFrom.length;
    const deg = new Int32Array(this.count);
    for (let i = 0; i < m; i++) {
      const a = this.linkFrom[i] ?? 0;
      const b = this.linkTo[i] ?? 0;
      deg[a] = (deg[a] ?? 0) + 1;
      deg[b] = (deg[b] ?? 0) + 1;
    }
    this.linkBias = new Float32Array(m);
    this.linkWeight = new Float32Array(m);
    for (let i = 0; i < m; i++) {
      const a = this.linkFrom[i] ?? 0;
      const b = this.linkTo[i] ?? 0;
      const da = deg[a] ?? 1;
      const db = deg[b] ?? 1;
      this.linkBias[i] = da / (da + db);
      this.linkWeight[i] = (1 / Math.min(da, db)) * (this.linkStrength[i] ?? 0.6);
    }
  }

  #initGroups(): void {
    const byGroup = new Map<number, number[]>();
    for (let i = 0; i < this.count; i++) {
      const g = this.groupId[i] ?? -1;
      if (g < 0) continue;
      let list = byGroup.get(g);
      if (!list) {
        list = [];
        byGroup.set(g, list);
      }
      list.push(i);
    }
    this.#groups = [...byGroup.values()];
  }

  readonly #rand = (): number => {
    this.#seed = (this.#seed * 1_664_525 + 1_013_904_223) | 0;
    return (this.#seed >>> 0) / 4_294_967_296 - 0.5;
  };

  /** Advance one tick; returns true while still cooling. */
  tick(): boolean {
    if (this.alpha < FORCE_ALPHA_MIN || this.count === 0) {
      this.alpha = 0;
      return false;
    }
    const alpha = this.alpha;
    this.alpha += (0 - this.alpha) * FORCE_ALPHA_DECAY;

    this.#tree.build(this.count, this.x, this.y, this.#rand);
    this.#tree.applyRepulsion(
      this.count,
      this.x,
      this.y,
      this.vx,
      this.vy,
      FORCE_CHARGE * alpha,
      DISTANCE_MAX2,
      this.#rand,
    );
    this.#applyLinks(alpha);
    this.#applyGravity(alpha);
    if (this.#groups.length > 0) this.#applyCluster(alpha);

    const n = this.count;
    const x = this.x;
    const y = this.y;
    const vx = this.vx;
    const vy = this.vy;
    for (let i = 0; i < n; i++) {
      const nvx = (vx[i] ?? 0) * VELOCITY_KEEP;
      const nvy = (vy[i] ?? 0) * VELOCITY_KEEP;
      vx[i] = nvx;
      vy[i] = nvy;
      x[i] = (x[i] ?? 0) + nvx;
      y[i] = (y[i] ?? 0) + nvy;
    }

    if (this.alpha < FORCE_ALPHA_MIN) {
      this.alpha = 0;
      return false;
    }
    return true;
  }

  #applyLinks(alpha: number): void {
    const m = this.linkFrom.length;
    const x = this.x;
    const y = this.y;
    const vx = this.vx;
    const vy = this.vy;
    for (let i = 0; i < m; i++) {
      const a = this.linkFrom[i] ?? 0;
      const b = this.linkTo[i] ?? 0;
      let dx = (x[b] ?? 0) + (vx[b] ?? 0) - (x[a] ?? 0) - (vx[a] ?? 0);
      let dy = (y[b] ?? 0) + (vy[b] ?? 0) - (y[a] ?? 0) - (vy[a] ?? 0);
      if (dx === 0 && dy === 0) {
        dx = this.#rand() * 0.1;
        dy = this.#rand() * 0.1;
      }
      const l = Math.hypot(dx, dy) || 1e-6;
      const k = ((l - FORCE_LINK_DISTANCE) / l) * alpha * (this.linkWeight[i] ?? 0.6);
      dx *= k;
      dy *= k;
      const bias = this.linkBias[i] ?? 0.5;
      vx[b] = (vx[b] ?? 0) - dx * bias;
      vy[b] = (vy[b] ?? 0) - dy * bias;
      vx[a] = (vx[a] ?? 0) + dx * (1 - bias);
      vy[a] = (vy[a] ?? 0) + dy * (1 - bias);
    }
  }

  #applyGravity(alpha: number): void {
    const n = this.count;
    const k = FORCE_GRAVITY * alpha;
    const x = this.x;
    const y = this.y;
    const vx = this.vx;
    const vy = this.vy;
    for (let i = 0; i < n; i++) {
      vx[i] = (vx[i] ?? 0) - (x[i] ?? 0) * k;
      vy[i] = (vy[i] ?? 0) - (y[i] ?? 0) * k;
    }
  }

  #applyCluster(alpha: number): void {
    const groups = this.#groups;
    const gLen = groups.length;
    const gx = new Float64Array(gLen);
    const gy = new Float64Array(gLen);
    const gSize = new Int32Array(gLen);
    const gRad = new Float64Array(gLen);
    const shiftX = new Float64Array(gLen);
    const shiftY = new Float64Array(gLen);

    const x = this.x;
    const y = this.y;
    for (let gi = 0; gi < gLen; gi++) {
      const members = groups[gi];
      if (!members) continue;
      let sx = 0;
      let sy = 0;
      for (let m = 0; m < members.length; m++) {
        const idx = members[m] ?? 0;
        sx += x[idx] ?? 0;
        sy += y[idx] ?? 0;
      }
      const sz = members.length;
      gx[gi] = sx / sz;
      gy[gi] = sy / sz;
      gSize[gi] = sz;
      gRad[gi] = CLUSTER_SPACING * Math.sqrt(sz);
    }

    const cells = new Map<number, number[]>();
    const seen = new Int32Array(gLen).fill(-1);
    for (let j = 0; j < gLen; j++) {
      const cx = gx[j] ?? 0;
      const cy = gy[j] ?? 0;
      const r = gRad[j] ?? CLUSTER_SPACING;
      const c0 = Math.floor((cx - r) / CELL_PX);
      const c1 = Math.floor((cx + r) / CELL_PX);
      const r0 = Math.floor((cy - r) / CELL_PX);
      const r1 = Math.floor((cy + r) / CELL_PX);
      for (let col = c0; col <= c1; col++) {
        for (let row = r0; row <= r1; row++) {
          const key = col * CELL_KEY_STRIDE + row;
          const cell = cells.get(key);
          if (cell) {
            for (let k = 0; k < cell.length; k++) {
              const i = cell[k] ?? 0;
              if (seen[i] === j) continue;
              seen[i] = j;
              const dx = cx - (gx[i] ?? 0);
              const dy = cy - (gy[i] ?? 0);
              const dist = Math.hypot(dx, dy);
              const overlap = (gRad[i] ?? 0) + r - dist;
              if (overlap <= 0) continue;
              let ux: number;
              let uy: number;
              if (dist > 0) {
                ux = dx / dist;
                uy = dy / dist;
              } else {
                const ang = (i + j) * GOLDEN_ANGLE;
                ux = Math.cos(ang);
                uy = Math.sin(ang);
              }
              const si = gSize[i] ?? 1;
              const sj = gSize[j] ?? 1;
              const push = (CLUSTER_PUSH * overlap) / (si + sj);
              shiftX[i] = (shiftX[i] ?? 0) - ux * push * sj;
              shiftY[i] = (shiftY[i] ?? 0) - uy * push * sj;
              shiftX[j] = (shiftX[j] ?? 0) + ux * push * si;
              shiftY[j] = (shiftY[j] ?? 0) + uy * push * si;
            }
            cell.push(j);
          } else {
            cells.set(key, [j]);
          }
        }
      }
    }

    const vx = this.vx;
    const vy = this.vy;
    for (let gi = 0; gi < gLen; gi++) {
      const members = groups[gi];
      if (!members) continue;
      const cx = gx[gi] ?? 0;
      const cy = gy[gi] ?? 0;
      const sx = shiftX[gi] ?? 0;
      const sy = shiftY[gi] ?? 0;
      for (let m = 0; m < members.length; m++) {
        const idx = members[m] ?? 0;
        vx[idx] = (vx[idx] ?? 0) + ((cx - (x[idx] ?? 0)) * CLUSTER_PULL + sx) * alpha;
        vy[idx] = (vy[idx] ?? 0) + ((cy - (y[idx] ?? 0)) * CLUSTER_PULL + sy) * alpha;
      }
    }
  }
}
