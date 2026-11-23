// The store-to-flow distiller: `#/blocks?focus=<uuid>` turns a record's fold into a
// KnowledgeFlow without a hand-written answer. Every block is a record the store holds
// within reach, wired by the stored edges — the page invents nothing: bodies are
// edge-derived sentences, wires are the stored kinds mapped onto the flow's six words
// (with the stored kind quoted in the wire's `why`), and the tiers are BFS distance
// from the focus.
import { type KnowledgeBlock, type KnowledgeFlow, type ChainRow, type EdgeKind } from "./model";
// The whole store graph, not a focus neighbourhood: the distiller does its own selection
// locally, so it takes the wider `Graph` and reads `hops` only where a node happens to
// carry one (a `FocusGraph` is assignable here, and its nodes all do).
import { type Graph } from "../api/graph";

/** How many records the flow draws besides the focus: the best-wired ones first. */
const DRAWN_LIMIT = 8;

type FNode = { id: string; kind: string; seq: number; title?: string | null; status?: string | null; created?: string | null; judgement?: string | null; confidence?: number | null; hops?: number };

export function refOf(node: FNode): string {
  return `${node.kind}#${node.seq}`;
}

/** The wire word a stored edge kind reads as, oriented parent-ward. */
const WIRE: ReadonlyMap<string, EdgeKind> = new Map([
  ["proves", "rests on"],
  ["favors", "rests on"],
  ["cites_paper", "rests on"],
  ["produced_by", "leads to"],
  ["supersedes", "leads to"],
  ["narrows", "exposes"],
  ["requires", "needs"],
]);

