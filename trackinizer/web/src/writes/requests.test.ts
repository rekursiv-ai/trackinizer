import { afterEach, describe, expect, test, vi } from "vitest";
import { ApiError } from "../api/client";
import { recentEvents } from "../debug/log";
import { createToken } from "../api/me";
import { type Sent, stubFetch } from "../api/testing";
import {
  addEdgeRequest,
  batchRequest,
  clearEdgeAnnotationRequest,
  clearFieldRequest,
  createRequest,
  patchEdgeLabelsRequest,
  patchFieldRequest,
  purgeRequest,
  removeEdgeRequest,
  sendWithRetries,
  setEdgeAnnotationRequest,
  setFieldRequest,
  type WriteRequest,
} from "./requests";

const ID = "0b6f7c1e-2f7a-4c55-9d7e-1f0e6b1d2a33";
const PARENT = "5d3c2b1a-0f9e-4d8c-8b7a-6e5d4c3b2a19";
const EDGE = { from: ID, kind: "narrows", to: PARENT };
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

/** Where a route carries its key: the header, the body, or each batch item. */
type KeyPlace = "header" | "body" | "items";

/** Every write route: how to make its request, and what the server must receive. */
const ROUTES: readonly [string, () => WriteRequest<unknown>, Omit<Sent, "headers" | "query">, KeyPlace][] = [
  [
    "set a field",
    () => setFieldRequest("/api/inquiries/{target_id}/title", ID, { value: "Renamed", reason: "typo" }),
    { method: "PUT", path: `/api/inquiries/${ID}/title`, body: { value: "Renamed", reason: "typo" } },
    "header",
  ],
  [
    "set a field by compare-and-set",
    () => setFieldRequest("/api/inquiries/{target_id}/status", ID, { value: "complete", mode: "cas", expected: "active" }),
    { method: "PUT", path: `/api/inquiries/${ID}/status`, body: { value: "complete", mode: "cas", expected: "active" } },
    "header",
  ],
  [
    "clear a field",
    () => clearFieldRequest("/api/issue/{target_id}/priority", ID),
    { method: "DELETE", path: `/api/issue/${ID}/priority`, body: {} },
    "header",
  ],
  [
    "add one element to a list field",
    () => patchFieldRequest("/api/inquiries/{target_id}/labels", ID, { op: "add", value: "webui" }),
    { method: "PATCH", path: `/api/inquiries/${ID}/labels`, body: { op: "add", value: "webui" } },
    "header",
  ],
  [
    "create a row",
    () => createRequest("Issue", { title: "Ship it", narrows: [[PARENT, 10]] }),
    { method: "POST", path: "/api/inquiries/issue", body: { title: "Ship it", narrows: [[PARENT, 10]] } },
    "body",
  ],
  [
    "create and link rows",
    () =>
      batchRequest(
        [
          { kind: "Artifact", title: "Answer" },
          { kind: "Issue", title: "Follow-up" },
        ],
        [{ from_index: 0, to_id: PARENT, edge_kind: "produced_by" }],
      ),
    {
      method: "POST",
      path: "/api/inquiries/batch",
      body: {
        items: [
          { kind: "Artifact", title: "Answer" },
          { kind: "Issue", title: "Follow-up" },
        ],
        edges: [{ from_index: 0, to_id: PARENT, edge_kind: "produced_by" }],
      },
    },
    "items",
  ],
  [
    "add an edge",
    () => addEdgeRequest(EDGE, { priority: 0 }),
    { method: "POST", path: `/api/edges/${ID}/narrows/${PARENT}`, body: { priority: 0 } },
    "header",
  ],
  [
    "annotate an edge",
    () => setEdgeAnnotationRequest("note", EDGE, { value: "why" }),
    { method: "PUT", path: `/api/edges/${ID}/narrows/${PARENT}/note`, body: { value: "why" } },
    "header",
  ],
  [
    "clear an edge annotation",
    () => clearEdgeAnnotationRequest("valence", EDGE),
    { method: "DELETE", path: `/api/edges/${ID}/narrows/${PARENT}/valence`, body: {} },
    "header",
  ],
  [
    "remove one edge label",
    () => patchEdgeLabelsRequest(EDGE, { op: "sub", value: "blocked" }),
    { method: "PATCH", path: `/api/edges/${ID}/narrows/${PARENT}/labels`, body: { op: "sub", value: "blocked" } },
    "header",
  ],
  [
    "remove an edge",
    () => removeEdgeRequest(EDGE),
    { method: "DELETE", path: `/api/edges/${ID}/narrows/${PARENT}`, body: {} },
    "header",
  ],
  [
    "purge a row",
    () => purgeRequest(ID, { reason: "duplicate" }),
    { method: "DELETE", path: `/api/inquiries/${ID}`, body: { reason: "duplicate" } },
    "header",
  ],
];

