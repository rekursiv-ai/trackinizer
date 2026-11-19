// What the graph's panels say about a cluster or a node, worked out from the
// graph the view already holds: a thread's verdicts, its turning points, the
// beliefs most relied on, the experiments best made next; a node's chain up to
// its root, its evidence mix, what would wobble were it wrong. The same reads
// the design prototype's briefing gave, on the app's own data.

import type { Graph, GraphEdge, GraphNode } from "../api/graph";
// Records are ordered by `micros`, never by comparing their date strings: a
// record whose date is missing or unreadable has to sort, not throw.
import { micros } from "../live/rows";
import type { RootGroup } from "./roots";

/** A record as the panels read it: the node, with its edges and neighbours. */
export type Record = {
  readonly node: GraphNode;
  /** Edges whose child or parent it is, child to parent as the server stores them. */
  readonly edges: readonly GraphEdge[];
};

/** Everything a panel needs about one inquiry: its record and its neighbours by id. */
export type GraphView = {
  readonly byId: ReadonlyMap<string, GraphNode>;
  /** Each id's edges, child to parent. */
  readonly edgesOf: ReadonlyMap<string, readonly GraphEdge[]>;
};

/** `graph` as a `GraphView`, its edges gathered once to their ends. */
export function viewOf({ nodes, edges }: Graph): GraphView {
  const edgesOf = new Map<string, GraphEdge[]>(nodes.map(({ id }) => [id, []]));
  for (const edge of edges) {
    edgesOf.get(edge.from_id)?.push(edge);
    edgesOf.get(edge.to_id)?.push(edge);
  }
  return { byId: new Map(nodes.map((node) => [node.id, node])), edgesOf };
}

/** The members of `group`, in the graph's order. */
export function membersOf(group: RootGroup, { byId }: GraphView): GraphNode[] {
  return group.members.map((id) => byId.get(id)).filter((node) => node !== undefined);
}

/** How a thread's claims stand, by verdict. */
export type Verdicts = { readonly proven: number; readonly unproven: number; readonly disproven: number };

/** The verdicts of `nodes`' Beliefs; other kinds count to none of the three. */
export function verdictsOf(nodes: readonly GraphNode[]): Verdicts {
  const verdicts = { proven: 0, unproven: 0, disproven: 0 };
  for (const node of nodes) {
    if (node.kind !== "Belief") continue;
    if (node.judgement === "proven") verdicts.proven += 1;
    else if (node.judgement === "disproven") verdicts.disproven += 1;
    else verdicts.unproven += 1;
  }
  return verdicts;
}

/** The distinct inquiries `node` shares a `kind` edge with, child to parent as `kind` reads. */
export function neighboursOf(node: GraphNode, kind: string, { byId, edgesOf }: GraphView): { node: GraphNode; edge: GraphEdge; child: boolean }[] {
  const found: { node: GraphNode; edge: GraphEdge; child: boolean }[] = [];
  for (const edge of edgesOf.get(node.id) ?? []) {
    const child = edge.from_id === node.id;
    const other = byId.get(child ? edge.to_id : edge.from_id);
    if (other && edge.edge_kind === kind) found.push({ node: other, edge, child });
  }
  return found;
}

/** The citations that vote on `belief`: a proves or favors edge whose parent it is, newest evidence first. */
export function evidenceOf(belief: GraphNode, view: GraphView): { node: GraphNode; edge: GraphEdge }[] {
  return (view.edgesOf.get(belief.id) ?? [])
    .filter((edge) => edge.from_id !== belief.id && (edge.edge_kind === "proves" || edge.edge_kind === "favors"))
    .flatMap((edge) => {
      const node = view.byId.get(edge.from_id);
      return node ? [{ node, edge }] : [];
    })
    .sort((a, b) => micros(b.node.created) - micros(a.node.created));
}

/** How many of `belief`'s citations are load-bearing for it, and how many against. */
export function loadOf(belief: GraphNode, view: GraphView): { against: number; for: number } {
  const load = { against: 0, for: 0 };
  for (const { edge } of evidenceOf(belief, view)) {
    if (edge.edge_kind !== "proves") continue;
    if ((edge.valence ?? 0) < 0) load.against += 1;
    else load.for += 1;
  }
  return load;
}

