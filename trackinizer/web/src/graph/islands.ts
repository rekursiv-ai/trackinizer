import type { Graph } from "../api/graph";
import { micros } from "../live/rows";

export type Islands = {
  /** Record -> the island it is drawn in. An island's key names it: see `headless`. */
  homes: Map<string, string>;
  /** Keys of islands whose keying record heads nothing in its own family: a ball of
   * records nobody built on, gathered only because they happened while a real
   * inquiry was alive. It is not a line of inquiry, so no title of its own may be
   * worn as one. */
  headless: Set<string>;
};

/**
 * The design's field of islands: one compact ball per line of inquiry, as many
 * as the window holds and none of them a giant. A line of inquiry is a family —
 * a record with nothing above it in the window and everything hung off it: what
 * narrows it, what those produced, down to the leaves. A family the size cap
 * outgrows is cut into consecutive bands of its own spread, each a sub-inquiry;
 * a family too small to read as an island joins the neighbour its records touch
 * most, or touching nothing, the island whose records were alive when it
 * happened — so the field stays a field of balls, never a haze of specks.
 */
export function islandHomes({ nodes, edges }: Graph): Islands {
  const childrenOf = new Map<string, string[]>();
  const parentOf = new Map<string, string>();
  const kin = new Map<string, string[]>();
  const adj = new Map<string, string[]>();
  /** Who points at each record, by any edge: the graph's own sense of built on. */
  const pointedAt = new Map<string, string[]>();
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const headless = new Set<string>();
  // The server hangs a produced_by beside every narrows and every requires
  // between two Issues, and a requires across lines of inquiry is common:
  // counting its companion as kin would sew the threads into one family. So a
  // produced_by between two Issues binds only where the pair holds no such
  // companion — no narrows, no requires — and stands as producing on its own.
  const bound = new Set<string>();
  for (const edge of edges) {
    if (edge.edge_kind === "narrows" || edge.edge_kind === "requires") {
      bound.add(`${edge.from_id}|${edge.to_id}`);
      bound.add(`${edge.to_id}|${edge.from_id}`);
    }
  }
  const kinship = (edge: Graph["edges"][number]): boolean =>
    edge.edge_kind === "narrows"
      ? true
      : edge.edge_kind === "produced_by" &&
        !(
          byId.get(edge.from_id)?.kind === "Issue" &&
          byId.get(edge.to_id)?.kind === "Issue" &&
          bound.has(`${edge.from_id}|${edge.to_id}`)
        );
  for (const edge of edges) {
    if (kinship(edge)) {
      bump(childrenOf, edge.to_id, edge.from_id);
      bump(kin, edge.to_id, edge.from_id);
      bump(kin, edge.from_id, edge.to_id);
      if (!parentOf.has(edge.from_id)) parentOf.set(edge.from_id, edge.to_id);
    }
    bump(adj, edge.from_id, edge.to_id);
    bump(adj, edge.to_id, edge.from_id);
    // Edges are stored child to parent, so `to_id` is the end being built on.
    bump(pointedAt, edge.to_id, edge.from_id);
  }
  // The families: what hangs together by narrowing and producing, within the
  // window. Each takes its crown from its top-most record — the one nothing in
  // the window is above — so an island is named by its own line of inquiry.
  const topOf = new Map<string, string>();
  for (const node of nodes) {
    if (topOf.has(node.id)) continue;
    const members = [node.id];
    topOf.set(node.id, node.id);
    for (let head = 0; head < members.length; head++) {
      for (const other of kin.get(members[head]!) ?? []) {
        if (topOf.has(other)) continue;
        topOf.set(other, node.id);
        members.push(other);
      }
    }
    // The crown is the head of the line of inquiry: nothing above it in the
    // window and the family built on it. A loose satellite -- a paper or a result
    // the generator left unattached, which the design does to a third of them --
    // has nothing above it either, but nobody built on it, so it never names the
    // island it was absorbed into.
    //
    // Built on means narrowed or produced, and it also means cited: a claim with
    // twenty papers proving it is the most built-on record a graph can hold, and
    // it has no children at all. Reading kinship alone, a graph made of evidence
    // rather than of tasks -- which is most of a research corpus -- came out as a
    // field of nameless balls, each one headed by the very claim its island was
    // gathered around.
    const mine = new Set(members);
    const builtOn = (id: string): boolean =>
      (childrenOf.get(id)?.length ?? 0) > 0 || (pointedAt.get(id) ?? []).some((from) => mine.has(from));
    let crown = "";
    let crownAt = -1;
    for (const headed of [true, false]) {
      for (const id of members) {
        if (parentOf.has(id)) continue;
        if (headed && !builtOn(id)) continue;
        const at = micros(byId.get(id)?.created);
        if (at > crownAt) [crown, crownAt] = [id, at];
      }
      if (crown !== "") break;
    }
    if (crown === "") {
      for (const id of members) {
        const at = micros(byId.get(id)?.created);
        if (at > crownAt) [crown, crownAt] = [id, at];
      }
    }
    // The other half of that promise, kept: a satellite that was picked only
    // because the family has no head may not name the island it ends up in.
    if (!builtOn(crown)) headless.add(crown);
    for (const id of members) topOf.set(id, crown);
  }
  const membersOf = new Map<string, string[]>();
  for (const [id, key] of topOf) bump(membersOf, key, id);
  const refined = new Map<string, string>();
  const cap = islandCap(nodes.length);
  const least = leastIsland(nodes.length);
  for (const [key, members] of membersOf) {
    for (const [islandKey, row] of split(key, members, childrenOf, cap)) {
      if (headless.has(key)) headless.add(islandKey);
      for (const id of row) refined.set(id, islandKey);
    }
  }
  const parent = new Map<string, string>();
  const find = (key: string): string => {
    let top = key;
    while ((parent.get(top) ?? top) !== top) top = parent.get(top)!;
    for (let cur = key; cur !== top; cur = parent.get(cur)!) parent.set(cur, top);
    return top;
  };
  const size = new Map<string, number>();
  for (const key of refined.values()) {
    parent.set(key, key);
    size.set(key, (size.get(key) ?? 0) + 1);
  }
  // Both ways round. An edge is stored child to parent, and only the child's end
  // was counted, so an island reached by its citations alone — which is what a
  // claim is — registered as touching nothing and was handed to whatever happened
  // nearest it in time instead of to the papers that cite it. Which end of an edge
  // was written down is not a fact about the graph.
  const touch = new Map<string, Map<string, number>>();
  const meets = (from: string, to: string) => {
    const row = touch.get(from) ?? new Map<string, number>();
    row.set(to, (row.get(to) ?? 0) + 1);
    touch.set(from, row);
  };
  for (const edge of edges) {
    const [ga, gb] = [refined.get(edge.from_id), refined.get(edge.to_id)];
    if (ga === undefined || gb === undefined || ga === gb) continue;
    meets(ga, gb);
    meets(gb, ga);
  }
  const islands = (): string[] => [...size.keys()].filter((key) => find(key) === key);
  const window = new Map<string, [number, number]>();
  for (const [id, key] of refined) {
    const at = micros(byId.get(id)?.created);
    const row = window.get(key) ?? [at, at];
    window.set(key, [Math.min(row[0], at), Math.max(row[1], at)]);
  }
  // A family that touches nothing still happened: it joins the island whose
  // records were alive closest to when it did.
  const nearest = (small: string, ranked: string[]): string => {
    const [first, last] = window.get(small) ?? [0, 0];
    let [into, gap] = [ranked[ranked.length - 1]!, Infinity];
    // Biggest first: at any moment several islands are alive, so the closest is
    // usually a tie, and a speck that joins a speck is how a haze of small
    // balls starts. The largest of the tied is the one that reads as an island.
    for (let at = ranked.length - 1; at >= 0; at--) {
      const key = ranked[at]!;
      if (key === small) continue;
      const [start, end] = window.get(key) ?? [0, 0];
      const distance = last < start ? start - last : first > end ? first - end : 0;
      if (distance < gap) [into, gap] = [key, distance];
    }
    return into;
  };
  const absorb = (small: string, fallback: string, into?: string) => {
    // The neighbour its records touch most, but only among those that already
    // read as an island: hand a speck to a speck and the two snowball, and the
    // ball they grow into lands wherever the default puts it -- one giant among
    // the rest. A speck joins a ball, never another speck, if any ball touches.
    const per = new Map<string, number>();
    const spare = new Map<string, number>();
    for (const [other, count] of touch.get(small) ?? []) {
      const rep = find(other);
      if (rep === small) continue;
      const row = size.get(rep)! >= least ? per : spare;
      row.set(rep, (row.get(rep) ?? 0) + count);
    }
    const home = into ?? busiest(per) ?? busiest(spare) ?? fallback;
    parent.set(small, home);
    size.set(home, size.get(home)! + size.get(small)!);
    size.delete(small);
    const rowSmall = touch.get(small);
    if (rowSmall) {
      const rowInto = touch.get(home) ?? new Map<string, number>();
      for (const [other, count] of rowSmall) rowInto.set(other, (rowInto.get(other) ?? 0) + count);
      // Neither end of the merged row may be the island itself: a self-link
      // would make the island look like it touches nothing, and what touches
      // nothing goes to the biggest by default -- so every island it had
      // absorbed would have gone there too, one after the other.
      rowInto.delete(small);
      rowInto.delete(home);
      touch.set(home, rowInto);
    }
    touch.delete(small);
  };
  // The design's field is a handful of big balls, not a haze of small ones.
  // The seed keeps that promise now -- its threads live in time bands, so each
  // window holds whole ones -- so only a family too small to read as an island
  // gives itself up: to the neighbour its records touch most, or failing any
  // touch, to the island whose records were alive when it happened.
  /** How much of the graph meets an island: what decides a hub from a leaf at equal size. */
  const pull = (key: string): number => {
    let all = 0;
    for (const count of touch.get(key)?.values() ?? []) all += count;
    return all;
  };
  for (;;) {
    // Smallest first, and at equal size the least connected first. Without that
    // tiebreak the order is arbitrary among specks, and a graph of citations is
    // all specks to begin with — nothing is kin across a citation — so the claim
    // at the hub could be absorbed into one of the twenty papers citing it and
    // the island came out wearing a paper's title. Leaves go into hubs; a hub
    // has grown past the floor by the time its own turn would have come.
    const ranked = islands().sort((a, b) => size.get(a)! - size.get(b)! || pull(a) - pull(b));
    if (ranked.length < 2) break;
    const small = ranked.find((key) => size.get(key)! < least);
    if (small === undefined) break;
    absorb(small, (touch.get(small)?.size ?? 0) > 0 ? ranked[ranked.length - 1]! : nearest(small, ranked));
  }
  const homes = new Map([...refined].map(([id, key]) => [id, find(key)]));
  // Whether a head was built on is asked of the island it ended up as, not of the
  // kinship family it started from. The two are rarely the same: the papers that
  // prove a claim are not kin to it — nothing narrows or produces across a
  // citation — so each starts as a family of its own and they become one island
  // only here, in absorption. Asked too early, the claim at the centre of twenty
  // papers looked like a record nobody had built on, and its island went unnamed.
  const held = new Map<string, Set<string>>();
  for (const [id, key] of homes) (held.get(key) ?? held.set(key, new Set()).get(key)!).add(id);
  const nameless = new Set<string>();
  for (const [key, mine] of held) {
    const built = (childrenOf.get(key)?.length ?? 0) > 0
      || (pointedAt.get(key) ?? []).some((from) => mine.has(from));
    if (!built) nameless.add(key);
  }
  return { homes, headless: nameless };
}

