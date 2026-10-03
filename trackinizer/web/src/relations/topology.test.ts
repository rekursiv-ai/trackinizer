import { expect, test } from "vitest";
import { detail, META, peer, row, uuid } from "../detail/testing";
import { carriesPriority, provenanceLeft, relationChoices, relationEdge } from "./topology";

const choices = (kind: string) =>
  relationChoices(kind, META.edges).map(({ edgeKind, direction, label, targetKinds }) => [
    edgeKind,
    direction,
    label,
    targetKinds.join(","),
  ]);

test("an Issue takes the Issue-only relations both ways, and provenance and supersession with any kind", () => {
  const any = META.kinds.join(",");
  expect(choices("Issue")).toEqual([
    ["narrows", "out", "Narrows", "Issue"],
    ["narrows", "in", "Narrowed by", "Issue"],
    ["requires", "out", "Requires", "Issue"],
    ["requires", "in", "Required by", "Issue"],
    ["produced_by", "out", "Produced by", any],
    ["produced_by", "in", "Produces", any],
    ["supersedes", "out", "Supersedes", any],
    ["supersedes", "in", "Superseded by", any],
  ]);
});

test("a Belief cites and is cited; a Paper also cites papers; an Artifact cites claims but is never cited", () => {
  const edgeNames = (kind: string) => choices(kind).map(([edgeKind, direction]) => `${edgeKind}:${direction}`);
  const claims = ["Belief,Experiment"];
  expect(choices("Belief").filter(([edgeKind]) => edgeKind === "proves" || edgeKind === "favors")).toEqual([
    ["proves", "out", "Proves", ...claims],
    ["proves", "in", "Proved by", META.edges.proves!.from_kinds.join(",")],
    ["favors", "out", "Favors", ...claims],
    ["favors", "in", "Favored by", META.edges.favors!.from_kinds.join(",")],
  ]);
  expect(edgeNames("Paper")).toContain("cites_paper:out");
  expect(edgeNames("Paper")).toContain("cites_paper:in");
  expect(edgeNames("Artifact")).toContain("proves:out");
  expect(edgeNames("Artifact")).not.toContain("proves:in");
  expect(edgeNames("Artifact")).not.toContain("cites_paper:out");
});

test("a relation read from this inquiry is stored child to parent", () => {
  expect(relationEdge("me", { edgeKind: "narrows", direction: "out" }, "them")).toEqual({ from: "me", kind: "narrows", to: "them" });
  expect(relationEdge("me", { edgeKind: "narrows", direction: "in" }, "them")).toEqual({ from: "them", kind: "narrows", to: "me" });
});

test("only the Issue-to-Issue edge kinds carry a priority", () => {
  expect(Object.keys(META.edges).filter((edgeKind) => carriesPriority(edgeKind, META.edges, META.fieldOwners))).toEqual([
    "narrows",
    "requires",
  ]);
  expect(carriesPriority("narrows", META.edges, {})).toBe(false);
  expect(carriesPriority("unknown", META.edges, META.fieldOwners)).toBe(false);
});

test("removing an edge names a produced_by between the same two that stays, either way round", () => {
  const self = row("Issue", 1);
  const [older, newer, other] = [peer("Issue", 2), peer("Issue", 3), peer("Issue", 4)];
  const graph = detail(self, {
    edges: { narrows: [older, other], produced_by: [older] },
    backlinks: { requires: [newer], produced_by: [newer] },
  });
  expect(provenanceLeft(graph, { from: self.id, kind: "narrows", to: older.id }, older)).toEqual({
    from: self.id,
    kind: "produced_by",
    to: older.id,
  });
  expect(provenanceLeft(graph, { from: newer.id, kind: "requires", to: self.id }, newer)).toEqual({
    from: newer.id,
    kind: "produced_by",
    to: self.id,
  });
  expect(provenanceLeft(graph, { from: self.id, kind: "narrows", to: other.id }, other)).toBeNull();
  expect(provenanceLeft(graph, { from: self.id, kind: "produced_by", to: uuid(2) }, older)).toBeNull();
});