describe("every write route: method, path, body, key, and a retry that repeats both", () => {
  test.each(ROUTES)("%s", async (_, make, expected, keyPlace) => {
    const sent = stubFetch(() => Response.json({ id: ID, change_id: "c1" }));
    const request = make();
    await request.send();
    await request.send();
    await make().send();
    const [first, retry, next] = sent as [Sent, Sent, Sent];
    expect(retry).toEqual(first);

    const keys = (request: Sent) => keysOf(request, keyPlace);
    expect(keys(first).every((key) => UUID.test(key))).toBe(true);
    expect(keys(next).some((key) => keys(first).includes(key))).toBe(false);
    expect(new Set(keys(first)).size).toBe(keys(first).length);
    expect({ method: first.method, path: first.path, body: withoutKeys(first.body, keyPlace) }).toEqual(expected);
    expect(JSON.stringify(first.body)).not.toContain("actor");
  });
});

test("a write with no read to tell whether it landed is sent once, unless the server replays it: a create is", async () => {
  vi.useFakeTimers();
  const sent = stubFetch(() => Response.json({ detail: "database unavailable" }, { status: 503 }));
  const unread = sendWithRetries(setFieldRequest("/api/inquiries/{target_id}/title", ID, { value: "x" })).catch(() => {});
  await vi.runAllTimersAsync();
  await unread;
  expect(sent).toHaveLength(1);
  const create = sendWithRetries(createRequest("Issue", { title: "Ship it" })).catch(() => {});
  await vi.runAllTimersAsync();
  await create;
  expect(sent).toHaveLength(5);
});

test("an account write is sent once, even after a timeout or a 5xx", async () => {
  vi.useFakeTimers();
  const sent = stubFetch(() => Response.json({ detail: "database unavailable" }, { status: 503 }));
  const request: WriteRequest<unknown> = { route: "account", send: () => createToken({ name: "ci" }) };
  const failure = sendWithRetries(request).catch((error: unknown) => error);
  await vi.runAllTimersAsync();
  expect(await failure).toMatchObject({ status: 503, detail: "database unavailable" });
  expect(sent).toHaveLength(1);
});

test("a timeout, a dropped network or a 5xx is retried three times, 1, 3 and 9 s apart", async () => {
  vi.useFakeTimers();
  const answers = [
    () => Promise.reject(new TypeError("Failed to fetch")),
    () => Response.json({ detail: "database unavailable" }, { status: 503 }),
    () => new Response("bad gateway", { status: 502 }),
    () => Response.json({ id: ID, change_id: "c1" }),
  ];
  const sent = stubFetch(() => answers[sent.length - 1]!());
  const done = sendWithRetries(unsent(setFieldRequest("/api/inquiries/{target_id}/title", ID, { value: "x" })));
  const counts: number[] = [];
  for (const waitMs of [1000, 3000, 9000]) {
    await vi.advanceTimersByTimeAsync(waitMs - 1);
    counts.push(sent.length);
    await vi.advanceTimersByTimeAsync(1);
  }
  expect(await done).toEqual({ id: ID, change_id: "c1" });
  expect(counts).toEqual([1, 2, 3]);
  expect(sent).toHaveLength(4);
  expect(new Set(sent.map((request) => JSON.stringify(request))).size).toBe(1);
});

test("the fourth transient failure is thrown", async () => {
  vi.useFakeTimers();
  const sent = stubFetch(() => Response.json({ detail: "database unavailable" }, { status: 503 }));
  const failure = sendWithRetries(unsent(clearFieldRequest("/api/inquiries/{target_id}/description", ID))).catch(
    (error: unknown) => error,
  );
  await vi.runAllTimersAsync();
  expect(await failure).toMatchObject({ status: 503, detail: "database unavailable" });
  expect(sent).toHaveLength(4);
});

