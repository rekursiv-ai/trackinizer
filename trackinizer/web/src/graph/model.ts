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
  judgement?: string;
  confidence?: number;
  /** Distinct inquiries it shares an edge with. */
  degree: number;
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
  /** The layout's ends: the ids, until the layout swaps in their nodes. */
  source: string | DrawNode;
  target: string | DrawNode;
  look: LinkLook;
  /** One of its ends is hidden. */
  hidden: boolean;
};

/** What the view shows of the graph: what it hides, what it lights, and what search found. */
export type Lens = {
  /** Kinds and statuses the Filter hides: their nodes are never drawn. */
  readonly hiddenKinds: ReadonlySet<string>;
  readonly hiddenStatuses: ReadonlySet<string>;
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
  /** Search matches. */
  readonly matches: ReadonlySet<string>;
  /** Nodes whose titles show under them: the roots, while grouped. */
  readonly labelled: ReadonlySet<string>;
};

/** Nothing hidden, lit or found. */
export const PLAIN_LENS: Lens = {
  hiddenKinds: new Set(),
  hiddenStatuses: new Set(),
  lit: null,
  skipEdges: new Set(),
  only: false,
  strong: new Set(),
  matches: new Set(),
  labelled: new Set(),
};

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
      const drawn = { ...row, degree: 0, root: false, look: NO_LOOK, hidden: false };
      added.push(drawn);
      return drawn;
    });
    this.links = graph.edges.map(({ from_id: from, to_id: to, edge_kind: kind, valence }) => {
      const kept = linksBefore.get(linkKey(from, to, kind));
      if (kept) {
        kept.valence = valence;
        return kept;
      }
      addedLinks += 1;
      return { from, to, kind, valence, source: from, target: to, look: NO_LINK_LOOK, hidden: false };
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
      // The server sends only edges between the nodes it sends.
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
    const { hiddenKinds, hiddenStatuses, lit, skipEdges, only, strong, matches, labelled } = this.#lens;
    for (const drawn of this.nodes) {
      const far = lit?.get(drawn.id);
      const dimmed = lit !== null && far === undefined;
      const halo = strong.has(drawn.id) ? "strong" : matches.has(drawn.id) ? "match" : null;
      const label = labelled.has(drawn.id) ? drawn.title || "(untitled)" : null;
      drawn.look = nodeLook(drawn, this.#palette, { dimmed, far: far !== undefined && far >= 2, halo, label });
      drawn.hidden = hiddenKinds.has(drawn.kind) || hiddenStatuses.has(drawn.status) || (only && dimmed);
    }
    for (const link of this.links) {
      const [from, to] = [this.#byId.get(link.from)!, this.#byId.get(link.to)!];
      const dimmed = lit !== null && (!lit.has(link.from) || !lit.has(link.to) || skipEdges.has(link.kind));
      link.look = linkLook(link, Math.max(from.degree, to.degree), this.#palette, dimmed);
      link.hidden = from.hidden || to.hidden;
    }
  }
}

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
const NO_LOOK: NodeLook = { radius: 0, fill: "", fillAlpha: 0, wash: null, washAlpha: 0, ring: "", ringAlpha: 0, dot: null, halo: null, label: null };
const NO_LINK_LOOK: LinkLook = { color: "", width: 0, dash: null };
