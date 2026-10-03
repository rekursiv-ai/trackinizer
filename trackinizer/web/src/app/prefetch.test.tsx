import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, expect, onTestFinished, test, vi } from "vitest";
import { type Sent, stubFetch } from "../api/testing";
import { openEarlyStream } from "../live/earlyStream";
import { FakeEventSource } from "../live/testing";
import { DetailView, GraphView } from "../router/views";
import { App } from "./App";
import { prefetch, prefetched, takeReadAt } from "./prefetch";
import { LOGIN_URL } from "./session";

// The graph draws on a canvas, which jsdom lacks: a renderer that draws nothing stands in.
vi.mock("../graph/renderer", async () => ({ forceGraphRenderer: new (await import("../graph/testing")).FakeRenderer().create }));

const ID = "00000000-0000-4000-8000-000000000007";
const BOOT = ["/api/me/profile", "/api/meta/edges", "/api/meta/enums", "/api/meta/fields"];
const PROFILE = { user_id: "u1", email: "ada@example.com", name: "Ada", role: "writer", last_login: null, visual_workspace_enabled: false };

const BODIES: { [path: string]: unknown } = {
  "/api/meta/enums": { inquiry_kind_all: ["Issue", "Paper", "Belief"], status: ["active", "complete"] },
  "/api/meta/fields": { priority: "issue", authors: "paper", judgement: "belief", confidence: "belief" },
  "/api/meta/edges": {},
  "/api/me/profile": PROFILE,
  "/api/inquiries/Issue/7": { id: ID, kind: "Issue", seq: 7 },
  [`/api/web/get/${ID}`]: {
    self: { id: ID, kind: "Issue", seq: 7, title: "Issue seven", status: "active", created: "2026-09-20T00:00:00+00:00", modified: "2026-09-20T00:00:00+00:00" },
    edges: {},
    backlinks: {},
    changes: [],
  },
  "/api/web/graph": {
    nodes: [{ id: ID, kind: "Issue", seq: 7, title: "Issue seven", status: "active", created: "2026-09-20T00:00:00+00:00" }],
    edges: [],
  },
};

/** Answer as the server would: a list read with one row of its kind, the rest from `BODIES`. */
function serve(answer: { [path: string]: () => Response } = {}): Sent[] {
  return stubFetch((request) => {
    const url = new URL(request.url);
    const custom = answer[url.pathname];
    if (custom) return custom();
    if (url.pathname !== "/api/inquiries") return Response.json(BODIES[url.pathname]);
    const kind = url.searchParams.get("kind")!;
    return Response.json([
      {
        id: "00000000-0000-4000-8000-000000000001",
        kind,
        seq: 1,
        title: `${kind} from the started read`,
        status: "active",
        owner: null,
        labels: null,
        marginal_cost: { agent_usd: 0, resource_usd: 0 },
        created: "2026-09-20T00:00:00+00:00",
        modified: "2026-09-20T00:00:00+00:00",
      },
    ]);
  });
}

/** Each request as path, and for a list read its filters, sorted. */
function reads(sent: readonly Sent[]): string[] {
  return sent
    .map(({ path, query }) => (path === "/api/inquiries" ? `${path} ${new URLSearchParams(query).getAll("filter").join(" ")}`.trim() : path))
    .sort();
}

const ACTIVE = '/api/inquiries {"field":"status","op":"is","value":"active"}';

// The detail and the graph are chunks of their own; loaded first, each shows at
// once, as it does once main.tsx has awaited it.
beforeAll(() => Promise.all([DetailView.preload(), GraphView.preload()]));

beforeEach(() => {
  history.replaceState(null, "", location.pathname);
  sessionStorage.clear();
  vi.stubGlobal("EventSource", FakeEventSource);
  // jsdom has none; a list scrolls its focused row into view.
  Element.prototype.scrollIntoView = () => {};
  // As in production, where the agent canvas is off by default: in development
  // it wraps every list and detail, with reads of its own.
  vi.stubEnv("DEV", false);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
  vi.useRealTimers();
});

/**
 * Open the app on `hash` as main.tsx does: start the first reads, see `started`
 * go out before the app renders, then render it. Returns the requests sent.
 */
async function open(
  hash: string,
  started: readonly string[],
  { answer = {}, assign = vi.fn() }: { answer?: { [path: string]: () => Response }; assign?: (url: string) => void } = {},
): Promise<Sent[]> {
  history.replaceState(null, "", hash || location.pathname);
  const sent = serve(answer);
  prefetch(location.hash);
  await waitFor(() => expect(reads(sent)).toEqual([...started].sort()), { interval: 1 });
  render(<App assign={assign} />);
  return sent;
}

