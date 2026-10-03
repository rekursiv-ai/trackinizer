import { afterEach, describe, expect, test, vi } from "vitest";
import type { Peer } from "../api/detail";
import { editableFields } from "../api/fields";
import { stubFetch } from "../api/testing";
import { purgeEdit } from "../bulk/edits";
import { detail, peer, row, uuid } from "../detail/testing";
import { addRelationEdit, removeRelationEdit } from "../relations/edits";
import { edgeAnnotationEdit, edgeLabelEdit, type Edit, fieldEdit, listEdit } from "./edits";
import { batchRequest, createRequest, sendWithRetries, type WriteRequest } from "./requests";

// Every write kind, resent after an attempt that reached the server but whose
// answer was lost: the server replays none of these, so a read of the stored
// state decides (`reconcile`).

const ISSUE = editableFields("Issue");
const PAPER = editableFields("Paper");
const SELF = uuid(1);
const EDGE = { from: SELF, kind: "narrows", to: uuid(2) };

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

/**
 * What a read finds after the lost answer: the row's fields, the edge's
 * annotations, or neither, when `edge` is null (no edge) or `fields` is null (no
 * row: purged).
 */
type Stored = {
  readonly kind?: string;
  readonly fields?: { readonly [field: string]: unknown } | null;
  readonly edge?: Partial<Peer> | null;
};

/**
 * One write kind: its edit, and what a read finds when it landed, when it did
 * not, and when someone else changed it since. A state that is only there or
 * not (an element, an edge, a row) has no third value for someone else to
 * leave, so it has no `changed`.
 */
type Kind = readonly [string, () => Edit<unknown>, { landed: Stored; unsent: Stored; changed?: Stored }];

const KINDS: readonly Kind[] = [
  [
    "set a field",
    () => fieldEdit({ id: SELF, field: "priority", route: ISSUE.priority!, label: "Priority", from: 20, to: 10 }),
    { landed: { fields: { priority: 10 } }, unsent: { fields: { priority: 20 } }, changed: { fields: { priority: 30 } } },
  ],
  [
    "clear a field",
    () => fieldEdit({ id: SELF, field: "priority", route: ISSUE.priority!, label: "Priority", from: 20, to: null }),
    { landed: { fields: { priority: null } }, unsent: { fields: { priority: 20 } }, changed: { fields: { priority: 30 } } },
  ],
  [
    "set a compare-and-set field",
    () => fieldEdit({ id: SELF, field: "status", route: ISSUE.status!, label: "Status", from: "active", to: "complete" }),
    {
      landed: { fields: { status: "complete" } },
      unsent: { fields: { status: "active" } },
      changed: { fields: { status: "abandoned" } },
    },
  ],
  [
    "add an element to a list",
    () => listEdit({ id: SELF, field: "labels", route: ISSUE.labels!, label: "Labels", op: "add", value: "x" }),
    { landed: { fields: { labels: ["a", "x"] } }, unsent: { fields: { labels: ["a"] } } },
  ],
  [
    "remove an element from a list",
    () => listEdit({ id: SELF, field: "labels", route: ISSUE.labels!, label: "Labels", op: "sub", value: "x" }),
    { landed: { fields: { labels: ["a"] } }, unsent: { fields: { labels: ["a", "x"] } } },
  ],
  [
    "remove an Issue's last type",
    () => listEdit({ id: SELF, field: "issue_kind", route: ISSUE.issue_kind!, label: "Type", op: "sub", value: "bug", from: ["bug"] }),
    {
      landed: { fields: { issue_kind: null } },
      unsent: { fields: { issue_kind: ["bug"] } },
      changed: { fields: { issue_kind: ["bug", "feature"] } },
    },
  ],
  [
    "set an edge annotation",
    () => edgeAnnotationEdit({ edge: EDGE, annotation: "note", label: "Note", from: undefined, to: "why" }),
    { landed: { edge: { note: "why" } }, unsent: { edge: {} }, changed: { edge: { note: "theirs" } } },
  ],
  [
    "clear an edge annotation",
    () => edgeAnnotationEdit({ edge: EDGE, annotation: "priority", label: "Priority", from: 10, to: null }),
    { landed: { edge: {} }, unsent: { edge: { priority: 10 } }, changed: { edge: { priority: 30 } } },
  ],
  [
    "add an edge label",
    () => edgeLabelEdit({ edge: EDGE, op: "add", value: "k" }),
    { landed: { edge: { labels: ["k"] } }, unsent: { edge: {} } },
  ],
  [
    "remove an edge label",
    () => edgeLabelEdit({ edge: EDGE, op: "sub", value: "k" }),
    { landed: { edge: {} }, unsent: { edge: { labels: ["k"] } } },
  ],
  ["add an edge", () => addRelationEdit(EDGE), { landed: { edge: {} }, unsent: { edge: null } }],
  ["remove an edge", () => removeRelationEdit(EDGE, { done: "Removed", reason: "" }), { landed: { edge: null }, unsent: { edge: {} } }],
  ["purge a row", () => purgeEdit({ id: SELF, kind: "Issue", seq: 1 }, "dup"), { landed: { fields: null }, unsent: { fields: {} } }],
];

