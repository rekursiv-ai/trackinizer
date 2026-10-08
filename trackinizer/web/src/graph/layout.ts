// The design prototype's layout engine (server's graph.html, `initLayout` and
// `step`), ported as typed, framework-free code. It is not d3's: communities are
// packed first, as discs the margin 150 keeps apart, and every node then springs
// a short per-kind spring to its neighbours while a weak pull holds it to its
// community's centre. Repulsion is local — a 32-px cell of neighbours — so a
// node is only ever pushed by the ball it sits in, which is what packs an island
// tight enough to crown with its name.

/** A body the layout moves: d3's shape, so a renderer's nodes fit it as they are. */
export type Body = { x?: number; y?: number; vx?: number; vy?: number };

/** One community: its members, and where the packed centres put it. */
type Region = { readonly members: number[]; x: number; y: number; radius: number };

/** What the layout runs on: bodies, the community of each, and the links between them. */
export type LayoutInput<B extends Body> = {
  readonly nodes: readonly B[];
  /** Each node's community, by its index in `nodes`; a node without one keeps to itself. */
  readonly communityOf: (node: B) => string | undefined;
  /** Each link's ends by index, its kind, and how many distinct links touch each end. */
  readonly links: readonly { readonly source: number; readonly target: number; readonly kind: string }[];
  readonly radiusOf: (node: B) => number;
};

/** A running layout: call `tick` until it says it has settled. */
export type Layout = { tick(): boolean };

/** The spring lengths, by edge kind: the design's, the citation that decides shortest. */
const SPRING: { [kind: string]: number } = { proves: 18, favors: 24, produced_by: 26, narrows: 30, requires: 34, supersedes: 22, cites_paper: 38 };
/** The springs' strengths, by edge kind; between communities they all but vanish. */
const STRENGTH: { [kind: string]: number } = { proves: 0.09, favors: 0.06, produced_by: 0.05, narrows: 0.05, requires: 0.03, supersedes: 0.06, cites_paper: 0.025 };
/** The room a community of `n` nodes claims: its disc, before the centres are packed. */
const regionRadius = (n: number): number => 13 * Math.sqrt(n) + 30;
/** The clear space between two communities' discs, for the names that crown them. */
const CLEARANCE = 150;
/** The pull that holds a node to its community's centre, per tick. */
const GRAVITY = 0.006;
/** The repulsion cell: a node feels only the neighbours within a cell and its own. */
const CELL = 32;
/** The packing's passes over the community centres, before any node is placed. */
const PACK_PASSES = 500;

/**
 * Lay `input` out: pack the communities' centres, scatter each node in its own
 * community's disc, and return the layout to `tick` until settled. Deterministic
 * where it can be: the centres take a sunflower spiral, so the same graph lays
 * out the same.
 */