test.each(["#/list/Issue", "#/list/issue", "#/list"])(
  "a link to a list (%j) starts its reads before the app, which shows them: none goes out twice",
  async (hash) => {
    const sent = await open(hash, [...BOOT, ACTIVE]);
    expect(await screen.findByText("Issue from the started read")).toBeTruthy();
    expect(location.hash).toBe("#/list/Issue");
    expect(reads(sent)).toEqual([...BOOT, ACTIVE].sort());
  },
);

test.each([
  ["", []],
  ["#/graph", []],
  // Opened on a focus, the graph selects it, and Peek reads its detail.
  ["#/graph?focus=Issue/7&hops=2", ["/api/inquiries/Issue/7", `/api/web/get/${ID}`]],
])(
  "a link to the graph (%j), the empty hash too, starts its read before the app, which shows it: none goes out twice",
  async (hash, peeked) => {
    const sent = await open(hash, [...BOOT, "/api/web/graph"]);
    expect(await screen.findByText("1 node")).toBeTruthy();
    expect(sent.find(({ path }) => path === "/api/web/graph")?.query).toBe("?limit=1000");
    await waitFor(() => expect(reads(sent)).toEqual([...BOOT, "/api/web/graph", ...peeked].sort()));
  },
);

test("a graph this tab has kept reads as kept: nothing is started for it", async () => {
  const kept = { limit: 100, hiddenKinds: [], hiddenStatuses: [], skipEdges: [], only: false, key: true, roots: true };
  sessionStorage.setItem("trackinizer.v2.graph", JSON.stringify(kept));
  const sent = await open("#/graph", BOOT);
  expect(await screen.findByText("1 node")).toBeTruthy();
  expect(sent.filter(({ path }) => path === "/api/web/graph").map(({ query }) => query)).toEqual(["?limit=100"]);
});

test("a Paper list opens on All, so the read started for it asks for every status", async () => {
  const sent = await open("#/list/Paper", [...BOOT, "/api/inquiries"]);
  expect(await screen.findByText("Paper from the started read")).toBeTruthy();
  expect(reads(sent)).toEqual([...BOOT, "/api/inquiries"].sort());
});

test("a list this tab has kept reads as kept: nothing is started for it", async () => {
  const kept = { tab: "closed", choices: [], grouping: "none", ordering: "created", pages: {}, collapsed: [], focus: null };
  sessionStorage.setItem("trackinizer.v2.list.Issue", JSON.stringify(kept));
  const sent = await open("#/list/Issue", BOOT);
  expect(await screen.findByText("Issue from the started read")).toBeTruthy();
  expect(reads(sent)).toEqual([...BOOT, '/api/inquiries {"field":"status","op":"ne","value":"active"}'].sort());
});

test("the started list read asks exactly what the list asks for itself", async () => {
  // The list's own first read: a tab that kept the default state starts none.
  const kept = { tab: "active", choices: [], grouping: "none", ordering: "created", pages: {}, collapsed: [], focus: null };
  sessionStorage.setItem("trackinizer.v2.list.Issue", JSON.stringify(kept));
  const own = await open("#/list/Issue", BOOT);
  await screen.findByText("Issue from the started read");
  cleanup();
  sessionStorage.clear();
  const started = await open("#/list/Issue", [...BOOT, ACTIVE]);
  const query = (sent: readonly Sent[]) => sent.find(({ path }) => path === "/api/inquiries")?.query;
  expect(query(started)).toBe(query(own));
});

test.each([
  ["#/ref/Issue/7", ["/api/inquiries/Issue/7", `/api/web/get/${ID}`]],
  [`#/lookup/${ID}`, [`/api/web/get/${ID}`]],
])("a link to a detail (%s) starts its reads before the app, which shows them: none goes out twice", async (hash, detail) => {
  const sent = await open(hash, [...BOOT, ...detail]);
  expect(await screen.findByRole("heading", { name: "Issue seven" })).toBeTruthy();
  expect(reads(sent)).toEqual([...BOOT, ...detail].sort());
});

test("a 401 on a started read sends the browser to sign in, as any read's does", async () => {
  const assign = vi.fn();
  const answer = { "/api/me/profile": () => Response.json({ detail: "not signed in" }, { status: 401 }) };
  const sent = await open("#/list/Issue", [...BOOT, ACTIVE], { answer, assign });
  await waitFor(() => expect(assign).toHaveBeenCalledWith(LOGIN_URL), { interval: 1 });
  expect(assign).toHaveBeenCalledTimes(1);
  expect(reads(sent).filter((path) => path === "/api/me/profile")).toHaveLength(1);
});

