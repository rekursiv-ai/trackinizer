const MAX_Q_INIT = 8192;
const THETA2 = 0.9 * 0.9;

/** Flat typed-array Barnes-Hut quadtree for 2D many-body repulsion. */
export class BarnesHutTree {
  #cap = MAX_Q_INIT;
  #cx = new Float64Array(MAX_Q_INIT);
  #cy = new Float64Array(MAX_Q_INIT);
  #half = new Float64Array(MAX_Q_INIT);
  #mass = new Float64Array(MAX_Q_INIT);
  #mx = new Float64Array(MAX_Q_INIT);
  #my = new Float64Array(MAX_Q_INIT);
  #child = new Int32Array(MAX_Q_INIT * 4);
  #leaf = new Int32Array(MAX_Q_INIT);
  #stack = new Int32Array(256);

  #ensureCap(needed: number): void {
    if (needed <= this.#cap) return;
    const nextCap = Math.max(needed, this.#cap * 2);
    const grow64 = (old: Float64Array) => {
      const arr = new Float64Array(nextCap);
      arr.set(old);
      return arr;
    };
    const grow32 = (old: Int32Array, mult: number, fill: number) => {
      const arr = new Int32Array(nextCap * mult);
      if (fill !== 0) arr.fill(fill);
      arr.set(old);
      return arr;
    };
    this.#cx = grow64(this.#cx);
    this.#cy = grow64(this.#cy);
    this.#half = grow64(this.#half);
    this.#mass = grow64(this.#mass);
    this.#mx = grow64(this.#mx);
    this.#my = grow64(this.#my);
    this.#child = grow32(this.#child, 4, -1);
    this.#leaf = grow32(this.#leaf, 1, -1);
    this.#cap = nextCap;
  }

  build(count: number, x: Float32Array, y: Float32Array, jitter: () => number): void {
    let minX = Infinity;
    let maxX = -Infinity;
    let minY = Infinity;
    let maxY = -Infinity;
    for (let i = 0; i < count; i++) {
      const xi = x[i] ?? 0;
      const yi = y[i] ?? 0;
      if (xi < minX) minX = xi;
      if (xi > maxX) maxX = xi;
      if (yi < minY) minY = yi;
      if (yi > maxY) maxY = yi;
    }
    const span = Math.max(maxX - minX, maxY - minY, 1);
    this.#child.fill(-1, 0, Math.min(this.#cap * 4, Math.max(count * 16, 64)));
    let qCount = 1;
    this.#cx[0] = (minX + maxX) / 2;
    this.#cy[0] = (minY + maxY) / 2;
    this.#half[0] = span / 2 + 1;
    this.#mass[0] = 0;
    this.#mx[0] = 0;
    this.#my[0] = 0;
    this.#leaf[0] = -1;

    const allocNode = (cx: number, cy: number, half: number): number => {
      const idx = qCount++;
      this.#ensureCap(qCount);
      this.#cx[idx] = cx;
      this.#cy[idx] = cy;
      this.#half[idx] = half;
      this.#mass[idx] = 0;
      this.#mx[idx] = 0;
      this.#my[idx] = 0;
      this.#leaf[idx] = -1;
      const base = idx * 4;
      this.#child[base] = -1;
      this.#child[base + 1] = -1;
      this.#child[base + 2] = -1;
      this.#child[base + 3] = -1;
      return idx;
    };

    const insert = (
      nodeIdx: number,
      bodyIdx: number,
      bx: number,
      by: number,
      depth: number,
    ) => {
      const mass = this.#mass[nodeIdx] ?? 0;
      this.#mass[nodeIdx] = mass + 1;
      this.#mx[nodeIdx] = (this.#mx[nodeIdx] ?? 0) + bx;
      this.#my[nodeIdx] = (this.#my[nodeIdx] ?? 0) + by;

      const existing = this.#leaf[nodeIdx] ?? -1;
      if (mass === 0) {
        this.#leaf[nodeIdx] = bodyIdx;
        return;
      }
      if (depth > 24) return;
      if (existing >= 0) {
        this.#leaf[nodeIdx] = -1;
        const ex = x[existing] ?? 0;
        const ey = y[existing] ?? 0;
        if (Math.abs(ex - bx) < 1e-4 && Math.abs(ey - by) < 1e-4) {
          bx += jitter() * 0.01;
          by += jitter() * 0.01;
        }
        const half = (this.#half[nodeIdx] ?? 1) * 0.5;
        const cx = this.#cx[nodeIdx] ?? 0;
        const cy = this.#cy[nodeIdx] ?? 0;
        const q1 = (ex >= cx ? 1 : 0) + (ey >= cy ? 2 : 0);
        let c1 = this.#child[nodeIdx * 4 + q1] ?? -1;
        if (c1 < 0) {
          c1 = allocNode(
            cx + (q1 & 1 ? half : -half),
            cy + (q1 & 2 ? half : -half),
            half,
          );
          this.#child[nodeIdx * 4 + q1] = c1;
        }
        insert(c1, existing, ex, ey, depth + 1);
      }

      const half = (this.#half[nodeIdx] ?? 1) * 0.5;
      const cx = this.#cx[nodeIdx] ?? 0;
      const cy = this.#cy[nodeIdx] ?? 0;
      const q2 = (bx >= cx ? 1 : 0) + (by >= cy ? 2 : 0);
      let c2 = this.#child[nodeIdx * 4 + q2] ?? -1;
      if (c2 < 0) {
        c2 = allocNode(
          cx + (q2 & 1 ? half : -half),
          cy + (q2 & 2 ? half : -half),
          half,
        );
        this.#child[nodeIdx * 4 + q2] = c2;
      }
      insert(c2, bodyIdx, bx, by, depth + 1);
    };

    for (let i = 0; i < count; i++) {
      insert(0, i, x[i] ?? 0, y[i] ?? 0, 0);
    }
  }

  applyRepulsion(
    count: number,
    x: Float32Array,
    y: Float32Array,
    vx: Float32Array,
    vy: Float32Array,
    strengthAlpha: number,
    distanceMax2: number,
    jitter: () => number,
  ): void {
    let stack = this.#stack;
    for (let i = 0; i < count; i++) {
      const xi = x[i] ?? 0;
      const yi = y[i] ?? 0;
      let sp = 0;
      stack[sp++] = 0;
      let fx = 0;
      let fy = 0;
      while (sp > 0) {
        const q = stack[--sp] ?? 0;
        const mass = this.#mass[q] ?? 0;
        if (mass === 0) continue;
        const leaf = this.#leaf[q] ?? -1;
        if (leaf === i) continue;

        const mx = (this.#mx[q] ?? 0) / mass;
        const my = (this.#my[q] ?? 0) / mass;
        let dx = mx - xi;
        let dy = my - yi;
        let d2 = dx * dx + dy * dy;
        const width = (this.#half[q] ?? 0) * 2;

        if (leaf >= 0 || (width * width) / THETA2 < d2) {
          if (d2 >= distanceMax2) continue;
          if (d2 === 0) {
            dx = jitter() * 0.1;
            dy = jitter() * 0.1;
            d2 = dx * dx + dy * dy;
          }
          if (d2 < 1) d2 = Math.sqrt(d2);
          const w = (strengthAlpha * mass) / d2;
          fx += dx * w;
          fy += dy * w;
        } else {
          const base = q * 4;
          for (let k = 0; k < 4; k++) {
            const ch = this.#child[base + k] ?? -1;
            if (ch >= 0) {
              if (sp >= stack.length) {
                const next = new Int32Array(stack.length * 2);
                next.set(stack);
                stack = this.#stack = next;
              }
              stack[sp++] = ch;
            }
          }
        }
      }
      vx[i] = (vx[i] ?? 0) + fx;
      vy[i] = (vy[i] ?? 0) + fy;
    }
  }
}
