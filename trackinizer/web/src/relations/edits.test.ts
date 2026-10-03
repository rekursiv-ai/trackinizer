import { afterEach, expect, test, vi } from "vitest";
import { stubFetch } from "../api/testing";
import { row, uuid } from "../detail/testing";
import { addRelationEdit, removeRelationEdit, supersedeWithNewEdit } from "./edits";

const EDGE = { from: uuid(3), kind: "narrows", to: uuid(1) };
const PATH = `/api/edges/${uuid(3)}/narrows/${uuid(1)}`;

afterEach(() => {
  vi.unstubAllGlobals();
});

test("an add is a bare POST keyed in its header, touches both ends, and has no undo", async () => {
  const sent = stubFetch(() => Response.json({ change_id: "c1", created: true }));
  const edit = addRelationEdit(EDGE);
  expect(await edit.request.send()).toEqual({ change_id: "c1", created: true });
  expect(sent).toHaveLength(1);
  expect(sent[0]).toMatchObject({ method: "POST", path: PATH, body: {} });
  expect(Object.keys(sent[0]!.headers)).toContain("idempotency-key");
  expect(edit.request.route).toBe("addEdge");
  expect(edit.touches).toEqual([uuid(3), uuid(1)]);
  expect(edit.undo).toBeUndefined();
});

test("a remove is a DELETE with {} or its reason, and never an actor", async () => {
  const sent = stubFetch(() => Response.json({ change_id: "c2", created: false }));
  await removeRelationEdit(EDGE, { done: "Removed", reason: "" }).request.send();
  await removeRelationEdit(EDGE, { done: "Removed", reason: "duplicate" }).request.send();
  expect(sent.map(({ method, path, body }) => [method, path, body])).toEqual([
    ["DELETE", PATH, {}],
    ["DELETE", PATH, { reason: "duplicate" }],
  ]);
  expect(sent[0]!.headers["idempotency-key"]).not.toBe(sent[1]!.headers["idempotency-key"]);
});

test("supersede with a new inquiry is one batch: the row, keyed in the body, and its supersedes edge", async () => {
  const sent = stubFetch(() => Response.json({ ids: [uuid(9)] }));
  const old = { ...row("Belief", 4), kind: "Belief" } as const;
  const edit = supersedeWithNewEdit(old, { title: "Sharper claim", description: "" });
  expect(edit.done).toBe("Created a new belief that supersedes Belief#4");
  expect(await edit.request.send()).toEqual({ ids: [uuid(9)] });
  expect(edit.request.route).toBe("batch");
  expect(edit.touches).toEqual([old.id]);
  const [request] = sent;
  expect(request).toMatchObject({ method: "POST", path: "/api/inquiries/batch" });
  expect(request!.headers["idempotency-key"]).toBeUndefined();
  const body = request!.body as { items: { idempotency_key: string }[] };
  expect(body).toEqual({
    items: [{ kind: "Belief", title: "Sharper claim", idempotency_key: body.items[0]!.idempotency_key }],
    edges: [{ edge_kind: "supersedes", from_index: 0, to_id: old.id }],
  });
  await supersedeWithNewEdit(old, { title: "Sharper claim", description: "Why." }).request.send();
  expect((sent[1]!.body as { items: object[] }).items[0]).toMatchObject({ description: "Why." });
});
