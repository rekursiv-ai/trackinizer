import { afterEach, expect, test, vi } from "vitest";
import { addEdge, clearEdgeAnnotation, type EdgeRef, patchEdgeLabels, removeEdge, setEdgeAnnotation } from "./edges";
import { keyed } from "./idempotency";
import { stubFetch } from "./testing";

const CHILD = "0b6f7c1e-2f7a-4c55-9d7e-1f0e6b1d2a33";
const PARENT = "5d3c2b1a-0f9e-4d8c-8b7a-6e5d4c3b2a19";
const EDGE: EdgeRef = { from: CHILD, kind: "narrows", to: PARENT };
const PATH = `/api/edges/${CHILD}/narrows/${PARENT}`;

afterEach(() => {
  vi.unstubAllGlobals();
});

/** The one request `sent` holds, with the key the header must carry. */
function only(sent: ReturnType<typeof stubFetch>, key: string) {
  expect(sent).toHaveLength(1);
  expect(sent[0]!.headers).toEqual({ "content-type": "application/json", "idempotency-key": key });
  return sent[0]!;
}

test("an add is a POST with its annotations in the body and its key in the header", async () => {
  const sent = stubFetch(() => Response.json({ change_id: "c1", created: true }));
  const write = keyed({ priority: 10, note: "first" });
  expect(await addEdge(EDGE, write)).toEqual({ change_id: "c1", created: true });
  expect(only(sent, write.key)).toMatchObject({ method: "POST", path: PATH, body: { priority: 10, note: "first" } });
});

test("an annotation is a PUT of its value to its own route", async () => {
  const sent = stubFetch(() => Response.json({ change_id: "c2", created: false }));
  const note = keyed({ value: "why", reason: "context" });
  await setEdgeAnnotation("note", EDGE, note);
  const valence = keyed({ value: -0.4 });
  await setEdgeAnnotation("valence", { ...EDGE, kind: "proves" }, valence);
  expect(sent.map(({ method, path, body, headers }) => [method, path, body, headers["idempotency-key"]])).toEqual([
    ["PUT", `${PATH}/note`, { value: "why", reason: "context" }, note.key],
    ["PUT", `/api/edges/${CHILD}/proves/${PARENT}/valence`, { value: -0.4 }, valence.key],
  ]);
});

test("clearing an annotation and removing an edge are DELETEs with a {} body", async () => {
  const sent = stubFetch(() => Response.json({ change_id: null, created: false }));
  const clear = keyed({});
  await clearEdgeAnnotation("priority", EDGE, clear);
  expect(only(sent, clear.key)).toMatchObject({ method: "DELETE", path: `${PATH}/priority`, body: {} });
  sent.length = 0;
  const remove = keyed({ reason: "duplicate" });
  await removeEdge(EDGE, remove);
  expect(only(sent, remove.key)).toMatchObject({ method: "DELETE", path: PATH, body: { reason: "duplicate" } });
});

test("a label changes one element at a time with PATCH", async () => {
  const sent = stubFetch(() => Response.json({ change_id: "c3", created: false }));
  const write = keyed({ op: "add" as const, value: "blocked" });
  await patchEdgeLabels(EDGE, write);
  expect(only(sent, write.key)).toMatchObject({
    method: "PATCH",
    path: `${PATH}/labels`,
    body: { op: "add", value: "blocked" },
  });
});

test("a retry sends the same key and body again", async () => {
  const sent = stubFetch(() => Response.json({ change_id: "c4", created: false }));
  const write = keyed({ value: 20 });
  await setEdgeAnnotation("priority", EDGE, write);
  await setEdgeAnnotation("priority", EDGE, write);
  expect(sent[1]).toEqual(sent[0]);
});

// Compile-time checks: `tsc --noEmit` reads them, and nothing calls this.
export async function rejected(): Promise<void> {
  // An actor, which the server must stamp itself,
  // @ts-expect-error
  await addEdge(EDGE, keyed({ note: "x", actor: "me" }));
  // @ts-expect-error
  await setEdgeAnnotation("note", EDGE, keyed({ value: "x", actor: "me" }));
  // compare-and-set, which no annotation has,
  // @ts-expect-error
  await setEdgeAnnotation("note", EDGE, keyed({ value: "x", mode: "cas", expected: "y" }));
  // a value of the wrong type,
  // @ts-expect-error
  await setEdgeAnnotation("valence", EDGE, keyed({ value: "high" }));
  // a whole list of labels, which changes one element at a time,
  // @ts-expect-error
  await setEdgeAnnotation("labels", EDGE, keyed({ value: ["a"] }));
  // and a body without its key.
  // @ts-expect-error
  await removeEdge(EDGE, {});
}