export function distillFlow(graph: Graph, focusId: string, hops: number, asked?: FNode, descById?: Map<string, string>): { flow: KnowledgeFlow; ids: string[] } {
  // Every GraphNode already satisfies FNode (FNode's fields are the wider, nullable ones),
  // so this drops the empty entries rather than narrowing a type.
  const nodes: readonly FNode[] = (graph.nodes || []).filter((n) => Boolean(n && n.id));
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const focus = byId.get(focusId) || asked || nodes[0];
  const deg = new Map<string, number>();
  for (const e of graph.edges || []) {
    deg.set(e.from_id, (deg.get(e.from_id) || 0) + 1);
    deg.set(e.to_id, (deg.get(e.to_id) || 0) + 1);
  }
  // the drawn set: the asked record, its direct neighbours (the wires the store
  // actually holds), then the best-wired second hop up to the cap
  const neighbourIds: string[] = [];
  for (const e of graph.edges || []) {
    if (e.from_id === focus.id && !neighbourIds.includes(e.to_id)) neighbourIds.push(e.to_id);
    if (e.to_id === focus.id && !neighbourIds.includes(e.from_id)) neighbourIds.push(e.from_id);
  }
  neighbourIds.sort((a, b) => (deg.get(b) || 0) - (deg.get(a) || 0));
  const drawnIds = new Set([focus.id, ...neighbourIds.slice(0, DRAWN_LIMIT)]);
  const secondHop = [];
  for (const e of graph.edges || []) {
    if (secondHop.length >= DRAWN_LIMIT - drawnIds.size) break;
    const other = drawnIds.has(e.from_id) && !drawnIds.has(e.to_id) ? e.to_id
      : drawnIds.has(e.to_id) && !drawnIds.has(e.from_id) ? e.from_id : null;
    if (other && !drawnIds.has(other)) { drawnIds.add(other); secondHop.push(other); }
  }
  const drawn = [focus, ...nodes.filter((n) => drawnIds.has(n.id) && n.id !== focus.id)];
  const slot = new Map(drawn.map((n, i) => [n.id, `K#${i + 1}`]));

  // tiers: BFS distance from the focus across drawn edges
  const adj = new Map<string, string[]>();
  for (const e of graph.edges || []) {
    if (!slot.has(e.from_id) || !slot.has(e.to_id)) continue;
    (adj.get(e.from_id) || adj.set(e.from_id, []).get(e.from_id)!).push(e.to_id);
    (adj.get(e.to_id) || adj.set(e.to_id, []).get(e.to_id)!).push(e.from_id);
  }
  const tierOf = new Map([[focus.id, 0]]);
  let frontier = [focus.id];
  while (frontier.length) {
    const next: string[] = [];
    for (const id of frontier) for (const nb of adj.get(id) || []) {
      if (!tierOf.has(nb)) { tierOf.set(nb, (tierOf.get(id) || 0) + 1); next.push(nb); }
    }
    frontier = next;
  }
  const maxTier = Math.max(...[...tierOf.values()], 0);
  const rows: string[][] = Array.from({ length: maxTier + 1 }, () => []);
  for (const n of drawn) rows[tierOf.get(n.id) ?? 0].push(slot.get(n.id) || "");

  const blocks: KnowledgeBlock[] = drawn.map((n) => {
    const id = slot.get(n.id) || "";
    // the body is edge-derived: what this record rests on and what rests on it, by slot
    const onto: string[] = [], from: string[] = [];
    for (const e of graph.edges || []) {
      if (e.from_id !== n.id && e.to_id !== n.id) continue;
      const otherId = e.from_id === n.id ? e.to_id : e.from_id;
      const other = slot.get(otherId);
      if (!other) continue;
      const wire = WIRE.get(e.edge_kind) || e.edge_kind;
      (e.to_id === n.id ? onto : from).push(`${other} (${wire})`);
    }
    const described = descById?.get(n.id);
    const parts: string[] = [];
    if (onto.length) parts.push(`wired to ${onto.slice(0, 3).join(", ")}${onto.length > 3 ? `, and ${onto.length - 3} more` : ""}`);
    if (from.length) parts.push(`${from.slice(0, 3).join(", ")}${from.length > 3 ? `, and ${from.length - 3} more` : ""} point here`);
    const wireSentence = parts.length
      ? parts.join("; ") + ". The wires are the store's own \u2014 this page wrote none of them."
      : "No stored edge lands here within reach \u2014 the title is the whole claim, and the wires to it are still to be drawn.";
    const describedText = described && described.trim().length > 0
      ? (described.trim().length > 260 ? described.trim().slice(0, described.trim().lastIndexOf(" ", 260)) + "\u2026" : described.trim())
      : null;
    const body = describedText
      ? describedText + (parts.length ? ` \u2014 ${wireSentence}` : "")
      : wireSentence;
    const judgement = (n.judgement || "").toLowerCase();
    const status = judgement === "proven" ? "settled" : "open";
    const wired = (deg.get(n.id) || 0);
    const chain: ChainRow[] = [];
    for (const e of graph.edges || []) {
      if (e.from_id !== n.id && e.to_id !== n.id) continue;
      const other = byId.get(e.from_id === n.id ? e.to_id : e.from_id);
      if (!other) continue;
      const valence = (e as { valence?: number }).valence;
      chain.push({
        ref: refOf(other),
        note: `${e.edge_kind}${e.to_id === n.id ? " \u2192 here" : " \u2192 there"}`,
        meta: typeof valence === "number" ? `${valence > 0 ? "+" : ""}${valence} ${valence < 0 ? "against" : "for"}` : (n.hops != null ? `${n.hops} hop${n.hops === 1 ? "" : "s"} out` : ""),
      });
      if (chain.length >= 3) break;
    }
    const bits = [refOf(n)];
    if (n.judgement) bits.push(n.judgement);
    if (typeof n.confidence === "number") bits.push(`conf ${n.confidence}`);
    return {
      id, status,
      title: n.title || refOf(n),
      body,
      derived: `${refOf(n)} \u00b7 ${wired} wire${wired === 1 ? "" : "s"}`,
      provenance: bits.join(" \u00b7 "),
      ...(chain.length ? { chain } : {}),
    };
  });

  // wires: stored edges between drawn records, mapped onto the six words
  const edges = [];
  for (const e of graph.edges || []) {
    if (!slot.has(e.from_id) || !slot.has(e.to_id)) continue;
    const valence = (e as { valence?: number }).valence;
    const stored = e.edge_kind + (typeof valence === "number" ? ` \u00b7 valence ${valence > 0 ? "+" : ""}${valence}` : "");
    if (e.edge_kind === "produced_by" || e.edge_kind === "narrows") {
      // stored child -> parent; the wire reads parent -> child
      edges.push({ from: slot.get(e.to_id) || "", to: slot.get(e.from_id) || "", kind: WIRE.get(e.edge_kind) || "leads to", why: `stored: ${stored}` });
    } else {
      edges.push({ from: slot.get(e.from_id) || "", to: slot.get(e.to_id) || "", kind: WIRE.get(e.edge_kind) || "rests on", why: `stored: ${stored}` });
    }
  }

  const ids = drawn.map((n) => n.id);
  const flow: KnowledgeFlow = {
    title: `${focus.title || refOf(focus)} \u00b7 distilled`,
    prompt: `generated from the store \u2014 the records within ${hops} hop${hops === 1 ? "" : "s"} of ${refOf(focus)}, distilled into claims and wired by the stored edges. No hand-written answer.`,
    anchor: "K#1",
    rows: rows.filter((r) => r.length > 0),
    blocks,
    edges,
  };
  return { flow, ids };
}
