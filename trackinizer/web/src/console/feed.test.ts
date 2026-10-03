import { QueryClient } from "@tanstack/react-query";
import { afterEach, expect, test, vi } from "vitest";
import type { FeedCursor, FeedEvent } from "../api/sessions";
import { stubFetch } from "../api/testing";
import { appendEvents, ConsoleFeed } from "./feed";

afterEach(() => {
  vi.unstubAllGlobals();
});

const A = "00000000-0000-4000-8000-00000000000a";
const B = "00000000-0000-4000-8000-00000000000b";

/** Record `seq` of `session`'s part `part`, as the feed sends one. */
function event(seq: number, { session = A, part = 0 } = {}): FeedEvent {
  return {
    session_id: session,
    actor: session === A ? "codex" : "claude",
    rooms: [],
    part,
    seq,
    kind: "AssistantMessage",
    created: `2026-10-01T10:00:${String(seq % 60).padStart(2, "0")}Z`,
    model: "m",
    message: { content: `Turn ${seq}` },
    text: `Turn ${seq}`,
  };
}

const cursor = (last: FeedEvent): FeedCursor => ({ created: last.created, session_id: last.session_id, part: last.part, seq: last.seq });

/** Every record: no agent, room, CLI or kind left out. */
const EVERYTHING = { actor: [], room: [], cli: [], kind: [] };

test("events append once each, by session, part and seq, and past 5,000 the oldest drop", () => {
  const first = appendEvents([], [event(0), event(1), event(0, { part: 1 }), event(0, { session: B })]);
  expect(first.map((held) => held.key)).toEqual([`${A}:0:0`, `${A}:0:1`, `${A}:1:0`, `${B}:0:0`]);
  expect(appendEvents(first, [event(1)])).toBe(first);
  const many = appendEvents(first, Array.from({ length: 5000 }, (_, k) => event(k + 2)));
  expect(many).toHaveLength(5000);
  expect([many[0]!.key, many.at(-1)!.key]).toEqual([`${A}:0:2`, `${A}:0:5001`]);
  expect(many[0]!.n).toBe(4);
  expect(many[0]!.record).toMatchObject({ idx: 2, kind: "AssistantMessage", payload: { content: "Turn 2" }, text: "Turn 2" });
});

test("live: the first read is the newest page; a stream batch reads past the cursor until a short page", async () => {
  const tail = [event(0), event(1)];
  const full = Array.from({ length: 300 }, (_, k) => event(k + 2));
  const sent = stubFetch((request) => {
    const query = new URL(request.url).searchParams;
    if (query.get("tail") === "true") return Response.json({ events: tail, next_after: cursor(tail.at(-1)!) });
    if (query.get("after_seq") === "1") return Response.json({ events: full, next_after: cursor(full.at(-1)!) });
    return Response.json({ events: [event(302)], next_after: cursor(event(302)) });
  });
  const feed = new ConsoleFeed(new QueryClient({ defaultOptions: { queries: { retry: false } } }), { live: true }, EVERYTHING, 4);
  const loading = feed.load();
  expect(await feed.update({ ids: new Set(["x"]), gap: false })).toEqual({ afterMs: 1000 });
  await loading;
  expect(feed.getSnapshot()).toMatchObject({ loading: false, error: null });
  expect(feed.getSnapshot().held).toHaveLength(2);
  expect(await feed.update({ ids: new Set(["x"]), gap: false })).toBeNull();
  expect(feed.getSnapshot().held).toHaveLength(303);
  expect(sent.map((request) => Object.fromEntries(new URLSearchParams(request.query)))).toEqual([
    { tail: "true", limit: "300" },
    { after_created: tail[1]!.created, after_session: A, after_part: "0", after_seq: "1", limit: "300" },
    { after_created: full.at(-1)!.created, after_session: A, after_part: "0", after_seq: "301", limit: "300" },
  ]);
});

