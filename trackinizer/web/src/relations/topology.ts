import type { Detail, Peer } from "../api/detail";
import type { EdgeRef } from "../api/edges";
import type { EdgeTopology, FieldOwners } from "../api/meta";
import { sentence } from "../detail/relationGroups";

/**
 * One relation an inquiry can take, from `/api/meta/edges`: an edge kind, which
 * end of it this inquiry is, and the kinds the other end may be.
 */
export type RelationChoice = {
  readonly edgeKind: string;
  /**
   * Edges are stored child to parent, as in `RelationGroup`. `out`: this inquiry
   * is the child (it `narrows` the target). `in`: it is the parent.
   */
  readonly direction: "out" | "in";
  /** Read from this inquiry, in the server's words: `Narrows`, `Narrowed by`. */
  readonly label: string;
  /** The kinds the server accepts at the other end, PascalCase. */
  readonly targetKinds: readonly string[];
};

/**
 * Every relation an inquiry of `kind` can take, in the server's edge order, each
 * kind's outgoing choice before its incoming one, as the relation groups read.
 * The server refuses an edge whose ends are the wrong kinds, so only these are
 * offered.
 */
export function relationChoices(kind: string, topology: EdgeTopology): RelationChoice[] {
  return Object.entries(topology).flatMap(([edgeKind, rule]) => {
    const sides = [
      { direction: "out", ends: rule.from_kinds, targetKinds: rule.to_kinds, name: rule.forward },
      { direction: "in", ends: rule.to_kinds, targetKinds: rule.from_kinds, name: rule.inverse },
    ] as const;
    return sides
      .filter(({ ends }) => ends.includes(kind))
      .map(({ direction, targetKinds, name }) => ({ edgeKind, direction, label: sentence(name), targetKinds }));
  });
}

/** The stored edge between inquiry `self` and `other` for a relation read from `self`. */
export function relationEdge(
  self: string,
  { edgeKind, direction }: Pick<RelationChoice, "edgeKind" | "direction">,
  other: string,
): EdgeRef {
  return direction === "out" ? { from: self, kind: edgeKind, to: other } : { from: other, kind: edgeKind, to: self };
}

/**
 * Whether edges of `edgeKind` carry a priority: those that join the kind owning
 * the `priority` field to itself and nothing else (Issue to Issue).
 *
 * `/api/meta/edges` does not say which annotations a kind carries (the server
 * keeps it as `applies_to_edge_kinds` on `Edge`), and a priority means "this
 * child's priority under that parent", which only an Issue-to-Issue edge can
 * hold. The server refuses a priority anywhere else with its own message.
 */
export function carriesPriority(edgeKind: string, topology: EdgeTopology, fieldOwners: FieldOwners): boolean {
  const owner = fieldOwners.priority;
  const rule = topology[edgeKind];
  const onlyOwner = (kinds: readonly string[]) => kinds.length === 1 && kinds[0]!.toLowerCase() === owner;
  return owner !== undefined && rule !== undefined && onlyOwner(rule.from_kinds) && onlyOwner(rule.to_kinds);
}

/**
 * The provenance edge between `detail`'s inquiry and `peer` that removing `edge`
 * leaves in place, or null.
 *
 * The first structural edge between two inquiries also stores the younger as
 * `produced_by` the older, and removing that edge does not remove the inferred
 * one. Whether a given provenance edge was inferred or drawn by hand, it stays.
 */
export function provenanceLeft(detail: Detail, edge: EdgeRef, peer: Peer): EdgeRef | null {
  if (edge.kind === PROVENANCE) return null;
  const self = detail.self.id;
  if (detail.edges[PROVENANCE]?.some((origin) => origin.id === peer.id)) return { from: self, kind: PROVENANCE, to: peer.id };
  if (detail.backlinks[PROVENANCE]?.some((product) => product.id === peer.id)) {
    return { from: peer.id, kind: PROVENANCE, to: self };
  }
  return null;
}

/**
 * The edge kind that replaces its parent with its child, leaving the parent's
 * status as it was. The plan's Supersede actions are built on it.
 */
export const SUPERSEDES = "supersedes";

/** The edge kind the server infers between two inquiries: the child is produced by the parent. */
const PROVENANCE = "produced_by";