test("an answer from the server is not retried", async () => {
  const conflict = stubFetch(() => Response.json({ detail: "expected 'active'", code: "conflict" }, { status: 409 }));
  await expect(
    sendWithRetries(setFieldRequest("/api/inquiries/{target_id}/status", ID, { value: "complete" })),
  ).rejects.toMatchObject({ status: 409 });
  expect(conflict).toHaveLength(1);
});

test("a batch that fails is retried with the same item keys", async () => {
  vi.useFakeTimers();
  const answers = [() => Response.json({ detail: "database unavailable" }, { status: 503 }), () => Response.json({ ids: [ID] })];
  const sent = stubFetch(() => answers[sent.length - 1]!());
  const done = sendWithRetries(
    batchRequest([{ kind: "Issue", title: "Follow-up" }], [{ from_index: 0, to_id: PARENT, edge_kind: "narrows" }]),
  );
  await vi.runAllTimersAsync();
  expect(await done).toEqual({ ids: [ID] });
  expect(sent).toHaveLength(2);
  expect(sent[1]).toEqual(sent[0]);
});

test("a write logs its send, each automatic retry with its attempt, wait and key, and its final failure", async () => {
  vi.useFakeTimers();
  vi.spyOn(console, "warn").mockImplementation(() => {});
  stubFetch(() => Response.json({ detail: "database unavailable" }, { status: 503 }));
  const request = unsent(setFieldRequest("/api/inquiries/{target_id}/title", ID, { value: "A private title" }));
  const failure = sendWithRetries(request).catch((error: unknown) => error);
  await vi.runAllTimersAsync();
  const failed = (await failure) as ApiError;
  const events = recentEvents().filter(({ event, fields }) => event.startsWith("write.") && fields.key === request.key);
  expect(events.map(({ event, fields }) => [event, fields.route, fields.attempt, fields.wait_ms])).toEqual([
    ["write.send", "setField", undefined, undefined],
    ["write.retry", "setField", 2, 1000],
    ["write.retry", "setField", 3, 3000],
    ["write.retry", "setField", 4, 9000],
    ["write.failed", "setField", 4, undefined],
  ]);
  expect(events.at(-1)!.fields).toMatchObject({ status: 503, request_id: failed.sent!.id });
  expect(JSON.stringify(recentEvents())).not.toContain("private");
});

test("a Retry after attempts that got no answer reads first; one found landed resolves, logged, and is not sent again", async () => {
  vi.useFakeTimers();
  vi.spyOn(console, "warn").mockImplementation(() => {});
  const sent = stubFetch(() => Promise.reject(new TypeError("Failed to fetch")));
  let landed = false;
  const request = { ...removeEdgeRequest(EDGE), reconcile: async () => (landed ? { change_id: "c1", created: false } : null) };
  const failure = sendWithRetries(request).catch(() => {});
  await vi.runAllTimersAsync();
  await failure;
  expect(sent).toHaveLength(4);
  landed = true;
  expect(await sendWithRetries(request)).toEqual({ change_id: "c1", created: false });
  expect(sent).toHaveLength(4);
  const events = recentEvents().filter(({ fields }) => fields.key === request.key);
  expect(events.map(({ event }) => event)).toEqual([
    "write.send",
    "write.retry",
    "write.retry",
    "write.retry",
    "write.failed",
    "write.resend",
    "write.landed",
  ]);
});

/** `request`, whose read before a resend always finds it has not landed. */
function unsent<Result>(request: WriteRequest<Result>): WriteRequest<Result> {
  return { ...request, reconcile: async () => null };
}

function keysOf(request: Sent, place: KeyPlace): string[] {
  const body = request.body as { idempotency_key?: string; items?: { idempotency_key: string }[] };
  switch (place) {
    case "header":
      expect(body).not.toHaveProperty("idempotency_key");
      return [request.headers["idempotency-key"]!];
    case "body":
      expect(request.headers).not.toHaveProperty("idempotency-key");
      return [body.idempotency_key!];
    case "items":
      expect(request.headers).not.toHaveProperty("idempotency-key");
      return body.items!.map((item) => item.idempotency_key);
  }
}

/** `body` with its keys taken out, to compare with what the caller asked for. */
function withoutKeys(body: unknown, place: KeyPlace): unknown {
  const { idempotency_key: _, ...rest } = body as { idempotency_key?: string; items?: object[] };
  if (place !== "items") return rest;
  return { ...rest, items: rest.items!.map((item) => withoutKeys(item, "body")) };
}