/**
 * The most of the window one island may hold before it is cut into bands. A
 * share rather than a count: what makes a field unreadable is not an island of
 * some particular size, it is one island being the field and the rest a fringe
 * around it, and that is a question about the window the reader asked for — so
 * widening the window lets a thread grow with it instead of hitting a number
 * chosen against some other graph's biggest thread.
 */
const ISLAND_SHARE = 0.4;
/**
 * …but a narrow window is often one line of inquiry and nothing else, where a
 * share would cut a whole thread into bands and invent structure that is not
 * there. Below this no family is split at any share.
 */
const SPLIT_FLOOR = 240;

/** The cap a window of `total` records puts on one island. */
const islandCap = (total: number): number => Math.max(SPLIT_FLOOR, Math.ceil(total * ISLAND_SHARE));
/**
 * What counts as too small to read as an island, as a share of the window, for
 * the same reason the cap is one: a count tuned on a five-thousand-record view
 * is most of a hundred-record one, so an absolute floor swallows every real
 * thread a small graph has and draws the whole of it as a single ball. Eight
 * families of forty read as eight islands either way; four families of eight
 * read as one island under a floor of 24 and as four under a share.
 */
const MIN_ISLAND_SHARE = 0.08;
/** The share's ceiling: past this a window is large enough that 24 reads as a speck. */
const MIN_ISLAND_CEILING = 24;
/** …and its floor: a pair of records is not a line of inquiry at any scale. */
const MIN_ISLAND_FLOOR = 3;

