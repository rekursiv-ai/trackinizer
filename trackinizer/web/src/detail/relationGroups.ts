import type { Detail, Peer } from "../api/detail";
import type { EdgeTopology } from "../api/meta";

/** One edge kind in one direction, with the inquiries at the other end. */
export type RelationGroup = {
  readonly edgeKind: string;
  /**
   * Edges are stored child to parent. `out`: this inquiry is the child (it
   * `narrows` each peer). `in`: it is the parent (each peer narrows it).
   */
  readonly direction: "out" | "in";
  /** The server's name read from this inquiry: `narrows`, `narrowed_by`. */
  readonly name: string;
  /** The name as a label: `Narrows`, `Narrowed by`. */
  readonly label: string;
  readonly peers: readonly Peer[];
};

/** One edge joining a rail peer: its group, and the peer as that edge carries it, with the edge's annotations. */
export type RailEdge = { readonly group: RelationGroup; readonly peer: Peer };

/** A neighbour in the rail, once, with every lineage edge that joins it to this inquiry. */
export type RailPeer = { readonly peer: Peer; readonly edges: readonly RailEdge[] };

/**
 * `detail`'s relations, one group per edge kind and direction.
 *
 * Groups follow the server's edge order, each kind's outgoing group before its
 * incoming one; labels are the server's `forward` and `inverse` names. An edge
 * kind the topology does not name still shows, under its own name either way,
 * told apart by the direction's arrow. Peers sort by the edge's priority, unset
 * last, then kind and seq.
 */
export function relationGroups(detail: Detail, topology: EdgeTopology): RelationGroup[] {
  const kinds = [...new Set([...Object.keys(topology), ...Object.keys(detail.edges), ...Object.keys(detail.backlinks)])];
  return kinds.flatMap((edgeKind) => {
    const rule = topology[edgeKind];
    const sides = [
      { direction: "out", peers: detail.edges[edgeKind], name: rule?.forward ?? edgeKind },
      { direction: "in", peers: detail.backlinks[edgeKind], name: rule?.inverse ?? edgeKind },
    ] as const;
    return sides
      .filter(({ peers }) => peers?.length)
      .map(({ direction, peers, name }) => ({
        edgeKind,
        direction,
        name,
        label: sentence(name),
        peers: [...peers].sort(byEdgePriority),
      }));
  });
}

/**
 * Whether the rail shows edges of `edgeKind`: an inquiry's lineage, which the
 * plan names. Its parents are those it narrows, requires,
 * supersedes or was produced by; its children the reverse. Citations (`proves`,
 * `favors`, `cites_paper`) are other relations.
 */
export function isRailEdge(edgeKind: string): boolean {
  return ["narrows", "requires", "produced_by", "supersedes"].includes(edgeKind);
}

/**
 * The rail's peers on one side, `out` for parents and `in` for children: each
 * peer once, in the order of its first edge among `groups`, with every lineage
 * edge joining it, so a `narrows` and `produced_by` pair is one row.
 */
export function railPeers(groups: readonly RelationGroup[], direction: RelationGroup["direction"]): RailPeer[] {
  const peers = new Map<string, { peer: Peer; edges: RailEdge[] }>();
  for (const group of groups) {
    if (group.direction !== direction || !isRailEdge(group.edgeKind)) continue;
    for (const peer of group.peers) {
      const known = peers.get(peer.id);
      if (known) known.edges.push({ group, peer });
      else peers.set(peer.id, { peer, edges: [{ group, peer }] });
    }
  }
  return [...peers.values()];
}

/** `narrowed_by` → `Narrowed by`. */
export function sentence(name: string): string {
  const words = name.replaceAll("_", " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function byEdgePriority(a: Peer, b: Peer): number {
  return (a.priority ?? Infinity) - (b.priority ?? Infinity) || a.kind.localeCompare(b.kind) || a.seq - b.seq;
}