test("Messages reads only the conversation, in place of its kinds, so its newest page is all lines it shows", async () => {
  const page = Array.from({ length: 300 }, (_, k) => event(k));
  const sent = stubFetch((request) => {
    const events = new URL(request.url).searchParams.has("after_seq") ? [] : page;
    return Response.json({ events, next_after: cursor(page.at(-1)!) });
  });
  const messages = { actor: ["codex"], room: [], cli: [], kind: ["AgentToAgentMessage", "AssistantMessage", "ContextState", "UserMessage"] };
  const feed = new ConsoleFeed(new QueryClient({ defaultOptions: { queries: { retry: false } } }), { live: true }, messages, 1);
  await feed.load();
  await feed.update({ ids: new Set(["x"]), gap: false });
  expect(feed.getSnapshot().held).toHaveLength(300);
  expect(sent.map((request) => Object.fromEntries(new URLSearchParams(request.query)))).toEqual([
    { tail: "true", conversation: "true", limit: "300", actor: "codex" },
    { conversation: "true", after_created: page.at(-1)!.created, after_session: A, after_part: "0", after_seq: "299", limit: "300", actor: "codex" },
  ]);
});

test("live: while the newest pages hold too few records the view's level shows, older pages are read, at most five", async () => {
  // Context bookkeeping, which + Calls hides.
  const hidden = (seq: number): FeedEvent => ({ ...event(seq), kind: "ContextState", message: { kind: "token_count" } });
  const page = Array.from({ length: 300 }, (_, k) => hidden(k + 1));
  const sent = stubFetch((request) => {
    const query = new URL(request.url).searchParams;
    if (query.has("after_seq")) return Response.json({ events: [], next_after: cursor(page.at(-1)!) });
    // The page up to `until` holds the first page's oldest again, as `until` includes it.
    const events = query.has("until") ? [event(0), page[0]!] : page;
    return Response.json({ events, next_after: cursor(events.at(-1)!) });
  });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const feed = new ConsoleFeed(client, { live: true }, EVERYTHING, 2);
  await feed.load();
  expect(feed.getSnapshot().held.map(({ key }) => key).slice(0, 2)).toEqual([`${A}:0:0`, `${A}:0:1`]);
  expect(feed.getSnapshot().held).toHaveLength(301);
  expect(sent.map((request) => Object.fromEntries(new URLSearchParams(request.query)))).toEqual([
    { tail: "true", limit: "300" },
    { tail: "true", until: page[0]!.created, limit: "300" },
  ]);
  // Past the cursor, as before: the older pages left it at the newest.
  await feed.update({ ids: new Set(["x"]), gap: false });
  expect(new URLSearchParams(sent.at(-1)!.query).get("after_seq")).toBe("300");

  // Nothing it shows in five full pages: it stops there.
  let seq = 10_000;
  const reads = stubFetch(() => {
    const events = Array.from({ length: 300 }, () => hidden(seq--)).toReversed();
    return Response.json({ events, next_after: cursor(events.at(-1)!) });
  });
  const empty = new ConsoleFeed(client, { live: true }, EVERYTHING, 2);
  await empty.load();
  expect([reads.length, empty.getSnapshot().held.length]).toEqual([5, 1500]);
});