/** The least an island may hold in a window of `total` records. */
const leastIsland = (total: number): number =>
  Math.min(MIN_ISLAND_CEILING, Math.max(MIN_ISLAND_FLOOR, Math.ceil(total * MIN_ISLAND_SHARE)));

/**
 * Cut one family into islands of at most the cap: the breadth-first spread
 * from its crown, chunked, so each island is a connected band of the inquiry —
 * a sub-issue and what hangs off it — and all of them come out much of a size.
 */
function split(key: string, members: string[], childrenOf: Map<string, string[]>, cap: number): [string, string[]][] {
  if (members.length <= cap) return [[key, members]];
  const memberSet = new Set(members);
  const depth = new Map([[key, 0]]);
  const queue = [key];
  for (let head = 0; head < queue.length; head++) {
    const cur = queue[head]!;
    for (const child of childrenOf.get(cur) ?? []) {
      if (!memberSet.has(child) || depth.has(child)) continue;
      depth.set(child, depth.get(cur)! + 1);
      queue.push(child);
    }
  }
  const ordered = [...members].sort((a, b) => (depth.get(a) ?? members.length) - (depth.get(b) ?? members.length));
  const chunks = Math.ceil(ordered.length / cap);
  const per = Math.ceil(ordered.length / chunks);
  const pieces: [string, string[]][] = [];
  for (let at = 0; at < ordered.length; at += per) {
    const row = ordered.slice(at, at + per);
    pieces.push([row[0]!, row]);
  }
  return pieces;
}

function bump<K>(map: Map<K, string[]>, key: K, value: string): Map<K, string[]> {
  const row = map.get(key) ?? [];
  row.push(value);
  map.set(key, row);
  return map;
}

function busiest<K>(row: Map<K, number> | undefined): K | null {
  let [best, count] = [null as K | null, 0];
  for (const [key, n] of row ?? []) if (n > count) [best, count] = [key, n];
  return best;
}