export function layout<B extends Body>(input: LayoutInput<B>, random: () => number = Math.random): Layout {
  const { nodes, communityOf, links, radiusOf } = input;
  // The communities, in first-seen order.
  const regions = new Map<string, Region>();
  const communityOfIndex: (string | undefined)[] = nodes.map(communityOf);
  nodes.forEach((_, index) => {
    const community = communityOfIndex[index];
    if (community === undefined) return;
    let region = regions.get(community);
    if (!region) regions.set(community, (region = { members: [], x: 0, y: 0, radius: 0 }));
    region.members.push(index);
  });
  // The centres on a sunflower spiral, then packed: overlap pushed apart, links
  // between communities pulling gently together, the whole shrinking a breath a pass.
  const packed = [...regions.values()];
  packed.forEach((region, index) => {
    region.radius = regionRadius(region.members.length);
    const angle = index * 2.39996;
    const distance = 95 * Math.sqrt(index + 0.6);
    region.x = Math.cos(angle) * distance;
    region.y = Math.sin(angle) * distance;
  });
  const bundles = new Map<string, number>();
  for (const link of links) {
    const [from, to] = [communityOfIndex[link.source], communityOfIndex[link.target]];
    if (from === undefined || to === undefined || from === to) continue;
    const key = from < to ? `${from}|${to}` : `${to}|${from}`;
    bundles.set(key, (bundles.get(key) ?? 0) + 1);
  }
  for (let pass = 0; pass < PACK_PASSES; pass++) {
    for (let i = 0; i < packed.length; i++) {
      for (let j = i + 1; j < packed.length; j++) {
        const a = packed[i]!;
        const b = packed[j]!;
        const [dx, dy] = [b.x - a.x, b.y - a.y];
        const distance = Math.hypot(dx, dy) || 1;
        const wanted = a.radius + b.radius + CLEARANCE;
        if (distance < wanted) {
          const push = (wanted - distance) * 0.5 / distance;
          a.x -= dx * push;
          a.y -= dy * push;
          b.x += dx * push;
          b.y += dy * push;
        }
      }
    }
    for (const [key, count] of bundles) {
      const [aKey, bKey] = key.split("|");
      const a = regions.get(aKey!)!;
      const b = regions.get(bKey!)!;
      const pull = 0.0009 * Math.sqrt(count);
      a.x += (b.x - a.x) * pull;
      a.y += (b.y - a.y) * pull;
      b.x -= (b.x - a.x) * pull;
      b.y -= (b.y - a.y) * pull;
    }
    for (const region of packed) {
      region.x *= 0.992;
      region.y *= 0.992;
    }
  }
  // Scatter each node in its community's disc, at rest; the ticks pack the balls.
  for (const region of packed) {
    for (const index of region.members) {
      const angle = random() * Math.PI * 2;
      const distance = Math.sqrt(random()) * region.radius * 0.95;
      nodes[index]!.x = region.x + Math.cos(angle) * distance;
      nodes[index]!.y = region.y + Math.sin(angle) * distance;
      nodes[index]!.vx = 0;
      nodes[index]!.vy = 0;
    }
  }
  // The springs, once: length by kind, padded by the busier end's degree; a
  // link between communities holds with a twentieth of its strength.
  const degree = new Float32Array(nodes.length);
  for (const link of links) {
    degree[link.source] += 1;
    degree[link.target] += 1;
  }
  const springs = links.map((link) => ({
    source: link.source,
    target: link.target,
    length: (SPRING[link.kind] ?? 30) + 2.6 * Math.sqrt(Math.max(degree[link.source]!, degree[link.target]!, 1)),
    strength:
      (STRENGTH[link.kind] ?? 0.04) *
      (communityOfIndex[link.source] !== communityOfIndex[link.target] ? 0.08 : 1),
  }));
  const communityIndex = communityOfIndex.map((community) => (community === undefined ? -1 : packed.indexOf(regions.get(community)!)));
  let alpha = 1;
  return {
    tick(): boolean {
      if (alpha < 0.03) return false;
      const velocitiesX = new Float32Array(nodes.length);
      const velocitiesY = new Float32Array(nodes.length);
      for (const [index, node] of nodes.entries()) {
        velocitiesX[index] = (node.vx ?? 0) * 0.5;
        velocitiesY[index] = (node.vy ?? 0) * 0.5;
      }
      // Local repulsion, a cell and its eight neighbours: the ball packs round,
      // not on the cell grid. Each unordered pair is met once — cells hold only
      // the indices already placed, all of them earlier than `index`.
      const cells = new Map<number, number[]>();
      const repulsion = 38 * alpha + 6;
      const cellKey = (column: number, row: number): number => (column + 4096) * 8192 + (row + 4096);
      nodes.forEach((node, index) => {
        const column = Math.floor((node.x ?? 0) / CELL);
        const row = Math.floor((node.y ?? 0) / CELL);
        for (let offsetX = -1; offsetX <= 1; offsetX++) {
          for (let offsetY = -1; offsetY <= 1; offsetY++) {
            const neighbours = cells.get(cellKey(column + offsetX, row + offsetY));
            if (!neighbours) continue;
            for (const other of neighbours) {
              let [dx, dy] = [(node.x ?? 0) - (nodes[other]!.x ?? 0), (node.y ?? 0) - (nodes[other]!.y ?? 0)];
              let squared = dx * dx + dy * dy;
              if (squared > CELL * CELL * 2.2) continue;
              if (squared < 0.01) {
                dx = random() - 0.5;
                dy = random() - 0.5;
                squared = 0.5;
              }
              const distance = Math.sqrt(squared);
              const min = radiusOf(node) + radiusOf(nodes[other]!) + 3;
              let force = repulsion / squared;
              if (distance < min) force += (min - distance) / distance * 0.35;
              const [fx, fy] = [dx * force, dy * force];
              velocitiesX[index] += fx;
              velocitiesY[index] += fy;
              velocitiesX[other] -= fx;
              velocitiesY[other] -= fy;
            }
          }
        }
        const key = cellKey(column, row);
        const cell = cells.get(key) ?? [];
        cell.push(index);
        cells.set(key, cell);
      });
      // The springs, then the pull home to the community's centre.
      for (const spring of springs) {
        const a = nodes[spring.source]!;
        const b = nodes[spring.target]!;
        const [dx, dy] = [(b.x ?? 0) - (a.x ?? 0), (b.y ?? 0) - (a.y ?? 0)];
        const distance = Math.hypot(dx, dy) || 1;
        const force = spring.strength * (distance - spring.length) / distance;
        const [fx, fy] = [dx * force, dy * force];
        velocitiesX[spring.source] += fx;
        velocitiesY[spring.source] += fy;
        velocitiesX[spring.target] -= fx;
        velocitiesY[spring.target] -= fy;
      }
      const cap = 14 * alpha + 1.2;
      nodes.forEach((node, index) => {
        const community = communityIndex[index];
        if (community >= 0) {
          const region = packed[community]!;
          velocitiesX[index] += (region.x - (node.x ?? 0)) * GRAVITY;
          velocitiesY[index] += (region.y - (node.y ?? 0)) * GRAVITY;
        }
        const speed = Math.hypot(velocitiesX[index]!, velocitiesY[index]!);
        if (speed > cap) {
          velocitiesX[index] *= cap / speed;
          velocitiesY[index] *= cap / speed;
        }
        node.vx = velocitiesX[index]!;
        node.vy = velocitiesY[index]!;
        node.x = (node.x ?? 0) + velocitiesX[index]!;
        node.y = (node.y ?? 0) + velocitiesY[index]!;
      });
      alpha *= 0.985;
      return true;
    },
  };
}
