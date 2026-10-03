import { afterEach, expect, test, vi } from "vitest";
import { editableFields } from "../api/fields";
import type { FieldWrite } from "../api/inquiries";
import { stubFetch } from "../api/testing";
import { change, row } from "../detail/testing";
import {
  edgeAnnotationEdit,
  edgeLabelEdit,
  type Edit,
  fieldEdit,
  fieldValue,
  lastChangeOf,
  listEdit,
  sameValue,
  showValue,
} from "./edits";

const ID = "0b6f7c1e-2f7a-4c55-9d7e-1f0e6b1d2a33";
const EDGE = { from: ID, kind: "proves", to: "5d3c2b1a-0f9e-4d8c-8b7a-6e5d4c3b2a19" };
const ISSUE = editableFields("Issue");
const LANDED: FieldWrite = { id: ID, change_id: "c1" };

afterEach(() => {
  vi.unstubAllGlobals();
});

/** What `edit` sends, as `[method, path, body]`. */
async function shape<Result>(edit: Edit<Result>): Promise<unknown[]> {
  const sent = stubFetch(() => Response.json(LANDED));
  await edit.request.send();
  return [sent[0]!.method, sent[0]!.path.replace(ID, "{id}"), sent[0]!.body];
}

/** What undoing `edit` sends, after it returned `result`. */
async function undoShape<Result>(edit: Edit<Result>, result: Result): Promise<unknown[] | null> {
  const undo = edit.undo?.(result);
  return undo ? shape(undo) : null;
}

test("status, owner and judgement go by compare-and-set with the value the user saw", async () => {
  const status = fieldEdit({ id: ID, field: "status", route: ISSUE.status!, label: "Status", from: "active", to: "abandoned", reason: "dup" });
  expect(await shape(status)).toEqual([
    "PUT",
    "/api/inquiries/{id}/status",
    { value: "abandoned", mode: "cas", expected: "active", reason: "dup" },
  ]);
  expect(status.guard).toMatchObject({ type: "cas", id: ID, field: "status", label: "Status", base: "active", mine: "abandoned" });
  expect(await undoShape(status, LANDED)).toEqual([
    "PUT",
    "/api/inquiries/{id}/status",
    { value: "active", mode: "cas", expected: "abandoned" },
  ]);

  // Claiming an unowned row expects no owner; releasing one puts null.
  const claim = fieldEdit({ id: ID, field: "owner", route: ISSUE.owner!, label: "Owner", from: undefined, to: "ada" });
  expect(await shape(claim)).toEqual(["PUT", "/api/inquiries/{id}/owner", { value: "ada", mode: "cas", expected: null }]);
  expect(await undoShape(claim, LANDED)).toEqual([
    "PUT",
    "/api/inquiries/{id}/owner",
    { value: null, mode: "cas", expected: "ada" },
  ]);
  const again = claim.guard?.type === "cas" ? claim.guard.again("josh") : null;
  expect(again && (await shape(again))).toEqual(["PUT", "/api/inquiries/{id}/owner", { value: "ada", mode: "cas", expected: "josh" }]);
});

test("other fields set with PUT, clear with DELETE, and check the stored value at save", async () => {
  const priority = fieldEdit({ id: ID, field: "priority", route: ISSUE.priority!, label: "Priority", from: 20, to: 10 });
  expect(await shape(priority)).toEqual(["PUT", "/api/issue/{id}/priority", { value: 10 }]);
  expect(priority.guard).toMatchObject({ type: "check", base: 20, mine: 10 });
  expect(priority.done).toBe("Priority set to 10");

  const clear = fieldEdit({ id: ID, field: "priority", route: ISSUE.priority!, label: "Priority", from: 20, to: null });
  expect(await shape(clear)).toEqual(["DELETE", "/api/issue/{id}/priority", {}]);
  expect(clear.done).toBe("Cleared Priority");
  expect(await undoShape(clear, LANDED)).toEqual(["PUT", "/api/issue/{id}/priority", { value: 20 }]);
  expect(await undoShape(clear, { id: ID, change_id: null })).toBeNull();

  const description = fieldEdit({ id: ID, field: "description", route: ISSUE.description!, label: "Description", from: "a", to: "" });
  expect(await shape(description)).toEqual(["DELETE", "/api/inquiries/{id}/description", {}]);

  const read = stubFetch(() => Response.json(row("Issue", 1, { priority: 30 })));
  expect(priority.guard?.type === "check" && (await priority.guard.read())).toBe(30);
  expect(read.map((request) => request.path)).toEqual([`/api/inquiries/${ID}`]);
});

test("a list field changes one element at a time, even its last: never set or cleared whole (R4-F02)", async () => {
  const route = ISSUE.issue_kind!;
  // A DELETE of the whole list would also clear an element someone added since it was read.
  for (const to of [["bug", "task"], [], null]) {
    expect(() => fieldEdit({ id: ID, field: "issue_kind", route, label: "Type", from: ["bug"], to })).toThrow("one element at a time");
  }
  const add = listEdit({ id: ID, field: "issue_kind", route, label: "Type", op: "add", value: "task" });
  expect(await shape(add)).toEqual(["PATCH", "/api/issue/{id}/issue_kind", { op: "add", value: "task" }]);
  expect(add.done).toBe("Added task to Type");
  expect(await undoShape(add, LANDED)).toEqual(["PATCH", "/api/issue/{id}/issue_kind", { op: "sub", value: "task" }]);
  expect(await undoShape(add, { id: ID, change_id: null })).toBeNull();

  const notLast = listEdit({ id: ID, field: "issue_kind", route, label: "Type", op: "sub", value: "bug", from: ["bug", "task"] });
  expect(await shape(notLast)).toEqual(["PATCH", "/api/issue/{id}/issue_kind", { op: "sub", value: "bug" }]);
  const lastLabel = listEdit({ id: ID, field: "labels", route: ISSUE.labels!, label: "Labels", op: "sub", value: "x", from: ["x"] });
  expect(await shape(lastLabel)).toEqual(["PATCH", "/api/inquiries/{id}/labels", { op: "sub", value: "x" }]);
});