describe.each(KINDS)("%s, after an answer lost", (_, make, states) => {
  test("that landed: resolves with it, and is not sent again", async () => {
    const edit = make();
    const { writes, done } = resend(edit.request, states.landed);
    expect(await done).toMatchObject({ change_id: edit.request.key });
    expect(writes()).toHaveLength(1);
  });

  test("that did not land: sent again with the same key and body", async () => {
    const { writes, done } = resend(make().request, states.unsent);
    expect(await done).toEqual({ id: SELF, change_id: "c2", created: true });
    const [first, again] = writes();
    expect(again).toEqual(first);
  });

  const changed = states.changed;
  if (changed) {
    test("that someone else changed since: a 409 naming the change, and not sent again", async () => {
      const { writes, done } = resend(make().request, changed);
      await expect(done).rejects.toMatchObject({ status: 409, detail: expect.stringMatching(/^Not saved: someone changed /) });
      expect(writes()).toHaveLength(1);
    });
  }
});

test.each([
  ["a create", () => createRequest("Issue", { title: "Ship it" })],
  ["a batch", () => batchRequest([{ kind: "Issue", title: "Follow-up" }], [{ from_index: 0, to_id: uuid(2), edge_kind: "narrows" }])],
])("%s is sent again under the same keys without a read: the server replays them", async (_, make) => {
  const { writes, done, sent } = resend(make() as WriteRequest<unknown>, {});
  await done;
  const [first, again] = writes();
  expect(again).toEqual(first);
  expect(sent.filter((request) => request.method === "GET")).toEqual([]);
});

// A byline repeats, so a resend counts the author's copies against the list
// editing began from. Presence alone would read a copy someone else removed as
// unsent, and the resend would remove the other copy too.
test.each([
  ["someone else removed one copy: found landed, and not sent again", ["Bob", "Ada"], 1],
  ["it did not land: sent again", ["Ada", "Bob", "Ada"], 2],
])("removing a repeated author after an answer lost, where %s", async (_, authors, sends) => {
  const from = ["Ada", "Bob", "Ada"];
  const edit = listEdit({ id: SELF, field: "authors", route: PAPER.authors!, label: "Authors", op: "sub", value: "Ada", from });
  const { writes, done } = resend(edit.request, { kind: "Paper", fields: { authors } });
  await done;
  expect(writes()).toHaveLength(sends);
});

/**
 * Send `request` to a server whose first write reaches it and whose answer is
 * lost; reads then find `stored`, and a second write lands.
 */
function resend(request: WriteRequest<unknown>, { kind = "Issue", fields = {}, edge = null }: Stored) {
  vi.useFakeTimers();
  const sent = stubFetch((fetched) => {
    const path = new URL(fetched.url).pathname;
    if (fetched.method === "GET") {
      if (fields === null) return Response.json({ detail: "not found" }, { status: 404 });
      const self = row(kind, 1, fields);
      if (path === `/api/inquiries/${SELF}`) return Response.json(self);
      return Response.json(detail(self, { edges: edge ? { narrows: [peer("Issue", 2, edge)] } : {} }));
    }
    const writes = sent.filter((write) => write.method !== "GET");
    if (writes.length === 1) return Promise.reject(new TypeError("Failed to fetch"));
    return Response.json({ id: SELF, change_id: "c2", created: true });
  });
  const done = sendWithRetries(request);
  // Settled before the timers run, so a rejection is never unhandled meanwhile.
  done.catch(() => {});
  const ran = vi.runAllTimersAsync();
  return {
    sent,
    done: ran.then(() => done),
    writes: () => sent.filter((write) => write.method !== "GET"),
  };
}
