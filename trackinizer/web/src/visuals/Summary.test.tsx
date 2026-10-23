import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { stubFetch, type Sent } from "../api/testing";
import { Summary } from "./Summary";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const LEAD = "11111111-1111-4111-8111-111111111111";
const CHILD = "22222222-2222-4222-8222-222222222222";
const step = (start: number, end: number, title: string) => ({ start, end, title, text: `${title} text`, learned: "It was the cache." });
const session = (id: string, title: string, over: object = {}) => ({
  kind: "session", version: 1, session: id, ref: `AgentSession#${id === LEAD ? 1 : 2}`, state: "running",
  as_of: "2026-10-12T10:00:00+00:00", title, ask: "a", overview: `${title} overview`, outcome: "partial", items: 40,
  tool_calls: 9, errors: 1, learnings: 1,
  started: "2026-10-12T09:00:00+00:00", steps: [step(0, 9, "Read the loader"), step(10, 39, "Fix the cache")],
  phases: [step(0, 39, "Repair the loader")],
  beliefs: [{ id: "b0", claim: "The cache was stale.", verdict: "supported", confidence: 0.8, evidence: [{ at: 12, valence: 1 }, { at: 30, valence: -1 }] }],
  anchors: [{ item: 0, part: 0, idx: 0 }, { item: 9, part: 0, idx: 20 }, { item: 10, part: 0, idx: 24 }, { item: 12, part: 0, idx: 30 }, { item: 39, part: 0, idx: 90 }],
  ...over,
});
const row = (id: string, title: string, started: string) => ({
  session: id, ref: `AgentSession#${id === LEAD ? 1 : 2}`, title, outcome: "partial", started, items: 40,
  errors: 1, learnings: 1,
});
const corpus = {
  kind: "campaign", version: 1, campaign: "loader-fix", issue: "Issue#5", state: "running", as_of: "2026-10-12T10:00:00+00:00",
  sessions: [row(LEAD, "Fix the loader", "2026-10-12T09:00:00+00:00"), row(CHILD, "Check the cache", "2026-10-12T09:30:00+00:00")],
  efforts: [{ title: "Repair the cache", text: "Both sessions repaired it.", members: [
    { session: LEAD, title: "Repair the loader", text: "Lead part." }, { session: CHILD, title: "Check", text: "Child part." }] }],
  claims: [{ claim: "The cache was stale.", status: "contested", supporting: 1, refuting: 1, members: [
    { session: LEAD, claim: "The cache was stale.", verdict: "supported" }, { session: CHILD, claim: "Stale cache", verdict: "refuted" }] }],
};
const SERVICE = "campaign@service.invalid";
const rows = [corpus, session(LEAD, "Fix the loader"), session(CHILD, "Check the cache")].map((doc, i) => ({
  id: `row-${i}`, kind: "Artifact", seq: i + 1, title: "t", status: "complete", labels: [], account: SERVICE, description: JSON.stringify(doc),
}));
const record = (idx: number) => ({ idx, kind: idx % 2 ? "AssistantMessage" : "UserMessage", payload: {}, text: `record ${idx}`, context_id: null, timestamp: null, model: null, ciphertext: null });

type World = { readonly audiences?: readonly string[]; readonly refuse?: boolean; readonly summaries?: readonly object[] };

/** The server as the summary page meets it: summary rows by label, the profile, and the transcript routes. */
function serve(world: World = {}): Sent[] {
  return stubFetch((request) => {
    const url = new URL(request.url);
    if (url.pathname === "/api/me/profile") return Response.json({ read_audiences: world.audiences ?? ["open"], summary_accounts: [SERVICE] });
    if (url.pathname === "/api/inquiries") return Response.json(world.summaries ?? rows);
    if (world.refuse) return Response.json({ detail: { code: "session_audience", audience: "staff" } }, { status: 403 });
    if (url.pathname.endsWith("/parts")) return Response.json({ parts: [{ part: 0, name: "a.jsonl", format: "claude", records: 100, metadata: {} }] });
    const after = Number(url.searchParams.get("after_idx"));
    return Response.json({ part: 0, records: Array.from({ length: 80 }, (_, i) => record(after + 1 + i)) });
  });
}

function show(recordId: string | null = "ISSUE") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const instance = { id: "i", type: "trax.summary", version: 1, placement: "main" as const, record_id: recordId, params: {} };
  return render(<QueryClientProvider client={client}>
    <Summary instance={instance} canvas={null} onCanvasChanged={vi.fn()} focused />
  </QueryClientProvider>);
}

