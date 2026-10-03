/** A node as the layout moves it: d3 gives each its place and speed. */
export type Body = { x?: number; y?: number; vx?: number; vy?: number };

/** A d3 force: it runs once a tick at the layout's heat, `alpha`, over the nodes `initialize` gave it. */
export type Force<Node> = ((alpha: number) => void) & { initialize: (nodes: Node[]) => void };

/**
 * A force that gathers the nodes into one island per group, for force-graph's
 * `d3Force(name, force)`: `groupOf` names each node's group, read when the
 * layout takes its nodes; a node with none is left alone.
 *
 * Each tick, every node is pulled toward the middle of its group, and two
 * groups that overlap are pushed apart along the line between their middles.
 * A group counts as a disc that grows with the square root of its size, the
 * room its nodes take at the layout's spacing; it moves whole, and the bigger
 * of two moves the less, so a small island gives way to a large one. Groups
 * whose middles coincide part in a direction set by their order, so the same
 * places give the same push. Only groups near each other are checked
 * (`eachNearPair`), so thousands of groups, as thousands of roots over one shared
 * node make, stay fast.
 *
 * With the renderer's forces, on a production graph of 1,038 nodes under 55
 * roots (2026-10-02): once settled, 97% of the nodes in groups of 3 or more lie
 * nearest their own group's middle, against 32% without this force, and 7 of
 * 561 pairs of such groups overlap, against 72. It costs about 0.1 ms a tick.
 */
export function clusterForce<Node extends Body>(groupOf: (node: Node) => string | undefined): Force<Node> {
  let groups: Node[][] = [];
  const force = (alpha: number) => {
    const discs = groups.map(discOf);
    eachNearPair(discs, (i, j) => pushApart(discs[i]!, discs[j]!, (i + j) * GOLDEN_ANGLE));
    for (const [index, members] of groups.entries()) {
      const disc = discs[index]!;
      for (const node of members) {
        node.vx = (node.vx ?? 0) + ((disc.x - (node.x ?? 0)) * PULL + disc.shiftX) * alpha;
        node.vy = (node.vy ?? 0) + ((disc.y - (node.y ?? 0)) * PULL + disc.shiftY) * alpha;
      }
    }
  };
  const initialize = (nodes: Node[]) => {
    const byGroup = new Map<string, Node[]>();
    for (const node of nodes) {
      const group = groupOf(node);
      if (group === undefined) continue;
      const members = byGroup.get(group) ?? [];
      members.push(node);
      byGroup.set(group, members);
    }
    groups = [...byGroup.values()];
  };
  return Object.assign(force, { initialize });
}

/** A group as the force sees it: a disc at its nodes' middle, and the push it has gathered this tick. */
type Disc = { x: number; y: number; size: number; radius: number; shiftX: number; shiftY: number };

function discOf(members: readonly Body[]): Disc {
  let [x, y] = [0, 0];
  for (const node of members) {
    x += node.x ?? 0;
    y += node.y ?? 0;
  }
  const size = members.length;
  return { x: x / size, y: y / size, size, radius: SPACING * Math.sqrt(size), shiftX: 0, shiftY: 0 };
}

/**
 * Call `visit(i, j)`, `i < j`, once for each pair of discs whose bounding boxes
 * share a cell of a grid `CELL_PX` wide: every pair that overlaps, and only
 * pairs near each other. Each disc joins the cells its box covers, a one-node group's 1 to 4,
 * and meets the discs already in them; `seen` marks those it has met, so a pair
 * sharing several cells is visited once. At the layout's first places, checking
 * every pair took 195 ms a tick at 5,000 one-node groups and 3 s at 20,000; the
 * grid takes 9 and 26 ms (Node 24 on the dev Mac, 2026-10-02).
 */
function eachNearPair(discs: readonly Disc[], visit: (i: number, j: number) => void): void {
  const cells = new Map<number, number[]>();
  const seen = new Int32Array(discs.length).fill(-1);
  for (const [j, { x, y, radius }] of discs.entries()) {
    const [left, right] = [Math.floor((x - radius) / CELL_PX), Math.floor((x + radius) / CELL_PX)];
    const [top, bottom] = [Math.floor((y - radius) / CELL_PX), Math.floor((y + radius) / CELL_PX)];
    for (let column = left; column <= right; column++) {
      for (let row = top; row <= bottom; row++) {
        // Two cells that share a key only cost a few extra checks.
        const key = column * CELL_KEY_STRIDE + row;
        const cell = cells.get(key) ?? [];
        for (const i of cell) {
          if (seen[i] === j) continue;
          seen[i] = j;
          visit(i, j);
        }
        cell.push(j);
        cells.set(key, cell);
      }
    }
  }
}

/** Push discs `a` and `b` apart if they overlap, the bigger the less; coincident ones part along `angle`. */
function pushApart(a: Disc, b: Disc, angle: number): void {
  const distance = Math.hypot(b.x - a.x, b.y - a.y);
  const overlap = a.radius + b.radius - distance;
  if (overlap <= 0) return;
  const [unitX, unitY] = distance > 0 ? [(b.x - a.x) / distance, (b.y - a.y) / distance] : [Math.cos(angle), Math.sin(angle)];
  const push = (PUSH * overlap) / (a.size + b.size);
  a.shiftX -= unitX * push * b.size;
  a.shiftY -= unitY * push * b.size;
  b.shiftX += unitX * push * a.size;
  b.shiftY += unitY * push * a.size;
}

/** How hard a node is pulled toward its group's middle, per unit of distance, at full heat. */
const PULL = 0.1;
/** How much of two groups' overlap the push undoes per tick, at full heat. */
const PUSH = 0.5;
/** A group of `n` nodes counts as a disc of radius `SPACING * sqrt(n)`: its nodes' room at the layout's spacing. */
const SPACING = 30;
/** Spreads the directions coincident groups part in, as sunflower seeds are. */
const GOLDEN_ANGLE = Math.PI * (3 - Math.sqrt(5));
/** The neighbour grid's cell: a one-node group's disc across. */
const CELL_PX = 2 * SPACING;
/** Packs a cell's column and row into one key, distinct while rows stay within ±2^25 cells. */
const CELL_KEY_STRIDE = 2 ** 26;
