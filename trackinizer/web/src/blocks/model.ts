// The knowledge flow's data and its layout: what a model distills from the
// records — blocks of knowledge and the flow between them — and how those
// blocks arrange into columns, each one a step from what is settled on the left
// to what it leaves open on the right.
//
// The blocks are the AI's half of the view: given a prompt and the records it
// touches, the model decides how many blocks there are, what each claims, which
// records each stands on, and how one block leads to the next. The design is
// The design is the app's half and never changes: the same cards, the same labelled
// edges, whatever the prompt asked. This file holds only the contract between them.

/** How a block stands: settled by records, still open, or awaiting what has not run. */
export type BlockStatus = "settled" | "open" | "awaited";

// A block may carry one catalog node as its figure. Imported to be used below and
// re-exported so the brief's generator reads the flow's shapes from one file.
import type { Figure } from "../ui/figure";

export type { Figure };

/** How one block's knowledge flows into another, as the edge's label reads. */
export type EdgeKind = "leads to" | "leaves open" | "exposes" | "rests on" | "needs" | "awaits";

/** The edge kinds drawn solid: what the records already established. */
export const SOLID_KINDS: readonly EdgeKind[] = ["leads to", "leaves open", "exposes"];

/** Whether `kind` is drawn solid — established — rather than dashed, a want. */
export const isSolid = (kind: EdgeKind): boolean => SOLID_KINDS.includes(kind);

/** One row of a block's chain: the record that found it, what it found, and when. */
export type ChainRow = {
  /** The record as its ref reads, `Issue#15`; a click could open it. */
  readonly ref: string;
  /** What that record found, as one line of prose. */
  readonly note: string;
  /** Where and when it was found: `Josh · step 17,900 · grad_norm 0.8 → 41.3`. */
  readonly meta: string;
};

/** A block of knowledge: one distilled claim, and the records it stands on. */
export type KnowledgeBlock = {
  /** The block's id as its card wears it, `K#1`. */
  readonly id: string;
  readonly status: BlockStatus;
  /** The claim itself, an assertion rather than a name. */
  readonly title: string;
  /** The claim's body, in prose; `backticks` mark its mono tokens. */
  readonly body: string;
  /** What the block is, as its chip reads: `derived · 3 records`. */
  readonly derived: string;
  /** The provenance footer: `3 records · Issue#15 ×2 · step 17,900`. */
  readonly provenance: string;
  /** The records behind the claim, told when the card is expanded. */
  readonly chain?: readonly ChainRow[];
  /** The figure those records are a measurement of, shown with the chain when the
   *  block is opened: a `chart` of rows being compared, a `series` of a metric over
   *  steps, a `table` as its source prints it, an `equation` as it is stated. Pick the
   *  node from the catalog in LAW.md; a claim whose evidence is a number reads better
   *  as the figure than as a sentence about it. */
  readonly figure?: Figure;
};

/** One step of the flow: block `from`'s knowledge, as `kind` reads, into `to`.
 *  `why` is the distiller's own reason for the wire — what the modal says when
 *  a reader asks how this connection was made. */
export type FlowEdge = {
  readonly from: string;
  readonly to: string;
  readonly kind: EdgeKind;
  readonly why?: string;
};

/** A whole answer: the prompt that asked for it, and the flow that answers it. */
export type KnowledgeFlow = {
  /** The answer's name, as the header reads it: `1.3B divergence · Issue#15`. */
  readonly title: string;
  /** The prompt that asked, shown as it was typed. */
  readonly prompt: string;
  /** The block the flow reads from, top and centre; the most-wired one when unset. */
  readonly anchor?: string;
  /** The composition, when the distiller wants it said outright: each inner array
   *  is one row of blocks, top first — `[[K#1], [K#2, K#3, K#4]]` is one up, three
   *  down. Unset, the rows derive from the edges' distance from the anchor. */
  readonly rows?: readonly (readonly string[])[];
  readonly blocks: readonly KnowledgeBlock[];
  readonly edges: readonly FlowEdge[];
  // Session files this answer registers: full programs kept for the whole session,
  // listed in the strip above the stage so later prompts still carry them.
  readonly files?: readonly { readonly path: string; readonly text: string; readonly language?: string }[];
};

/** One column of the flow: the blocks a step's distance from what is settled. */
export type FlowColumn = { readonly depth: number; readonly blocks: readonly KnowledgeBlock[] };

/**
 * The flow's columns: each block a step right of the longest chain of edges
 * into it, so knowledge reads left to right — what is settled, what it exposes,
 * what is awaited. Within a column, blocks keep the order they were given in,
 * which is the order the model ranked them.
 */
export function columnsOf({ blocks, edges }: KnowledgeFlow): FlowColumn[] {
  const into = new Map(blocks.map((block) => [block.id, [] as string[]]));
  for (const edge of edges) into.get(edge.to)?.push(edge.from);
  const depth = new Map<string, number>();
  const depthOf = (id: string): number => {
    const known = depth.get(id);
    if (known !== undefined) return known;
    depth.set(id, 0); // A cycle draws as it is asked, not as a loop of layout.
    // A block nothing flows into — the leftmost — sits at 0, so the longest
    // chain into any block is the column it lands in.
    const walked = Math.max(-1, ...into.get(id)!.map(depthOf)) + 1;
    depth.set(id, walked);
    return walked;
  };
  for (const block of blocks) depthOf(block.id);
  const byDepth = new Map<number, KnowledgeBlock[]>();
  for (const block of blocks) {
    const at = depth.get(block.id) ?? 0;
    byDepth.set(at, [...(byDepth.get(at) ?? []), block]);
  }
  return [...byDepth.entries()].sort(([a], [b]) => a - b).map(([depth, blocks]) => ({ depth, blocks }));
}