test("live: a gap reads past the cursor, then the newest page again, and a record read again replaces the one held", async () => {
  let server = [event(0), event(1)];
  const sent = stubFetch((request) => {
    const query = new URL(request.url).searchParams;
    const events = query.get("tail") === "true" ? server : server.filter(({ seq }) => seq > Number(query.get("after_seq")));
    return Response.json({ events, next_after: cursor((events.at(-1) ?? server.at(-1))!) });
  });
  const feed = new ConsoleFeed(new QueryClient({ defaultOptions: { queries: { retry: false } } }), { live: true }, EVERYTHING, 4);
  await feed.load();
  // A restart writes record 0 again, under the same key, and capture adds record 2.
  server = [{ ...event(0), message: { content: "Restarted" }, text: "Restarted" }, event(1), event(2)];
  const texts = () => feed.getSnapshot().held.map(({ n, event }) => [n, event.text]);
  await feed.update({ ids: new Set(["x"]), gap: false });
  expect(texts()).toEqual([[0, "Turn 0"], [1, "Turn 1"], [2, "Turn 2"]]);
  await feed.update({ ids: new Set(), gap: true });
  expect(texts()).toEqual([[0, "Restarted"], [1, "Turn 1"], [2, "Turn 2"]]);
  expect(feed.getSnapshot().held[0]!.record.text).toBe("Restarted");
  expect(sent.map((request) => new URLSearchParams(request.query)).map((query) => [query.get("tail"), query.get("after_seq")])).toEqual([
    ["true", null],
    [null, "1"],
    [null, "2"],
    ["true", null],
  ]);
});

test("history: the window's first page, then the next on request, while a full page says there may be more", async () => {
  const page = Array.from({ length: 300 }, (_, k) => event(k));
  const sent = stubFetch((request) =>
    new URL(request.url).searchParams.has("after_seq")
      ? Response.json({ events: [event(300)], next_after: cursor(event(300)) })
      : Response.json({ events: page, next_after: cursor(page.at(-1)!) }),
  );
  const window = { live: false, since: "2026-10-01T09:00:00.000Z", until: null } as const;
  const feed = new ConsoleFeed(new QueryClient({ defaultOptions: { queries: { retry: false } } }), window, EVERYTHING, 4);
  await feed.load();
  expect(feed.getSnapshot().more).toBe(true);
  await feed.more();
  expect(feed.getSnapshot()).toMatchObject({ more: false });
  expect(feed.getSnapshot().held).toHaveLength(301);
  expect(sent.map((request) => Object.fromEntries(new URLSearchParams(request.query)))).toEqual([
    { since: window.since, limit: "300" },
    { after_created: page.at(-1)!.created, after_session: A, after_part: "0", after_seq: "299", since: window.since, limit: "300" },
  ]);
});

test("a filtered feed asks for each of its agents, rooms, CLIs and kinds, on its first page and past its cursor", async () => {
  const sent = stubFetch((request) =>
    Response.json({ events: new URL(request.url).searchParams.has("tail") ? [event(0)] : [], next_after: cursor(event(0)) }),
  );
  const filter = { actor: ["codex", "claude"], room: ["ops"], cli: ["codex"], kind: ["ToolCall", "UserMessage"] };
  const feed = new ConsoleFeed(new QueryClient({ defaultOptions: { queries: { retry: false } } }), { live: true }, filter, 4);
  await feed.load();
  await feed.update({ ids: new Set(["x"]), gap: false });
  const reads = sent.map((request) => new URLSearchParams(request.query));
  const filters = (query: URLSearchParams) => ["actor", "room", "cli", "kind"].map((name) => query.getAll(name));
  expect(reads.map(filters)).toEqual([Object.values(filter), Object.values(filter)]);
  expect(reads.map((query) => [query.get("tail"), query.get("after_seq")])).toEqual([["true", null], [null, "0"]]);
});

test("a first page that fails says so, and a load reads it again", async () => {
  let refuse = true;
  stubFetch(() => (refuse ? Response.json({ detail: "statement timeout" }, { status: 500 }) : Response.json({ events: [event(0)], next_after: cursor(event(0)) })));
  const feed = new ConsoleFeed(new QueryClient({ defaultOptions: { queries: { retry: false } } }), { live: true }, EVERYTHING, 4);
  await feed.load();
  expect(feed.getSnapshot()).toMatchObject({ loading: false, error: { detail: "statement timeout" } });
  expect(await feed.update({ ids: new Set(["x"]), gap: false })).toBeNull();
  refuse = false;
  await feed.load();
  expect(feed.getSnapshot()).toMatchObject({ loading: false, error: null });
  expect(feed.getSnapshot().held).toHaveLength(1);
});