/** One date in a thread's story, for the briefing's turning points. */
export type Turn = { readonly created: string; readonly say: string; readonly refs: readonly string[] };

/**
 * A thread's turning points: the question opened, then the verdicts evidence
 * settled, the load-bearing citation that settled each, newest of each pair
 * first and the heaviest verdicts first, four told.
 */
export function turnsOf(group: RootGroup, view: GraphView): Turn[] {
  const root = group.root ? view.byId.get(group.root.id) : undefined;
  const turns: (Turn & { readonly weight: number })[] = [];
  if (root) turns.push({ created: root.created, say: "opened", refs: [root.id], weight: 9 });
  for (const node of membersOf(group, view)) {
    if (node.kind !== "Belief" || !node.judgement || node.judgement === "unproven") continue;
    const disproven = node.judgement === "disproven";
    const citations = evidenceOf(node, view).filter(({ edge }) => edge.edge_kind === "proves" && ((edge.valence ?? 0) < 0) === disproven);
    const strongest = citations
      .map((citation) => ({ citation, weight: Math.abs(citation.edge.valence ?? 0) }))
      .sort((a, b) => b.weight - a.weight)[0];
    if (!strongest) continue;
    turns.push({
      created: strongest.citation.node.created,
      say: disproven ? "broke" : "proved",
      refs: [strongest.citation.node.id, node.id],
      weight: strongest.weight + (disproven ? 1 : 0.8),
    });
  }
  return turns
    .sort((a, b) => b.weight - a.weight)
    .slice(0, 4)
    .sort((a, b) => micros(a.created) - micros(b.created));
}

/** A belief others lean on: its distinct citations, and the claims that cite it. */
export function reliedOn(nodes: readonly GraphNode[], view: GraphView, take = 5): { belief: GraphNode; evidence: number; leaning: number }[] {
  return nodes
    .filter((node) => node.kind === "Belief")
    .map((belief) => ({
      belief,
      evidence: evidenceOf(belief, view).length,
      leaning: leansOn(belief.id, view),
    }))
    .sort((a, b) => b.evidence + b.leaning * 3 - (a.evidence + a.leaning * 3))
    .slice(0, take);
}

/** How many distinct inquiries lean on `id` through a citation, within three steps of it. */
function leansOn(id: string, view: GraphView): number {
  const seen = new Set([id]);
  let frontier = [id];
  let leaning = 0;
  for (let hops = 0; hops < 3; hops++) {
    const next: string[] = [];
    for (const current of frontier) {
      for (const edge of view.edgesOf.get(current) ?? []) {
        if (edge.from_id !== current || (edge.edge_kind !== "proves" && edge.edge_kind !== "favors")) continue;
        if (seen.has(edge.to_id)) continue;
        seen.add(edge.to_id);
        next.push(edge.to_id);
        leaning += 1;
      }
    }
    frontier = next;
  }
  return leaning;
}

/** What a record paid for itself, both axes together. */
export function costOf(node: GraphNode): number {
  return (node.cost_agent_usd ?? 0) + (node.cost_resource_usd ?? 0);
}

/** How many records `id` stands as the producer of. */
function producedCount(id: string, { edgesOf }: GraphView): number {
  return (edgesOf.get(id) ?? []).filter((edge) => edge.to_id === id && edge.edge_kind === "produced_by").length;
}

/**
 * What it took to make `node`: its own cost plus each producer's, split among
 * everything that one produced. Repeating a record means paying for the work
 * behind it, not only its own line.
 */
export function shareOf(node: GraphNode, view: GraphView): number {
  let total = costOf(node);
  for (const edge of view.edgesOf.get(node.id) ?? []) {
    if (edge.from_id !== node.id || edge.edge_kind !== "produced_by") continue;
    const producer = view.byId.get(edge.to_id);
    if (!producer) continue;
    total += costOf(producer) / Math.max(1, producedCount(producer.id, view));
  }
  return total;
}

/** What a thread with nothing priced is assumed to pay for one test, the design's own figure. */
export const FALLBACK_TEST_USD = 20;

/** One test's price in `nodes`' thread: the median experiment's cost, and whether the thread paid it. */
export type TestPrice = { readonly usd: number; readonly priced: boolean };

/**
 * The typical price of one experiment in a thread: the median across its
 * experiments, producers included. A thread with no experiment, or none that
 * has spent anything, reads as `FALLBACK_TEST_USD` and `priced: false`, so a
 * panel can say the figure is an assumption rather than a bill.
 */
