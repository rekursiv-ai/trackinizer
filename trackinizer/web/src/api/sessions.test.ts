import { afterEach, expect, test, vi } from "vitest";
import { listSessionParts, readFeed, readFeedFacets, readSessionRecords, sendRoutedMessage, sendSessionMessage } from "./sessions";
import { stubFetch } from "./testing";

afterEach(() => {
  vi.unstubAllGlobals();
});

const ID = "00000000-0000-4000-8000-000000000001";

test("the parts listing is a GET with no body; part -1 comes back like any other", async () => {
  const parts = [
    { part: -1, name: "legacy", format: "", records: 2, metadata: {}, ir_id: ID },
    { part: 0, name: "s.jsonl", format: "claude", records: 250, metadata: {}, ir_id: null },
  ];
  const sent = stubFetch(() => Response.json({ parts }));
  expect(await listSessionParts(ID)).toEqual(parts);
  expect(sent).toEqual([
    { method: "GET", path: `/api/sessions/${ID}/parts`, query: "", headers: {}, body: undefined },
  ]);
});

test("a page of records names its part, the idx it follows, and asks for plaintext only", async () => {
  const records = [{ idx: 200, kind: "UserMessage", payload: {}, text: "hi" }];
  const sent = stubFetch(() => Response.json({ part: -1, records }));
  expect(await readSessionRecords(ID, { part: -1, afterIdx: 199, limit: 200 })).toEqual(records);
  expect(sent.map((request) => [request.method, request.path, Object.fromEntries(new URLSearchParams(request.query))])).toEqual([
    ["GET", `/api/sessions/${ID}/records`, { part: "-1", after_idx: "199", limit: "200", plaintext_only: "true" }],
  ]);
});

test("a session that is gone fails with the server's message", async () => {
  stubFetch(() => Response.json({ detail: "session not found" }, { status: 404 }));
  await expect(listSessionParts(ID)).rejects.toMatchObject({ status: 404, detail: "session not found" });
});

test("a message to a live session is one keyed POST of its text; the sender is the server's to attest", async () => {
  const sent = stubFetch(() => Response.json({ queued: 2 }));
  expect(await sendSessionMessage(ID, "Please stop.", "11111111-2222-4333-8444-555555555555")).toEqual({ queued: 2 });
  expect(sent).toEqual([
    {
      method: "POST",
      path: `/api/sessions/${ID}/inbound`,
      query: "",
      headers: { "content-type": "application/json", "idempotency-key": "11111111-2222-4333-8444-555555555555" },
      body: { text: "Please stop." },
    },
  ]);
});

test("a session no trax run is polling refuses a message with 409 and the server's reason", async () => {
  stubFetch(() => Response.json({ detail: "No active inbound poller" }, { status: 409 }));
  await expect(sendSessionMessage(ID, "hi", "11111111-2222-4333-8444-555555555555")).rejects.toMatchObject({
    status: 409,
    detail: "No active inbound poller",
  });
});

test("the feed's first live page is its newest; later pages resume past the whole cursor; a window bounds both ends", async () => {
  const sent = stubFetch(() => Response.json({ events: [], next_after: null }));
  expect(await readFeed({ tail: true, limit: 300 })).toEqual({ events: [], next_after: null });
  const cursor = { created: "2026-10-01T10:00:00Z", session_id: ID, part: 1, seq: 7 };
  await readFeed({ after: cursor, limit: 300 });
  await readFeed({ since: "2026-10-01T09:00:00.000Z", until: "2026-10-01T10:00:00.000Z", limit: 300 });
  expect(sent.map((request) => [request.method, request.path, Object.fromEntries(new URLSearchParams(request.query))])).toEqual([
    ["GET", "/api/web/feed", { tail: "true", limit: "300" }],
    ["GET", "/api/web/feed", { after_created: "2026-10-01T10:00:00Z", after_session: ID, after_part: "1", after_seq: "7", limit: "300" }],
    ["GET", "/api/web/feed", { since: "2026-10-01T09:00:00.000Z", until: "2026-10-01T10:00:00.000Z", limit: "300" }],
  ]);
});

test("each feed filter repeats its parameter once per value, and an empty one is left out", async () => {
  const sent = stubFetch(() => Response.json({ events: [], next_after: null }));
  await readFeed({ tail: true, limit: 300, actor: ["a", "b"], room: ["lab"], cli: [], kind: ["ToolCall", "UserMessage"] });
  expect(sent.map((request) => [request.path, [...new URLSearchParams(request.query)]])).toEqual([
    [
      "/api/web/feed",
      [["tail", "true"], ["limit", "300"], ["actor", "a"], ["actor", "b"], ["room", "lab"], ["kind", "ToolCall"], ["kind", "UserMessage"]],
    ],
  ]);
});

test("the feed keeps only conversation when asked, and says nothing otherwise", async () => {
  const sent = stubFetch(() => Response.json({ events: [], next_after: null }));
  await readFeed({ tail: true, limit: 300, conversation: true });
  await readFeed({ tail: true, limit: 300, conversation: false });
  expect(sent.map((request) => [...new URLSearchParams(request.query)])).toEqual([
    [["tail", "true"], ["conversation", "true"], ["limit", "300"]],
    [["tail", "true"], ["limit", "300"]],
  ]);
});

test("the facets count a window, either end open, under the feed's filters", async () => {
  const facets = {
    actors: [
      { actor: "a", session_id: ID, cli: "claude", rooms: ["lab"], count: 8, conversation: 4, last: "2026-10-01T10:08:00Z", ended: null },
    ],
    rooms: [{ room: "lab", count: 8, actors: ["a"] }],
    kinds: [{ kind: "ToolCall", count: 8 }],
  };
  const sent = stubFetch(() => Response.json(facets));
  expect(await readFeedFacets({ since: "2026-10-01T10:00:00.000Z", cli: ["claude"] })).toEqual(facets);
  await readFeedFacets({});
  expect(sent.map((request) => [request.method, request.path, [...new URLSearchParams(request.query)]])).toEqual([
    ["GET", "/api/web/feed/facets", [["since", "2026-10-01T10:00:00.000Z"], ["cli", "claude"]]],
    ["GET", "/api/web/feed/facets", []],
  ]);
});

test("a routed message is one keyed POST naming the agent, its room or null, and the text", async () => {
  const sent = stubFetch(() => Response.json({ delivered: [ID] }));
  expect(await sendRoutedMessage({ actor: "codex-arm", room: "ops" }, "hi", "11111111-2222-4333-8444-555555555555")).toEqual({ delivered: [ID] });
  await sendRoutedMessage({ actor: "codex-arm", room: null }, "hi", "11111111-2222-4333-8444-666666666666");
  expect(sent.map((request) => [request.method, request.path, request.headers["idempotency-key"], request.body])).toEqual([
    ["POST", "/api/messages", "11111111-2222-4333-8444-555555555555", { actor: "codex-arm", room: "ops", text: "hi" }],
    ["POST", "/api/messages", "11111111-2222-4333-8444-666666666666", { actor: "codex-arm", room: null, text: "hi" }],
  ]);
});