test("kinds the router cannot read show the app's crash screen, and a hash's wait for them rejects nothing", async () => {
  const unhandled: unknown[] = [];
  const record = (reason: unknown) => void unhandled.push(reason);
  process.on("unhandledRejection", record);
  const quiet = vi.spyOn(console, "error").mockImplementation(() => {});
  onTestFinished(() => {
    process.off("unhandledRejection", record);
    quiet.mockRestore();
  });
  // Server drift: a kind list that is not a list, which a kind in another case waits for to name its view.
  await open("#/list/issue", BOOT, { answer: { "/api/meta/enums": () => Response.json({ inquiry_kind_all: 5 }) } });
  expect((await screen.findByRole("alert")).textContent).toBe("kinds.find is not a function");
  expect(unhandled).toEqual([]);
});

test("a started read is its query's first fetch only: the next reads afresh", async () => {
  serve();
  history.replaceState(null, "", "#/activity");
  prefetch(location.hash);
  const read = vi.fn(async () => ({ ...PROFILE, name: "Fresh" }));
  for (const key of [["meta", "enums"], ["meta", "fields"], ["meta", "edges"]]) await prefetched(key, read);
  expect(await prefetched(["me", "profile"], read)).toEqual(PROFILE);
  expect(read).not.toHaveBeenCalled();
  expect(await prefetched(["me", "profile"], read)).toEqual({ ...PROFILE, name: "Fresh" });
  expect(read).toHaveBeenCalledTimes(1);
});

test("a started read no query has taken 10 s after load is dropped: a later query reads afresh", async () => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  serve();
  history.replaceState(null, "", "#/activity");
  prefetch(location.hash);
  vi.advanceTimersByTime(10_000);
  const read = vi.fn(async () => ({ ...PROFILE, name: "Fresh" }));
  expect(await prefetched(["me", "profile"], read)).toEqual({ ...PROFILE, name: "Fresh" });
  for (const key of [["meta", "enums"], ["meta", "fields"], ["meta", "edges"]]) await prefetched(key, read);
  expect(read).toHaveBeenCalledTimes(4);
});

test("a query that took a started read can learn once when it started, for the stream layer's gap recovery", async () => {
  vi.useFakeTimers({ toFake: ["Date"] });
  // The earlier tests' lists and details took reads too, and nothing asked.
  takeReadAt(() => true);
  serve();
  history.replaceState(null, "", "#/activity");
  vi.setSystemTime(1_000);
  prefetch(location.hash);
  vi.setSystemTime(2_000);
  const read = vi.fn(async () => PROFILE);
  await prefetched(["me", "profile"], read);
  const isProfile = (key: readonly unknown[]) => key[0] === "me";
  expect(takeReadAt((key) => key[0] === "inquiries")).toBeUndefined();
  expect(takeReadAt(isProfile)).toBe(1_000);
  expect(takeReadAt(isProfile)).toBeUndefined();
  for (const key of [["meta", "enums"], ["meta", "fields"], ["meta", "edges"]]) await prefetched(key, read);
});

/** Open every stream the page made that is not open yet: the app's, wherever it opened it. */
function openStreams() {
  for (const source of FakeEventSource.made) if (source.readyState !== 1) source.open();
}

/** The list and detail reads among `sent`, after the live layer has had 1 s to catch up. */
async function caughtUp(sent: readonly Sent[]): Promise<string[]> {
  await act(() => vi.advanceTimersByTimeAsync(1_000));
  return sent.map(({ path }) => path).filter((path) => !BOOT.includes(path));
}

test("a view that reads once the stream is open needs no catching up when it opens", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true, advanceTimeDelta: 2 });
  FakeEventSource.reset();
  const sent = serve();
  history.replaceState(null, "", "#/list/Issue");
  openEarlyStream();
  openStreams();
  render(<App assign={vi.fn()} />);
  await screen.findByText("Issue from the started read");
  openStreams();
  expect(await caughtUp(sent)).toEqual(["/api/inquiries"]);
});

test.each([
  ["#/list/Issue", "Issue from the started read", "/api/inquiries"],
  ["#/ref/Issue/7", "Issue seven", `/api/web/get/${ID}`],
  ["#/graph", "1 node", "/api/web/graph"],
])("a view on %s whose read started before the stream opened catches up when it opens", async (hash, shown, read) => {
  vi.useFakeTimers({ shouldAdvanceTime: true, advanceTimeDelta: 2 });
  FakeEventSource.reset();
  const sent = serve();
  history.replaceState(null, "", hash);
  openEarlyStream();
  prefetch(location.hash);
  await waitFor(() => expect(sent.map(({ path }) => path)).toContain(read), { interval: 1 });
  await act(() => vi.advanceTimersByTimeAsync(5));
  openStreams();
  render(<App assign={vi.fn()} />);
  await screen.findByText(shown);
  openStreams();
  expect((await caughtUp(sent)).filter((path) => path === read)).toEqual([read, read]);
});
