import { expect, test } from "vitest";
import { isRailEdge, type RailPeer, railPeers, relationGroups } from "./relationGroups";
import { detail, META, peer, row } from "./testing";

/** Each rail peer as its `Kind#seq` and the names of the edges joining it. */
const named = (peers: readonly RailPeer[]) =>
  peers.map(({ peer: { kind, seq }, edges }) => [`${kind}#${seq}`, edges.map((edge) => edge.group.name)]);

test("the rail lists each peer once with every lineage edge joining it: parents out, children in", () => {
  // A real record's shape: one parent by both narrows and produced_by, two outputs.
  const groups = relationGroups(
    detail(row("Issue", 937), {
      edges: {
        produced_by: [peer("Issue", 760), peer("Paper", 7)],
        narrows: [peer("Issue", 760)],
        proves: [peer("Belief", 9)],
      },
      backlinks: { produced_by: [peer("Experiment", 589), peer("AgentSession", 632)], favors: [peer("Belief", 4)] },
    }),
    META.edges,
  );
  expect(named(railPeers(groups, "out"))).toEqual([
    ["Issue#760", ["narrows", "produced_by"]],
    ["Paper#7", ["produced_by"]],
  ]);
  expect(named(railPeers(groups, "in"))).toEqual([
    ["AgentSession#632", ["produces"]],
    ["Experiment#589", ["produces"]],
  ]);
});

test("each rail edge keeps its own annotations, so a row's actions edit that edge", () => {
  const groups = relationGroups(
    detail(row("Issue", 1), {
      backlinks: { narrows: [peer("Issue", 2, { priority: 0, note: "first" })], produced_by: [peer("Issue", 2)] },
    }),
    META.edges,
  );
  const [child] = railPeers(groups, "in");
  expect(child!.edges.map(({ group, peer }) => [group.edgeKind, group.direction, peer.note])).toEqual([
    ["narrows", "in", "first"],
    ["produced_by", "in", undefined],
  ]);
});

test("the rail's edge kinds are lineage, in the server's order; the rest are other relations", () => {
  expect(META.enums.edge_kind!.filter(isRailEdge)).toEqual(["narrows", "requires", "produced_by", "supersedes"]);
});

test("groups follow the server's edge order, outgoing before incoming, named by the server", () => {
  const groups = relationGroups(
    detail(row("Issue", 1), {
      edges: { produced_by: [peer("Issue", 2)], narrows: [peer("Issue", 3)] },
      backlinks: { narrows: [peer("Issue", 4)], proves: [peer("Paper", 5)], mentions: [peer("Paper", 6)] },
    }),
    META.edges,
  );
  expect(groups.map((group) => [group.edgeKind, group.direction, group.label])).toEqual([
    ["narrows", "out", "Narrows"],
    ["narrows", "in", "Narrowed by"],
    ["produced_by", "out", "Produced by"],
    ["proves", "in", "Proved by"],
    ["mentions", "in", "Mentions"],
  ]);
});

test("peers sort by edge priority, unset last, then kind and seq", () => {
  const [group] = relationGroups(
    detail(row("Issue", 1), {
      backlinks: {
        narrows: [peer("Issue", 9), peer("Issue", 4, { priority: 20 }), peer("Issue", 2), peer("Issue", 7, { priority: 0 })],
      },
    }),
    META.edges,
  );
  expect(group.peers.map((p) => p.seq)).toEqual([7, 4, 2, 9]);
});

test("an inquiry with no edges has no groups", () => {
  expect(relationGroups(detail(row("Issue", 1), { edges: { narrows: [] } }), META.edges)).toEqual([]);
});
