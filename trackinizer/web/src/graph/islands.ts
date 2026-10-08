import type { Graph } from "../api/graph";
import { micros } from "../live/rows";

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
export function islandHomes({ nodes, edges }: Graph): Map<string, string> {
  const childrenOf = new Map<string, string[]>();
  const parentOf = new Map<string, string>();
  const kin = new Map<string, string[]>();
  const adj = new Map<string, string[]>();
  const byId = new Map(nodes.map((node) => [node.id, node]));
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
    // window and something hung below. A loose satellite -- a paper or a result
    // the generator left unattached, which the design does to a third of them --
    // has nothing above it either, but it heads nothing, so it never names the
    // island it was absorbed into.
    let crown = "";
    let crownAt = -1;
    for (const headed of [true, false]) {
      for (const id of members) {
        if (parentOf.has(id)) continue;
        if (headed && !(childrenOf.get(id)?.length)) continue;
        const at = micros(byId.get(id)?.created ?? "");
        if (at > crownAt) [crown, crownAt] = [id, at];
      }
      if (crown !== "") break;
    }
    if (crown === "") {
      for (const id of members) {
        const at = micros(byId.get(id)?.created ?? "");
        if (at > crownAt) [crown, crownAt] = [id, at];
      }
    }
    for (const id of members) topOf.set(id, crown);
  }
  const membersOf = new Map<string, string[]>();
  for (const [id, key] of topOf) bump(membersOf, key, id);
  const refined = new Map<string, string>();
  for (const [key, members] of membersOf) {
    for (const [islandKey, row] of split(key, members, childrenOf)) for (const id of row) refined.set(id, islandKey);
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
  const touch = new Map<string, Map<string, number>>();
  for (const edge of edges) {
    const [ga, gb] = [refined.get(edge.from_id), refined.get(edge.to_id)];
    if (ga === undefined || gb === undefined || ga === gb) continue;
    const row = touch.get(ga) ?? new Map<string, number>();
    row.set(gb, (row.get(gb) ?? 0) + 1);
    touch.set(ga, row);
  }
  const islands = (): string[] => [...size.keys()].filter((key) => find(key) === key);
  const window = new Map<string, [number, number]>();
  for (const [id, key] of refined) {
    const at = micros(byId.get(id)?.created ?? "");
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
      const row = size.get(rep)! >= MIN_ISLAND_NODES ? per : spare;
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
  for (;;) {
    const ranked = islands().sort((a, b) => size.get(a)! - size.get(b)!);
    if (ranked.length < 2) break;
    const small = ranked.find((key) => size.get(key)! < MIN_ISLAND_NODES);
    if (small === undefined) break;
    absorb(small, (touch.get(small)?.size ?? 0) > 0 ? ranked[ranked.length - 1]! : nearest(small, ranked));
  }
  return new Map([...refined].map(([id, key]) => [id, find(key)]));
}

/** The most records an island holds before it is cut into bands: the design's
 * biggest thread (948 with its papers attached) fits whole, so a line of
 * inquiry is never split. */
const ISLAND_CAP = 960;
/** A family smaller than this joins the island its records touch most. */
const MIN_ISLAND_NODES = 24;

/**
 * Cut one family into islands of at most the cap: the breadth-first spread
 * from its crown, chunked, so each island is a connected band of the inquiry —
 * a sub-issue and what hangs off it — and all of them come out much of a size.
 */
function split(key: string, members: string[], childrenOf: Map<string, string[]>): [string, string[]][] {
  if (members.length <= ISLAND_CAP) return [[key, members]];
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
  const chunks = Math.ceil(ordered.length / ISLAND_CAP);
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