const openLead = async () => {
  fireEvent.click(await screen.findByRole("button", { name: "Fix the loader" }));
  return await screen.findByRole("article", { name: "Fix the loader" });
};

test("with no record it asks for one", () => {
  serve();

  show(null);

  expect(screen.getByText(/Choose a campaign or one of its sessions/)).toBeTruthy();
});

test("a record with no summary yet says so", async () => {
  serve({ summaries: [] });

  show();

  expect(await screen.findByText(/No summary has been written for this yet/)).toBeTruthy();
});

test("the sessions tab lists the sessions by start with their overviews and no cost or tree", async () => {
  serve();

  show();

  const table = await screen.findByRole("table");
  const lines = within(table).getAllByRole("row").slice(1).map((r) => r.textContent);
  expect(lines[0]).toContain("Fix the loader");
  expect(lines[0]).toContain("Fix the loader overview");
  expect(lines[1]).toContain("Check the cache");
  expect(screen.getByText("2 sessions")).toBeTruthy();
});

test("the filter keeps the sessions that match", async () => {
  serve();
  show();
  await screen.findByRole("table");

  fireEvent.change(screen.getByRole("searchbox", { name: "Filter sessions" }), { target: { value: "cache" } });

  expect(screen.queryByRole("button", { name: "Fix the loader" })).toBeNull();
  expect(screen.getByRole("button", { name: /Check the cache/ })).toBeTruthy();
});

test("opening a session shows its steps, phases and beliefs with the evidence's item numbers", async () => {
  serve();
  show();

  const article = await openLead();

  expect(within(article).getByRole("heading", { name: /Fix the loader/ })).toBeTruthy();
  expect(within(within(article).getByRole("region", { name: "Steps" })).getAllByRole("heading", { level: 4 }).map((h) => h.textContent))
    .toEqual(["Read the loader", "Fix the cache"]);
  expect(within(article).getByRole("region", { name: "Phases" }).textContent).toContain("Repair the loader");
  const beliefs = within(article).getByRole("region", { name: "Beliefs" });
  expect(beliefs.textContent).toContain("The cache was stale.");
  expect(within(beliefs).getByLabelText("supports").parentElement?.textContent).toContain("#12");
  expect(within(beliefs).getByLabelText("refutes").parentElement?.textContent).toContain("#30");
});

test("a reader without the staff audience gets item numbers as text and never asks for the transcript", async () => {
  const sent = serve({ audiences: ["open"] });
  show();

  const article = await openLead();
  await screen.findByRole("tablist");

  expect(within(article).queryByRole("region", { name: "Raw transcript" })).toBeNull();
  expect(within(article).queryByRole("button", { name: "#12" })).toBeNull();
  expect(within(article).queryByRole("button", { name: "#10–39" })).toBeNull();
  expect(within(article).getByText("#12")).toBeTruthy();
  expect(sent.some((s) => s.path.startsWith("/api/sessions/"))).toBe(false);
});

test("a staff reader sees the transcript, and a span points it at that span's records", async () => {
  const sent = serve({ audiences: ["open", "staff"] });
  show();
  const article = await openLead();
  const raw = await within(article).findByRole("region", { name: "Raw transcript" });
  await within(raw).findByText("record 0");

  fireEvent.click(within(article).getByRole("button", { name: "#10–39" }));

  await within(raw).findByText("record 21");
  const asked = sent.filter((s) => s.path.endsWith("/records")).map((s) => new URLSearchParams(s.query).get("after_idx"));
  expect(asked).toEqual(["-1", "20"]);
  const inSpan = within(raw).getAllByRole("listitem").filter((li) => li.className.includes("sum-raw-in")).map((li) => li.textContent);
  expect([inSpan[0], inSpan.at(-1)]).toEqual(expect.arrayContaining([expect.stringContaining("record 24"), expect.stringContaining("record 90")]));
  expect(inSpan.length).toBe(67);
  expect(within(article).getByRole("button", { name: "#10–39" }).getAttribute("aria-pressed")).toBe("true");
});

