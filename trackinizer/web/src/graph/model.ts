import type { Graph } from "../api/graph";
import { type LinkLook, linkLook, type NodeLook, nodeLook, type Palette } from "./encode";

/**
 * One inquiry as the canvas draws it. The layout writes its place (`x`, `y`) and
 * speed (`vx`, `vy`) onto this object, so there is one per inquiry for as long
 * as it stays in the graph: its place survives every update.
 */
export type DrawNode = {
  readonly id: string;
  kind: string;
  seq: number;
  title: string;
  status: string;
  created: string;
  owner?: string;
  judgement?: string;
  confidence?: number;
  /** Distinct inquiries it shares an edge with. */
  degree: number;
  /** Weighted PageRank, normalised to the graph's busiest node: what lifts a hub's disc. */
  authority: number;
  /** An Issue that narrows nothing: the top of a tree (S6-03: `requires` makes no parent). */
  root: boolean;
  look: NodeLook;
  /** Not drawn: filtered out by its kind or status, or left out by Only these. */
  hidden: boolean;
  x?: number;
  y?: number;
  vx?: number;
  vy?: number;
};

/** One edge as the canvas draws it, child to parent; the arrow points at the parent. */
export type DrawLink = {
  readonly from: string;
  readonly to: string;
  readonly kind: string;
  valence?: number;
  /** When the wire was made and by whom, from the audit that made it. */
  asserted?: string;
  actor?: string;
  /** The layout's ends: the ids, until the layout swaps in their nodes. */
  source: string | DrawNode;
  target: string | DrawNode;
  look: LinkLook;
  /** One of its ends is hidden. */
  hidden: boolean;
};

/** What the view shows of the graph: what it hides, what it lights, and what search found. */
export type Lens = {
  /** Kinds, statuses and owners the Filter hides: their nodes are never drawn. */
  readonly hiddenKinds: ReadonlySet<string>;
  readonly hiddenStatuses: ReadonlySet<string>;
  readonly hiddenOwners: ReadonlySet<string>;
  /**
   * What a focus, or a hovered or selected node, lights: each lit node's hops
   * from it. Null lights everything.
   */
  readonly lit: ReadonlyMap<string, number> | null;
  /** Edge kinds the light does not walk: their links stay dim. */
  readonly skipEdges: ReadonlySet<string>;
  /** Only these: hide what the light leaves out, rather than dim it. */
  readonly only: boolean;
  /** The focus and the selection. */
  readonly strong: ReadonlySet<string>;
  /** What an agent pointed at: its halo shows under the focus's and over a match's, and nothing dims or hides for it. */
  readonly highlighted: ReadonlySet<string>;
  /** Search matches. */
  readonly matches: ReadonlySet<string>;
  /** Nodes whose titles show under them: the roots, while grouped. */
  readonly labelled: ReadonlySet<string>;
  /**
   * Where the timeline stands: each id that draws, and how much of itself it
   * draws, 1 for settled — the newest arrivals come up over a few steps. Null
   * shows the whole graph, which is where the view sits when nothing replays.
   */
  readonly shown: ReadonlyMap<string, number> | null;
};

/** Nothing hidden, lit or found. */
export const PLAIN_LENS: Lens = {
  hiddenKinds: new Set(),
  hiddenStatuses: new Set(),
  hiddenOwners: new Set(),
  lit: null,
  skipEdges: new Set(),
  only: false,
  strong: new Set(),
  matches: new Set(),
  highlighted: new Set(),
  labelled: new Set(),
  shown: null,
};

/**
 * The wires the field draws: the server's edges, minus each `produced_by` that only
 * companions a `narrows` or a `requires` between the same two Issues. The server
 * hangs one beside every refinement, so a refinement reads as a parent too; the
 * islands already decline to count that companion, and the light must decline as
 * well, or a fifth of the field's wires are drawn twice over themselves and the
 * whole thing reads as a hairball instead of a spine.
 */
