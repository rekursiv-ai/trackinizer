import { describe, expect, test } from "vitest";
import type { LoggedChange } from "../api/changes";
import { dayLabel, feedItems, isTab, mentioned, tabKinds } from "./feed";

const LABELS = { narrows: "narrows", requires: "requires" };

/** A stable UUID for test number `n`. */
function uuid(n: number): string {
  return `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
}

/** Change number `n`, `n` minutes past 10:00 on 2026-09-24 unless `created` says otherwise. */
function change(n: number, fields: Partial<LoggedChange> = {}): LoggedChange {
  return {
    id: uuid(n),
    created: `2026-09-24T10:${String(n % 60).padStart(2, "0")}:00Z`,
    actor: "ada@example.com",
    kind: "created",
    subject_id: uuid(1000 + n),
    subject_kind: "Issue",
    caused_by: null,
    reason: "",
    old: {},
    new: {},
    ...fields,
  };
}

/** Both halves of an edge from `child` to `parent`, as the server writes them. */
function edgePair(n: number, child: string, parent: string, edgeKind = "requires"): [LoggedChange, LoggedChange] {
  const first = change(n, {
    kind: "edge_added",
    subject_id: child,
    new: { peer_id: parent, peer_kind: "Issue", peer_edge_kind: edgeKind },
  });
  const second = change(n + 1, {
    kind: "edge_added",
    created: first.created,
    subject_id: parent,
    caused_by: first.id,
    new: { peer_id: child, peer_kind: "Issue", peer_edge_kind: edgeKind },
  });
  return [first, second];
}

test("tabs are change kind sets, each read in one request, and All reads every tab's (PA2)", () => {
  expect(tabKinds("all")).toEqual([
    "status",
    "belief_judgement",
    "created",
    "purged",
    "edge_added",
    "edge_removed",
    "description",
    "title",
    "issue_priority",
    "labels",
    "owner",
  ]);
  expect(tabKinds("created")).toEqual(["created", "purged"]);
  expect(tabKinds("relations")).toEqual(["edge_added", "edge_removed"]);
  expect(tabKinds("edits")).toEqual(["description", "title", "issue_priority", "labels", "owner"]);
  expect(["all", "edits", "status", "description", "nonsense"].map(isTab)).toEqual([true, true, true, false, false]);
});

describe("feedItems", () => {
  test("an edge's two halves read as one line on the child, in its own words", () => {
    const [child, parent] = [uuid(501), uuid(502)];
    const items = feedItems(edgePair(10, child, parent), LABELS);
    expect(items).toHaveLength(1);
    expect(items[0]!.subject.id).toBe(child);
    expect(items[0]!.phrase).toEqual(["added relation Requires ", { id: parent, kind: "Issue" }]);
  });

  test("a lone half with no cause is the child's; a lone caused half does not guess a direction", () => {
    const [first, second] = edgePair(10, uuid(501), uuid(502), "narrows");
    expect(feedItems([first], LABELS)[0]!.phrase[0]).toBe("added relation Narrows ");
    expect(feedItems([second], LABELS)[0]!.phrase[0]).toBe("added a narrows relation with ");
  });

  test("an inferred edge caused by another change still folds, and keeps its reason", () => {
    const [child, parent] = edgePair(10, uuid(501), uuid(502), "produced_by");
    const inferred = { ...child, caused_by: uuid(9), reason: "inferred provenance" };
    const items = feedItems([{ ...parent, caused_by: inferred.id }, inferred], {});
    expect(items.map((item) => [item.change.reason, item.phrase[0]])).toEqual([
      ["inferred provenance", "added relation Produced by "],
    ]);
  });

  test("status, judgement, description and create say what changed", () => {
    const phrases = feedItems(
      [
        change(1, { kind: "status", old: { status: "active" }, new: { status: "complete" } }),
        change(2, { kind: "belief_judgement", subject_kind: "Belief", new: { belief_judgement: "proven" } }),
        change(3, { kind: "belief_judgement", subject_kind: "Belief", old: { belief_judgement: "proven" } }),
        change(4, { kind: "description", old: { description: "a" }, new: { description: "b" } }),
        change(5, { kind: "description", new: { description: "b" } }),
        change(6, { kind: "description", old: { description: "a" }, new: { description: null } }),
        change(7, { subject_kind: "CodeChange" }),
      ],
      LABELS,
    ).map((item) => item.phrase.join(""));
    expect(phrases).toEqual([
      "changed status from active to complete",
      "marked as Proven",
      "cleared the judgement",
      "edited the description",
      "added a description",
      "cleared the description",
      "created the code change",
    ]);
  });

  test("title, priority, label and owner edits and purges say what changed, from brief rows (PA2)", () => {
    const phrases = feedItems(
      [
        change(1, { kind: "title", old: { title: "The old name" }, new: { title: "The new name, cut at 32 characte" } }),
        change(2, { kind: "issue_priority", old: { issue_priority: 20 }, new: { issue_priority: 10 } }),
        change(3, { kind: "issue_priority", new: { issue_priority: 0 } }),
        change(4, { kind: "issue_priority", old: { issue_priority: 30 } }),
        change(5, { kind: "labels", old: { labels: ["ui"] }, new: { labels: ["ui", "server"] } }),
        change(6, { kind: "labels", old: { labels: ["ui", "docs", "server"] }, new: { labels: ["server", "perf"] } }),
        change(7, { kind: "owner", new: { owner: "bo@example.com" } }),
        change(8, { kind: "owner", old: { owner: "bo@example.com" }, new: { owner: "Agent" } }),
        change(9, { kind: "owner", old: { owner: "Agent" } }),
        change(10, { kind: "purged", subject_kind: "Belief", reason: "duplicate" }),
      ],
      LABELS,
    ).map((item) => item.phrase.join(""));
    expect(phrases).toEqual([
      "changed the title",
      "changed priority from 20 to 10",
      "set priority to 0",
      "cleared the priority",
      "added label server",
      "added label perf and removed labels ui, docs",
      "set the owner to bo@example.com",
      "changed the owner from bo@example.com to Agent",
      "cleared the owner",
      "purged the belief",
    ]);
  });

  test("a removed edge's two halves read as one line on the child; a lone caused half names the edge kind alone (PA2)", () => {
    const [child, parent] = [uuid(501), uuid(502)];
    const removed = edgePair(10, child, parent).map((half) => ({ ...half, kind: "edge_removed", old: half.new, new: {} }));
    const items = feedItems(removed, LABELS);
    expect(items.map((item) => [item.subject.id, item.phrase])).toEqual([
      [child, ["removed relation Requires ", { id: parent, kind: "Issue" }]],
    ]);
    expect(feedItems([removed[1]!], LABELS)[0]!.phrase).toEqual(["removed a requires relation with ", { id: child, kind: "Issue" }]);
  });
});

test("mentioned lists each subject and peer once, in order of first mention", () => {
  const [child, parent] = [uuid(501), uuid(502)];
  const items = feedItems([change(30, { subject_id: parent }), ...edgePair(10, child, parent)], LABELS);
  expect(mentioned(items).map((m) => m.id)).toEqual([parent, child]);
});

describe("dayLabel", () => {
  const now = new Date(2026, 8, 26, 9, 0).getTime();

  test("names today and yesterday by calendar day, not by 24 hours", () => {
    expect(dayLabel(new Date(2026, 8, 26, 0, 5).toISOString(), now)).toBe("Today");
    expect(dayLabel(new Date(2026, 8, 25, 23, 50).toISOString(), now)).toBe("Yesterday");
  });

  test("names an older day by weekday and date, and adds the year when it differs", () => {
    expect(dayLabel(new Date(2026, 8, 21, 12, 0).toISOString(), now)).toBe("Monday, Sep 21");
    expect(dayLabel(new Date(2025, 11, 31, 12, 0).toISOString(), now)).toBe("Wednesday, Dec 31, 2025");
  });
});