test("an item in a belief's evidence opens its record for a staff reader", async () => {
  const sent = serve({ audiences: ["open", "staff"] });
  show();
  const article = await openLead();
  await within(article).findByRole("region", { name: "Raw transcript" });

  fireEvent.click(within(within(article).getByRole("region", { name: "Beliefs" })).getByRole("button", { name: "#12" }));

  await waitFor(() => expect(sent.filter((s) => s.path.endsWith("/records")).map((s) => new URLSearchParams(s.query).get("after_idx")))
    .toContain("26"));
});

test("a reader the server refuses sees a note, whatever the page believed", async () => {
  serve({ audiences: ["open", "staff"], refuse: true });
  show();
  const article = await openLead();

  const raw = await within(article).findByRole("region", { name: "Raw transcript" });

  expect(await within(raw).findByText("The transcript is available to staff only.")).toBeTruthy();
  expect(within(raw).queryAllByRole("listitem")).toEqual([]);
});

test("the corpus tab groups efforts and claims and opens a member's session", async () => {
  serve();
  show();
  await screen.findByRole("table");

  fireEvent.click(screen.getByRole("tab", { name: "Corpus" }));

  const efforts = screen.getByRole("region", { name: "Efforts" });
  expect(efforts.textContent).toContain("Repair the cache");
  expect(efforts.textContent).toContain("2 sessions · 2");
  const claims = screen.getByRole("region", { name: "Beliefs" });
  expect(claims.textContent).toContain("contested");
  fireEvent.click(within(claims).getAllByRole("button", { name: "Check the cache" })[0]!);
  expect(await screen.findByRole("article", { name: "Check the cache" })).toBeTruthy();
  expect(screen.getByRole("tab", { name: "Session" }).getAttribute("aria-selected")).toBe("true");
});

test("without the campaign's own summary the sessions still list and the corpus tab is off", async () => {
  serve({ summaries: rows.slice(1) });

  show();

  expect(await screen.findByRole("button", { name: "Fix the loader" })).toBeTruthy();
  expect((screen.getByRole("tab", { name: "Corpus" }) as HTMLButtonElement).disabled).toBe(true);
  expect((screen.getByRole("tab", { name: "Session" }) as HTMLButtonElement).disabled).toBe(true);
});

test("the transcript pages later and earlier from where it opened", async () => {
  const sent = serve({ audiences: ["open", "staff"] });
  show();
  const article = await openLead();
  const raw = await within(article).findByRole("region", { name: "Raw transcript" });
  await within(raw).findByText("record 79");

  fireEvent.click(within(raw).getByRole("button", { name: "Later" }));
  await within(raw).findByText("record 159");
  fireEvent.click(within(raw).getByRole("button", { name: "Earlier" }));
  await within(raw).findByText("record 79");

  const asked = sent.filter((s) => s.path.endsWith("/records")).map((s) => new URLSearchParams(s.query).get("after_idx"));
  expect(asked).toEqual(["-1", "79", "-1"]);
  expect((within(raw).getByRole("button", { name: "Earlier" }) as HTMLButtonElement).disabled).toBe(true);
});

test("a campaign that has closed says so and stops refreshing", async () => {
  const closed = [{ ...corpus, state: "closed" }, session(LEAD, "Fix the loader", { state: "closed" })].map((doc, i) => ({
    id: `c-${i}`, kind: "Artifact", seq: i, title: "t", status: "complete", labels: [], account: SERVICE, description: JSON.stringify(doc),
  }));
  const sent = serve({ summaries: closed });

  show();

  expect(await screen.findByText("closed")).toBeTruthy();
  expect(sent.filter((s) => s.path === "/api/inquiries").length).toBe(1);
});

test("a row filed by someone who is not a trusted account is never shown", async () => {
  const forged = rows.map((r) => ({ ...r, account: "mallory@example.com" }));
  serve({ summaries: forged });

  show();

  expect(await screen.findByText(/No summary has been written for this yet/)).toBeTruthy();
});

test("a summary that cannot be read can be tried again", async () => {
  let failing = true;
  stubFetch((request) => {
    const url = new URL(request.url);
    if (url.pathname === "/api/me/profile") return Response.json({ read_audiences: ["open"], summary_accounts: [SERVICE] });
    if (failing) return Response.json({ detail: "down" }, { status: 400 });
    return Response.json(rows);
  });
  show();

  const retry = await screen.findByRole("button", { name: "Retry" });
  failing = false;
  fireEvent.click(retry);

  expect(await screen.findByRole("table")).toBeTruthy();
});