export function wires(graph: Graph): Graph["edges"] {
  const bound = new Set<string>();
  for (const { from_id: from, to_id: to, edge_kind: kind } of graph.edges) {
    if (kind === "narrows" || kind === "requires") {
      bound.add(`${from}|${to}`);
      bound.add(`${to}|${from}`);
    }
  }
  const kindOf = new Map(graph.nodes.map((row) => [row.id, row.kind]));
  return graph.edges.filter(
    ({ from_id: from, to_id: to, edge_kind: kind }) =>
      !(kind === "produced_by" && kindOf.get(from) === "Issue" && kindOf.get(to) === "Issue" && bound.has(`${from}|${to}`)),
  );
}

/**
 * The graph the canvas draws, kept across the server's answers.
 *
 * Every answer is the whole graph. An inquiry or edge still in it keeps its
 * object, with the new fields, so the layout keeps its place; one that left is
 * dropped, and a new one gets an object of its own. A node that arrives once
 * others are placed starts at rest beside a placed neighbour, or the middle of
 * the placed nodes, and the placed nodes slow down, so the layout shifts
 * locally (v1's `seedNewNodePositions`). Looks are worked out here, not per
 * frame (`encode.ts`), from the `Lens` the view last showed.
 */
export class GraphModel {
  nodes: DrawNode[] = [];
  links: DrawLink[] = [];
  #palette: Palette;
  #lens: Lens = PLAIN_LENS;
  #neighbours = new Map<string, Set<string>>();
  #byId = new Map<string, DrawNode>();

  constructor(palette: Palette) {
    this.#palette = palette;
  }

  /** Take the server's `graph`; true when its inquiries or edges changed, not only their fields. */
  apply(graph: Graph): boolean {
    const nodesBefore = new Map(this.nodes.map((drawn) => [drawn.id, drawn]));
    const linksBefore = new Map(this.links.map((link) => [linkKey(link.from, link.to, link.kind), link]));
    const added: DrawNode[] = [];
    let addedLinks = 0;
    this.nodes = graph.nodes.map((row) => {
      const kept = nodesBefore.get(row.id);
      if (kept) return Object.assign(withoutBelief(kept), row);
      const drawn = { ...row, degree: 0, authority: 0, root: false, look: NO_LOOK, hidden: false };
      added.push(drawn);
      return drawn;
    });
    // A wire needs both its ends to be drawable. This build's server sends only
    // edges between the records it sends, and the view trusted that outright: one
    // edge naming a record outside the window — another producer of the same
    // shape, or a record dropped between the two queries — threw on a node that
    // was not there and took the whole view down with a crash screen.
    const drawable = new Set(this.nodes.map((drawn) => drawn.id));
    this.links = wires(graph)
      .filter(({ from_id: from, to_id: to }) => drawable.has(from) && drawable.has(to))
      .map(({ from_id: from, to_id: to, edge_kind: kind, valence, asserted, actor }) => {
        const kept = linksBefore.get(linkKey(from, to, kind));
        if (kept) {
          kept.valence = valence;
          kept.asserted = asserted;
          kept.actor = actor;
          return kept;
        }
        addedLinks += 1;
        return { from, to, kind, valence, asserted, actor, source: from, target: to, look: NO_LINK_LOOK, hidden: false };
      });
    this.#byId = new Map(this.nodes.map((drawn) => [drawn.id, drawn]));
    // Every node was kept or added, so with none added a count that differs means
    // one left; the same holds for links.
    const changed =
      added.length > 0 ||
      addedLinks > 0 ||
      this.nodes.length !== nodesBefore.size ||
      this.links.length !== linksBefore.size;
    this.#neighbours = this.#connect();
    weighAuthority(this.nodes, this.links);
    if (changed) place(added, this.nodes, this.#neighbours);
    this.#restyle();
    return changed;
  }

  /** The node drawn for inquiry `id`, if the graph has it. */
  find(id: string): DrawNode | undefined {
    return this.#byId.get(id);
  }

  /** Draw with `palette` what `lens` shows, from now on. */
  show(palette: Palette, lens: Lens): void {
    this.#palette = palette;
    this.#lens = lens;
    this.#restyle();
  }

  /** Set each node's degree and whether it is a root; returns each node's neighbours' ids. */
  #connect(): Map<string, Set<string>> {
    const neighbours = new Map(this.nodes.map((drawn) => [drawn.id, new Set<string>()]));
    const children = new Set<string>();
    for (const link of this.links) {
      // Both ends are drawable: `apply` drops a wire whose ends are not here.
      neighbours.get(link.from)!.add(link.to);
      neighbours.get(link.to)!.add(link.from);
      if (link.kind === "narrows") children.add(link.from);
    }
    for (const drawn of this.nodes) {
      drawn.degree = neighbours.get(drawn.id)!.size;
      drawn.root = drawn.kind === "Issue" && !children.has(drawn.id);
    }
    return neighbours;
  }

