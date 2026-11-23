// Naming the islands the grouping makes: where each one sits, and where its name
// goes so that no two names overlap and none is ever dropped.

/** A box in the space the caller measures in; the canvas's is the world's, at the zoom's scale. */
export type Box = { readonly left: number; readonly top: number; readonly right: number; readonly bottom: number };

/** Where one island sits: the middle of its nodes, and how far its body spreads. */
export type Island = {
  readonly key: string;
  readonly x: number;
  readonly y: number;
  /** The radius its body fills, a stray node left out (`BODY_SHARE`). */
  readonly radius: number;
  /** How many of its nodes are drawn, and how many of those are Beliefs. */
  readonly shown: number;
  readonly beliefs: number;
};

/** A node as this module reads it. */
type Placed = { readonly x?: number; readonly y?: number; readonly hidden: boolean; readonly kind: string };

/**
 * Where each group of `nodes` sits, by the key `groupOf` gives it; a node with
 * no group, no place, or hidden counts for nothing, and a group with none of its
 * nodes drawn is left out.
 *
 * A group's radius is the distance that `BODY_SHARE` of its nodes lie within,
 * not the furthest: one node flung out by a link across the graph would
 * otherwise push the island's name far off the body it names.
 */
export function islandsOf(nodes: readonly Placed[], groupOf: (node: Placed) => string | undefined): Island[] {
  const members = new Map<string, Placed[]>();
  for (const node of nodes) {
    if (node.hidden || !Number.isFinite(node.x) || !Number.isFinite(node.y)) continue;
    const key = groupOf(node);
    if (key === undefined) continue;
    const kept = members.get(key) ?? [];
    kept.push(node);
    members.set(key, kept);
  }
  const islands: Island[] = [];
  for (const [key, group] of members) {
    let [x, y, beliefs] = [0, 0, 0];
    for (const node of group) {
      x += node.x!;
      y += node.y!;
      if (node.kind === "Belief") beliefs += 1;
    }
    [x, y] = [x / group.length, y / group.length];
    const spread = group.map((node) => Math.hypot(node.x! - x, node.y! - y)).sort((a, b) => a - b);
    islands.push({ key, x, y, radius: spread[Math.floor((spread.length - 1) * BODY_SHARE)]!, shown: group.length, beliefs });
  }
  return islands;
}

/** One island's name, placed: where to draw it, the island it names, and whether a place was free. */
export type Placement = { readonly island: Island; readonly x: number; readonly y: number; readonly box: Box; readonly placed: boolean };

/**
 * Where to draw each island's name, the largest island first so that the
 * islands most worth reading take the best places.
 *
 * A name goes above its island, and when that place is taken, below it, beside
 * it, then further out. `placed` says whether one of those places was free: the
 * caller can drop a name that fits nowhere, rather than draw it over a
 * neighbour's — which would read as one wrong name instead of a missing one.
 * Zooming in spreads the islands on screen, and the dropped names come back.
 *
 * `width` measures a name, `gap` is the space to keep clear around one, and
 * `height` how tall one is, the caller's units throughout, so the canvas can
 * work in world units at the zoom's scale and a test in plain numbers.
 */
export function placeLabels(
  islands: readonly Island[],
  width: (island: Island) => number,
  { height, gap, taken = [] }: { height: number; gap: number; taken?: readonly Box[] },
): Placement[] {
  const used = [...taken];
  const placements: Placement[] = [];
  for (const island of [...islands].sort((a, b) => b.shown - a.shown)) {
    const half = width(island) / 2;
    const out = island.radius + gap;
    // Above, below, beside, then further above, below and out: a reader looks
    // over an island's top first, and a name beside one is the last that still
    // reads as its. The far ring keeps a crowded view from stacking names on a
    // neighbour's, which reads as one wrong name rather than six right ones.
    const spots: [number, number][] = [
      [island.x, island.y - out - height],
      [island.x, island.y + out],
      [island.x - out - half, island.y - height / 2],
      [island.x + out + half, island.y - height / 2],
      [island.x, island.y - out - height * 2.4],
      [island.x, island.y + out + height * 1.4],
      [island.x - out - half - gap * 3, island.y - height / 2],
      [island.x + out + half + gap * 3, island.y - height / 2],
      [island.x, island.y - out - height * 3.8],
      [island.x, island.y + out + height * 2.8],
    ];
    const boxOf = ([x, y]: [number, number]): Box => ({ left: x - half - gap, top: y - gap, right: x + half + gap, bottom: y + height + gap });
    const free = spots.find((spot) => !used.some((other) => overlaps(boxOf(spot), other)));
    const [x, y] = free ?? spots[0]!;
    used.push(boxOf([x, y]));
    placements.push({ island, x, y, box: boxOf([x, y]), placed: free !== undefined });
  }
  return placements;
}

function overlaps(a: Box, b: Box): boolean {
  return a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
}

/** The share of an island's nodes its radius covers: the body, without the strays. */
const BODY_SHARE = 0.9;