test("an Issue's last type clears with DELETE, since the server will not empty it by PATCH, checked at save", async () => {
  const route = ISSUE.issue_kind!;
  const last = listEdit({ id: ID, field: "issue_kind", route, label: "Type", op: "sub", value: "bug", from: ["bug"], reason: "why" });
  expect(await shape(last)).toEqual(["DELETE", "/api/issue/{id}/issue_kind", { reason: "why" }]);
  expect(last.done).toBe("Removed bug from Type");
  // A DELETE takes the whole list, so a type added since it was read must stop it.
  expect(last.guard).toMatchObject({ type: "check", base: ["bug"], mine: undefined });
  const read = stubFetch(() => Response.json(row("Issue", 1, { issue_kind: ["bug", "feature"] })));
  expect(last.guard && (await last.guard.read())).toEqual(["bug", "feature"]);
  expect(read.map((request) => request.path)).toEqual([`/api/inquiries/${ID}`]);
  const over = last.guard?.again(["bug", "feature"]);
  expect(over && (await shape(over))).toEqual(["DELETE", "/api/issue/{id}/issue_kind", { reason: "why" }]);
  expect(over?.guard?.base).toEqual(["bug", "feature"]);
  expect(await undoShape(last, LANDED)).toEqual(["PATCH", "/api/issue/{id}/issue_kind", { op: "add", value: "bug" }]);
  expect(await undoShape(last, { id: ID, change_id: null })).toBeNull();
});

test("an undo has no undo of its own", () => {
  const edit = fieldEdit({ id: ID, field: "title", route: ISSUE.title!, label: "Title", from: "Old", to: "New" });
  expect(edit.undo?.(LANDED)?.undo).toBeUndefined();
  expect(() => fieldEdit({ id: ID, field: "title", route: ISSUE.title!, label: "Title", from: "Old", to: "" })).toThrow(
    "cannot be cleared",
  );
});

test("an edge annotation touches both ends and undoes to its old value", async () => {
  const edit = edgeAnnotationEdit({ edge: EDGE, annotation: "valence", label: "Valence", from: 0.5, to: -0.5 });
  expect(edit.touches).toEqual([EDGE.from, EDGE.to]);
  expect(edit.guard).toBeUndefined();
  const path = `/api/edges/{id}/proves/${EDGE.to}/valence`;
  expect(await shape(edit)).toEqual(["PUT", path, { value: -0.5 }]);
  expect(await undoShape(edit, { change_id: "c1", created: false })).toEqual(["PUT", path, { value: 0.5 }]);

  const clearNote = edgeAnnotationEdit({ edge: EDGE, annotation: "note", label: "Note", from: "why", to: "" });
  expect(await shape(clearNote)).toEqual(["DELETE", `/api/edges/{id}/proves/${EDGE.to}/note`, {}]);
  expect(await undoShape(clearNote, { change_id: "c2", created: false })).toEqual([
    "PUT",
    `/api/edges/{id}/proves/${EDGE.to}/note`,
    { value: "why" },
  ]);

  const label = edgeLabelEdit({ edge: EDGE, op: "add", value: "key" });
  expect(await undoShape(label, { change_id: "c3", created: false })).toEqual([
    "PATCH",
    `/api/edges/{id}/proves/${EDGE.to}/labels`,
    { op: "sub", value: "key" },
  ]);
});

test("values compare as JSON, with every unset value alike", () => {
  expect(sameValue(undefined, null) && sameValue("", []) && sameValue(null, undefined)).toBe(true);
  // Inside a value, unset values differ, as the server's JSON compare has them:
  // else a check at save misses a change to an Experiment's config.
  expect(sameValue({ x: null }, { x: [] })).toBe(false);
  expect(sameValue({ x: "" }, {})).toBe(false);
  expect(sameValue([null], [""])).toBe(false);
  expect(sameValue({ lr: 0.1, layers: [2, 4] }, { layers: [2, 4], lr: 0.1 })).toBe(true);
  expect(sameValue({ lr: 0.1 }, { lr: 0.2 })).toBe(false);
  expect(sameValue(["a", "b"], ["b", "a"])).toBe(false);
  expect(sameValue([1], { 0: 1 })).toBe(false);
  expect(sameValue("0", 0)).toBe(false);
  expect(sameValue(null, "x")).toBe(false);
  expect([undefined, "text", ["a", "b"], 3, { a: 1 }].map(showValue)).toEqual(["Not set", "text", "a, b", "3", '{\n  "a": 1\n}']);
});

test("a field reads from a row as the detail reads it, and its last change by storage name", () => {
  const belief = row("Belief", 2, { judgement: "proven", marginal_cost: { agent_usd: 1.5, resource_usd: 0 } });
  expect([fieldValue(belief, "judgement"), fieldValue(belief, "confidence"), fieldValue(belief, "marginal_cost_agent_usd")]).toEqual([
    "proven",
    undefined,
    1.5,
  ]);
  const changes = [
    change(belief, 3, { kind: "belief_confidence", actor: "ada" }),
    change(belief, 2, { kind: "belief_judgement", actor: "josh" }),
    change(belief, 1, { kind: "belief_judgement", actor: "ada" }),
  ];
  expect(lastChangeOf(changes, "judgement")?.actor).toBe("josh");
  expect(lastChangeOf(changes, "status")).toBeUndefined();
});