export function testPriceOf(nodes: readonly GraphNode[], view: GraphView): TestPrice {
  const costs = nodes
    .filter((node) => node.kind === "Experiment")
    .map((node) => shareOf(node, view))
    .filter((cost) => cost > 0)
    .sort((a, b) => a - b);
  if (costs.length === 0) return { usd: FALLBACK_TEST_USD, priced: false };
  return { usd: costs[Math.floor(costs.length / 2)]!, priced: true };
}

/** One experiment worth making next: the claim it would settle, and how. */
export type NextExperiment = {
  readonly belief: GraphNode;
  /** How contested or thin the claim is, 0 to 1: the higher, the more worth settling. */
  readonly score: number;
  /** The experiment to re-run, or the thread's latest to adapt; null designs one afresh. */
  readonly template: GraphNode | null;
  /** True when `template` proved this claim before: re-run it; false, adapt it. */
  readonly rerun: boolean;
  readonly evidence: number;
  readonly leaning: number;
  /** What one test in this thread costs, per-dollar ranking included. */
  readonly price: TestPrice;
};

/**
 * The experiments best made next among `nodes`' open claims: the more undecided
 * a claim and the more that rides on it, the sooner it is worth settling — per
 * dollar a test costs, so a cheap answer to the same question comes first. The
 * same data always gives the same list.
 */
export function nextExperiments(group: RootGroup, view: GraphView, take = 3): NextExperiment[] {
  const members = membersOf(group, view);
  const experiments = members.filter((node) => node.kind === "Experiment");
  const price = testPriceOf(members, view);
  const out: NextExperiment[] = [];
  for (const node of members) {
    if (node.kind !== "Belief" || node.judgement === "proven" || node.judgement === "disproven") continue;
    const load = loadOf(node, view);
    const evidence = load.for + load.against;
    const leaning = leansOn(node.id, view);
    if (evidence === 0 && leaning === 0) continue;
    const undecided = 1 - Math.abs(2 * (node.confidence ?? 0.5) - 1) * 0.85;
    const contested = load.for > 0 && load.against > 0 ? 1.5 : 1;
    const score = (undecided * contested * Math.log2(2 + evidence + leaning * 3)) / Math.sqrt(price.usd);
    // What to run: the strongest experiment that cites it, else the thread's newest.
    const citing = evidenceOf(node, view).filter(({ node: other }) => other.kind === "Experiment");
    const strongest = citing
      .map((citation) => ({ citation, weight: Math.abs(citation.edge.valence ?? 0) }))
      .sort((a, b) => b.weight - a.weight)[0];
    const template = strongest ? strongest.citation.node : (experiments.at(-1) ?? null);
    out.push({ belief: node, score, template, rerun: strongest !== undefined, evidence, leaning, price });
  }
  return out.sort((a, b) => b.score - a.score).slice(0, take);
}

/** What a panel's records cost, and per verdict their own proofs settled. */
export type Spend = {
  readonly agent: number;
  readonly resource: number;
  readonly total: number;
  /** Settled claims one of `paid`'s proofs bears on. */
  readonly settled: number;
  /** `total` over `settled`; null when nothing has settled, so no cost per verdict is claimed. */
  readonly perSettled: number | null;
};

/**
 * The bill for `paid`: each axis's total, and what per verdict those records'
 * own proofs settled. A claim counts as settled here only when a proof among
 * `paid` bears on it, whichever window the claim itself was made in — the
 * design divided its spend by the verdicts its own records moved.
 */
export function spendOf(paid: readonly GraphNode[], view: GraphView): Spend {
  const [agent, resource] = paid.reduce(
    ([a, r], node) => [a + (node.cost_agent_usd ?? 0), r + (node.cost_resource_usd ?? 0)],
    [0, 0],
  );
  const settled = new Set<string>();
  for (const node of paid) {
    for (const edge of view.edgesOf.get(node.id) ?? []) {
      if (edge.from_id !== node.id || edge.edge_kind !== "proves") continue;
      const belief = view.byId.get(edge.to_id);
      if (belief?.kind === "Belief" && (belief.judgement === "proven" || belief.judgement === "disproven")) {
        settled.add(belief.id);
      }
    }
  }
  const total = agent + resource;
  return { agent, resource, total, settled: settled.size, perSettled: settled.size === 0 ? null : total / settled.size };
}