  #restyle(): void {
    const { hiddenKinds, hiddenStatuses, hiddenOwners, lit, skipEdges, only, strong, highlighted, matches, labelled, shown } =
      this.#lens;
    for (const drawn of this.nodes) {
      const far = lit?.get(drawn.id);
      const dimmed = lit !== null && far === undefined;
      const halo = strong.has(drawn.id) ? "strong" : highlighted.has(drawn.id) ? "highlight" : matches.has(drawn.id) ? "match" : null;
      const label = labelled.has(drawn.id) ? drawn.title || "(untitled)" : null;
      // A replay grows the graph by showing more of it, not by re-sending it: a
      // record before the timeline's place is not drawn at all, and the few
      // behind it come up as they are reached.
      const appear = shown === null ? 1 : shown.get(drawn.id) ?? 0;
      // What a focus lights keeps its kind's hue at every zoom: the reader asked
      // what this neighbourhood is made of, and the answer should not wait for a zoom.
      drawn.look = nodeLook(drawn, this.#palette, { dimmed, far: far !== undefined && far >= 2, halo, label, near: lit !== null && !dimmed, appear });
      drawn.hidden =
        appear === 0 ||
        hiddenKinds.has(drawn.kind) ||
        hiddenStatuses.has(drawn.status) ||
        hiddenOwners.has(drawn.owner ?? "") ||
        (only && dimmed);
    }
    for (const link of this.links) {
      const [from, to] = [this.#byId.get(link.from)!, this.#byId.get(link.to)!];
      const dimmed = lit !== null && (!lit.has(link.from) || !lit.has(link.to) || skipEdges.has(link.kind));
      link.look = linkLook(link, Math.max(from.degree, to.degree), this.#palette, dimmed);
      // A kind switched off in the key is gone from the canvas, as the
      // prototype's `hiddenEdges` are: not dimmed, not drawn.
      link.hidden = from.hidden || to.hidden || skipEdges.has(link.kind);
    }
  }
}

/**
 * Each node's authority, the design's weighted PageRank over child→parent edges
 * (dependent→dependency), normalised to the graph's busiest node and square-
 * rooted. Degree would crown every hub; authority crowns only what the evidence
 * actually flows through, which is what packs an island into a ball with a bright
 * core instead of a loose scatter of equal dots.
 */
function weighAuthority(nodes: readonly DrawNode[], links: readonly DrawLink[]): void {
  const count = nodes.length;
  if (count === 0) return;
  const indexOf = new Map(nodes.map((node, position) => [node.id, position] as const));
  const from = new Int32Array(links.length);
  const to = new Int32Array(links.length);
  const weight = new Float64Array(links.length);
  const outWeight = new Float64Array(count);
  links.forEach((link, position) => {
    const [a, b] = [indexOf.get(link.from), indexOf.get(link.to)];
    if (a === undefined || b === undefined) return;
    from[position] = a;
    to[position] = b;
    // A weight has to be a number: this is a PageRank over the whole field, so
    // one NaN valence spreads through every pass and comes out as a NaN
    // authority on every record — and authority sets the radius, so the entire
    // graph stops being drawn. A valence that is not a reading weighs as an
    // absent one does, and a reading past one is held at one.
    const vote = typeof link.valence === "number" && Number.isFinite(link.valence)
      ? Math.max(0.15, Math.min(1, Math.abs(link.valence)))
      : 1;
    weight[position] = (EDGE_WEIGHT[link.kind] ?? 0.3) * vote;
    outWeight[a] += weight[position];
  });
  let current = new Float64Array(count).fill(1 / count);
  let next = new Float64Array(count);
  for (let pass = 0; pass < PAGERANK_PASSES; pass++) {
    let dangling = 0;
    for (let node = 0; node < count; node++) if (outWeight[node] === 0) dangling += current[node];
    next.fill((0.15 + 0.85 * dangling) / count);
    for (let edge = 0; edge < links.length; edge++) {
      if (outWeight[from[edge]!] === 0) continue;
      next[to[edge]!] += (0.85 * current[from[edge]!]! * weight[edge]) / outWeight[from[edge]!];
    }
    [current, next] = [next, current];
  }
  let peak = 0;
  for (let node = 0; node < count; node++) peak = Math.max(peak, current[node]!);
  if (peak <= 0) return;
  for (let node = 0; node < count; node++) nodes[node]!.authority = Math.sqrt(current[node]! / peak);
}

/** The PageRank's weight per edge kind: evidence flows hardest through proof. */
const EDGE_WEIGHT: { [kind: string]: number } = {
  proves: 1,
  favors: 0.5,
  cites_paper: 0.6,
  narrows: 0.35,
  requires: 0.35,
  produced_by: 0.25,
  supersedes: 0.4,
};
const PAGERANK_PASSES = 40;

/** `drawn` without a Belief's fields, which an answer that lacks them has cleared. */
function withoutBelief(drawn: DrawNode): DrawNode {
  delete drawn.judgement;
  delete drawn.confidence;
  return drawn;
}

/**
 * Start each of `added` at rest beside a placed neighbour, or the middle of the
 * placed nodes, and slow the placed ones, once any of `nodes` is placed: before
 * that, the layout places them all.
 */
function place(added: readonly DrawNode[], nodes: readonly DrawNode[], neighbours: ReadonlyMap<string, ReadonlySet<string>>): void {
  const fresh = new Set(added);
  const placed = new Map(
    nodes.filter((drawn) => !fresh.has(drawn) && Number.isFinite(drawn.x) && Number.isFinite(drawn.y)).map((drawn) => [drawn.id, drawn]),
  );
  if (placed.size === 0) return;
  const middle = { x: 0, y: 0 };
  for (const drawn of placed.values()) {
    middle.x += drawn.x! / placed.size;
    middle.y += drawn.y! / placed.size;
    drawn.vx = (drawn.vx ?? 0) * SLOW_DOWN;
    drawn.vy = (drawn.vy ?? 0) * SLOW_DOWN;
  }
  for (const [index, drawn] of added.entries()) {
    const anchor = [...neighbours.get(drawn.id)!].map((id) => placed.get(id)).find((peer) => peer !== undefined) ?? middle;
    const distance = 36 + 12 * Math.sqrt(drawn.degree);
    const angle = (placed.size + index) * GOLDEN_ANGLE;
    Object.assign(drawn, { x: anchor.x! + distance * Math.cos(angle), y: anchor.y! + distance * Math.sin(angle), vx: 0, vy: 0 });
  }
}

function linkKey(from: string, to: string, kind: string): string {
  return `${from} ${to} ${kind}`;
}

/** What a placed node's speed keeps when nodes arrive. */
const SLOW_DOWN = 0.25;

/** Spreads the nodes that arrive together around their anchor, as sunflower seeds are. */
const GOLDEN_ANGLE = Math.PI * (3 - Math.sqrt(5));

/** Every node and link is styled before the canvas sees it; this only fills the field until then. */
const NO_LOOK: NodeLook = { radius: 0, fill: "", fillAlpha: 0, wash: null, washAlpha: 0, ring: "", ringAlpha: 0, dot: null, halo: null, label: null, duo: "" };
const NO_LINK_LOOK: LinkLook = { color: "", width: 0, dash: null };
