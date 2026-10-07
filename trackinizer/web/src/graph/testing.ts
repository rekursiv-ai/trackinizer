// Test helpers for the graph view; only tests import this file.
import type { GraphEdge, GraphNode } from "../api/graph";
import type { Meta } from "../app/boot";
import type { Palette } from "./encode";
import type { DrawLink, DrawNode } from "./model";
import type { Covered, Renderer, RendererEvents } from "./renderer";

/** A stable, distinct UUID for test number `n`. */
export function uuid(n: number): string {
  return `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
}

/** Inquiry number `n` as the graph sends it: an active Issue unless `fields` say otherwise. */
export function node(n: number, fields: Partial<GraphNode> = {}): GraphNode {
  return {
    id: uuid(n),
    kind: "Issue",
    seq: n,
    title: `Issue ${n}`,
    status: "active",
    created: new Date(Date.UTC(2026, 8, 20, 0, n)).toISOString().replace("Z", "+00:00"),
    ...fields,
  };
}

/** An edge from inquiry `from` (the child) to `to`, a `narrows` unless `kind` says otherwise. */
export function edge(from: number, to: number, kind = "narrows", fields: Partial<GraphEdge> = {}): GraphEdge {
  return { from_id: uuid(from), to_id: uuid(to), edge_kind: kind, ...fields };
}

/** A palette whose every colour names what it is for, so a test reads which one a look took. */
export const PALETTE: Palette = {
  kinds: { Issue: "issue-hue", Belief: "belief-hue", Paper: "paper-hue" },
  edges: { narrows: "narrows-hue", requires: "requires-hue", favors: "favors-hue", cites_paper: "cites-hue" },
  status: { active: "active-ring", complete: "complete-ring", abandoned: "abandoned-ring", invalid: "invalid-ring" },
  fallback: "fallback",
  background: "background",
  support: "support",
  against: "against",
  faint: "faint",
  halo: "halo",
  match: "match",
  highlight: "highlight",
};

/** The server's vocabulary as far as the graph reads it: kinds, statuses and edge kinds. */
export const META: Meta = {
  kinds: ["Issue", "Paper", "Belief"],
  enums: { status: ["active", "complete", "abandoned", "invalid"] },
  fieldOwners: {},
  edges: {
    narrows: { from_kinds: ["Issue"], to_kinds: ["Issue"], forward: "narrows", inverse: "narrowed_by" },
    proves: { from_kinds: ["Paper"], to_kinds: ["Belief"], forward: "proves", inverse: "proved_by" },
    favors: { from_kinds: ["Paper"], to_kinds: ["Belief"], forward: "favors", inverse: "favored_by" },
    cites_paper: { from_kinds: ["Paper"], to_kinds: ["Paper"], forward: "cites", inverse: "cited_by" },
    produced_by: { from_kinds: ["Issue", "Paper", "Belief"], to_kinds: ["Issue", "Paper", "Belief"], forward: "produced_by", inverse: "produces" },
  },
};

/**
 * A renderer that draws nothing and records what the view asked of it; its
 * `events` are the view's, for a test to point and click with. Node number `n`
 * is at (10n, 20) on screen.
 */
export class FakeRenderer implements Renderer {
  nodes: readonly DrawNode[] = [];
  links: readonly DrawLink[] = [];
  /** Each `setData` so far, by whether it asked to frame the graph once settled. */
  sets: boolean[] = [];
  /** Each frame a `setData` asked for once the layout settles: the titles of the visible nodes it keeps. */
  settleFrames: string[][] = [];
  /** How many nodes each `setData` drew. */
  counts: number[] = [];
  repaints = 0;
  fits = 0;
  /** Each `zoomBy` factor, in order. */
  zooms: number[] = [];
  /** The titles of the nodes centred on, in order. */
  centred: string[] = [];
  /** Each fit to some nodes: the titles of the visible ones it framed, and what covered the canvas as it framed them. */
  framed: { titles: string[]; covered: Covered }[] = [];
  /** What covered the canvas at each `refit`. */
  refits: Covered[] = [];
  /** Each node's group, by title, as the last `group` set it; null when not grouped. */
  groups: Map<string, string | undefined> | null = null;
  disposed = false;
  events: RendererEvents | null = null;
  /** What the view says covers the canvas, as a renderer measures it each time it frames. */
  covered: (() => Covered) | null = null;

  /** Make this the renderer, as `createRenderer` would. */
  readonly create = (_host: HTMLElement, events: RendererEvents, covered: () => Covered): Renderer => {
    this.events = events;
    this.covered = covered;
    return this;
  };

  setData(nodes: readonly DrawNode[], links: readonly DrawLink[], frame: ((node: DrawNode) => boolean) | null): void {
    this.nodes = nodes;
    this.links = links;
    this.sets.push(frame !== null);
    this.counts.push(nodes.length);
    if (frame) this.settleFrames.push(nodes.filter((drawn) => !drawn.hidden && frame(drawn)).map((drawn) => drawn.title));
  }

  centre(node: DrawNode): void {
    this.centred.push(node.title);
  }

  screenOf(node: DrawNode): { x: number; y: number } {
    return { x: 10 * node.seq, y: 20 };
  }

  /** The drawn node titled `title`. */
  node(title: string): DrawNode {
    const found = this.nodes.find((drawn) => drawn.title === title);
    if (!found) throw new Error(`No node titled ${title} is drawn.`);
    return found;
  }

  repaint(): void {
    this.repaints += 1;
  }

  fit(filter?: (node: DrawNode) => boolean): void {
    if (!filter) {
      this.fits += 1;
      return;
    }
    this.framed.push({ titles: this.nodes.filter((drawn) => !drawn.hidden && filter(drawn)).map((drawn) => drawn.title), covered: this.covered!() });
  }

  refit(): void {
    this.refits.push(this.covered!());
  }

  group(groupOf: ((node: DrawNode) => string | undefined) | null): void {
    this.groups = groupOf ? new Map(this.nodes.map((drawn) => [drawn.title, groupOf(drawn)])) : null;
  }

  zoomBy(factor: number): void {
    this.zooms.push(factor);
  }

  dispose(): void {
    this.disposed = true;
  }

  /** The titles of the nodes drawn and not hidden, in order. */
  shown(): string[] {
    return this.nodes.filter((drawn) => !drawn.hidden).map((drawn) => drawn.title);
  }
}