/** What a node's panel tells of where it came from: its chain of produced_by up to a root, parent first. */
export function chainOf(node: GraphNode, view: GraphView, most = 6): GraphNode[] {
  const chain = [node];
  let current = node;
  for (let hops = 0; hops < most; hops++) {
    const parents = (view.edgesOf.get(current.id) ?? [])
      .filter((edge) => edge.from_id === current.id && edge.edge_kind === "produced_by")
      .map((edge) => view.byId.get(edge.to_id))
      .filter((parent) => parent !== undefined);
    const parent = parents[0];
    if (!parent) break;
    chain.unshift(parent);
    current = parent;
  }
  return chain.length > 1 ? chain : [];
}

/** What would wobble were `node` wrong: the claims that cite it, near first. */
export function impactOf(node: GraphNode, view: GraphView, take = 5): { belief: GraphNode; hops: number }[] {
  const seen = new Map<string, number>([[node.id, 0]]);
  let frontier = [node.id];
  const out: { belief: GraphNode; hops: number }[] = [];
  for (let hops = 1; hops <= 3 && frontier.length > 0; hops++) {
    const next: string[] = [];
    for (const current of frontier) {
      for (const edge of view.edgesOf.get(current) ?? []) {
        if (edge.from_id !== current || (edge.edge_kind !== "proves" && edge.edge_kind !== "favors")) continue;
        if (seen.has(edge.to_id)) continue;
        const belief = view.byId.get(edge.to_id);
        if (!belief) continue;
        seen.set(edge.to_id, hops);
        out.push({ belief, hops });
        next.push(edge.to_id);
      }
    }
    frontier = next;
  }
  return out.sort((a, b) => a.hops - b.hops || micros(b.belief.created) - micros(a.belief.created)).slice(0, take);
}

/** Each edge kind's two verbs: how the edge reads from its child end, and from its parent. */
const VERBS: { readonly [kind: string]: readonly [child: string, parent: string] } = {
  produced_by: ["produced_by", "produces"],
  narrows: ["narrows", "narrowed_by"],
  requires: ["requires", "required_by"],
  proves: ["proves", "proved_by"],
  favors: ["favors", "favored_by"],
  supersedes: ["supersedes", "superseded_by"],
  cites_paper: ["cites", "cited_by"],
};

/** One of the panel's connection groups: the edges a node shares, named by the verb they read as. */
export type ConnectionGroup = { readonly label: string; readonly rows: { node: GraphNode; edge: GraphEdge }[] };

/**
 * `node`'s connections, grouped by verb: what it produced, what cites it, what
 * it narrows. A Belief's `proves` and `favors` are left out — its ledger tells
 * those better. The busiest groups come first.
 */
export function groupsOf(node: GraphNode, view: GraphView): ConnectionGroup[] {
  const { byId, edgesOf } = view;
  const groups = new Map<string, { node: GraphNode; edge: GraphEdge }[]>();
  for (const edge of edgesOf.get(node.id) ?? []) {
    const verbs = VERBS[edge.edge_kind];
    if (!verbs) continue;
    const child = edge.from_id === node.id;
    if (node.kind === "Belief" && !child && (edge.edge_kind === "proves" || edge.edge_kind === "favors")) continue;
    const other = byId.get(child ? edge.to_id : edge.from_id);
    if (!other) continue;
    const label = child ? verbs[0] : verbs[1];
    const rows = groups.get(label) ?? [];
    rows.push({ node: other, edge });
    groups.set(label, rows);
  }
  return [...groups]
    .map(([label, rows]) => ({ label, rows: rows.sort((a, b) => micros(a.node.created) - micros(b.node.created)) }))
    .sort((a, b) => b.rows.length - a.rows.length || a.label.localeCompare(b.label));
}

/** `belief`'s evidence, the way its ledger reads: the heaviest votes first, whichever way they vote. */
export function ledgerOf(belief: GraphNode, view: GraphView): { node: GraphNode; edge: GraphEdge }[] {
  return evidenceOf(belief, view).sort((a, b) => Math.abs(b.edge.valence ?? 1) - Math.abs(a.edge.valence ?? 1));
}
