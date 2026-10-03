import type { APIRequestContext, Page } from "@playwright/test";
import { expect, test } from "../fixtures";

// Phase 1 is done when every read of the old UI (the deleted
// server/assets/index.html) has a place in v2. This file makes one inquiry of
// each kind with every field the old detail page shows set, relations with every
// annotation it shows, and a status change with a reason, then finds each of
// them in v2's detail and list rows, and follows the old UI's own link forms. The
// server is shared with the other spec files, so every row carries a tag of this
// run's.

const TAG = `p1-${crypto.randomUUID().slice(0, 8)}`;
const SHA = "0123456789abcdef0123456789abcdef01234567";
const LABELS = [TAG, "second", "third"];
const COMMON = { owner: "josh", labels: LABELS, subscribers: ["Agent"], marginal_cost: { agent_usd: 1.25, resource_usd: 0.5 } };
const ITEMS = [
  {
    kind: "Issue",
    title: `Issue ${TAG}`,
    description: "Links Belief#1 and **bold** text.",
    issue_kind: ["bug"],
    validation: "The phase 1 journey passes.",
    priority: 10,
  },
  { kind: "Belief", title: `Belief ${TAG}`, judgement: "proven", confidence: 0.7 },
  {
    kind: "Paper",
    title: `Paper ${TAG}`,
    abstract: "An abstract worth reading.",
    authors: ["Ada Lovelace", "Alan Turing"],
    publication_type: "inproceedings",
    venue: "NeurIPS",
    subvenue: "Workshop on graphs",
    publish_date: "2024-12-10",
    source: "doi:10.1000/p1",
    google_scholar_cluster_id: "cluster-p1",
    google_scholar_cites_id: "cites-p1",
  },
  { kind: "CodeChange", title: `CodeChange ${TAG}`, sha: SHA },
  { kind: "WebResult", title: `WebResult ${TAG}`, url: "https://example.com/p1" },
  { kind: "WebSearch", title: `WebSearch ${TAG}`, query: "parity query", provider: "arxiv" },
  { kind: "Artifact", title: `Artifact ${TAG}` },
] as const;
const [ISSUE, BELIEF, PAPER, CODECHANGE, WEBRESULT, WEBSEARCH, ARTIFACT] = ITEMS.map((_, n) => n);

/** Ids by item index, then the Experiment's and the AgentSession's. */
let ids: string[] = [];
let experiment = "";
let session = "";

async function post(request: APIRequestContext, path: string, data: object): Promise<{ [key: string]: unknown }> {
  const response = await request.post(path, { data });
  expect(response.ok(), await response.text()).toBe(true);
  return response.json();
}

test.beforeAll(async ({ request }) => {
  const items = ITEMS.map((item) => ({ ...item, ...COMMON, idempotency_key: crypto.randomUUID() }));
  const edges = [
    { from_index: PAPER, to_index: BELIEF, edge_kind: "proves", valence: 0.8, note: "Strong evidence", labels: ["key"] },
    { from_index: WEBRESULT, to_index: BELIEF, edge_kind: "favors", valence: -0.4 },
  ];
  ids = (await post(request, "/api/inquiries/batch", { items, edges })).ids as string[];
  // Created on their own: an Experiment names its code changes by id, and a
  // session starts through the sessions route, as `trax run` starts one.
  experiment = (
    await post(request, "/api/inquiries/experiment", {
      title: `Experiment ${TAG}`,
      outcome: "Every read has a place.",
      config: { lr: 0.25, steps: 3 },
      codechanges: [ids[CODECHANGE]],
      ...COMMON,
      idempotency_key: crypto.randomUUID(),
    })
  ).id as string;
  session = (
    await post(request, "/api/sessions/start", {
      cli: "codex",
      title: `Session ${TAG}`,
      // The server resumes a session by its CLI's id, so a fixed one would hand a
      // rerun of this setup the last run's session, under the last run's title.
      cli_session_id: `sess-${TAG}`,
      rooms: ["room-p1"],
      // Stored as given: a start that names no time stores none.
      started: "2026-09-26T10:00:00+00:00",
      idempotency_key: crypto.randomUUID(),
    })
  ).id as string;
  await post(request, `/api/sessions/${session}/end`, { ended: "2026-09-26T11:30:00+00:00" });
  const closed = await request.put(`/api/inquiries/${ids[ARTIFACT]}/status`, {
    data: { value: "complete", reason: `Closed in ${TAG}.` },
  });
  expect(closed.ok(), await closed.text()).toBe(true);
});

const heading = (page: Page) => page.getByRole("heading", { level: 1 });
const properties = (page: Page) => page.getByRole("complementary", { name: "Properties" });
const field = (page: Page, name: string) => page.locator(`[data-field="${name}"]`);

async function open(page: Page, id: string, title: string) {
  await page.goto(`/app/#/lookup/${id}`);
  await expect(heading(page)).toHaveText(title);
}

/** The common fields the old detail shows on every kind: identity, people, labels, cost, times. */
async function expectCommon(page: Page, id: string) {
  const panel = properties(page);
  await expect(panel.locator('[data-field="owner"] dd')).toContainText("josh");
  await expect(panel.locator('[data-field="subscribers"] dd')).toContainText("Agent");
  await expect(panel.locator('[data-field="labels"] dd')).toHaveText(LABELS.join(""));
  await expect(panel.locator('[data-field="account"] dd')).toHaveText("no-auth@localhost");
  await expect(panel.locator('[data-field="marginal_cost_agent_usd"] dd')).toHaveText("$1.25");
  await expect(panel.locator('[data-field="marginal_cost_resource_usd"] dd')).toHaveText("$0.50");
  for (const name of ["ID", "Created", "Updated"]) await expect(panel.getByText(name, { exact: true })).toBeVisible();
  await expect(panel.getByText(id, { exact: true })).toBeVisible();
}

/** The href of the "Open <label>" link beside a field, which the old UI drew as the value's own link. */
const opens = (page: Page, label: string) => properties(page).getByRole("link", { name: `Open ${label}` });

test("an Issue shows its type, priority, done-when, status and Markdown description", async ({ page }) => {
  await open(page, ids[ISSUE], `Issue ${TAG}`);
  await expectCommon(page, ids[ISSUE]);
  const panel = properties(page);
  await expect(panel.locator('[data-field="status"] dd')).toHaveText("Active");
  await expect(panel.locator('[data-field="priority"] dd')).toHaveText("P1 High10");
  await expect(panel.locator('[data-field="issue_kind"] dd')).toHaveText("bug");
  await expect(field(page, "validation")).toContainText("The phase 1 journey passes.");
  const description = field(page, "description");
  await expect(description.getByRole("link", { name: "Belief#1" })).toHaveAttribute("href", "#/ref/Belief/1");
  await expect(description.locator("strong")).toHaveText("bold");
});

test("a Belief shows its judgement, the author's and the evidence's confidence, and annotated evidence", async ({
  page,
}) => {
  await open(page, ids[BELIEF], `Belief ${TAG}`);
  await expectCommon(page, ids[BELIEF]);
  const panel = properties(page);
  await expect(panel.locator('[data-field="judgement"] dd')).toHaveText("Proven");
  await expect(panel.locator('[data-field="confidence"] dd')).toHaveText("0.70");
  await expect(panel.locator('[data-field="evidence_confidence"] dd')).toHaveText(/^0\.\d\d$/);
  // The old page's edge line: the peer's ref, status and title, then valence, labels and note.
  const proved = page.getByRole("group", { name: "Proved by", exact: true });
  await expect(proved).toContainText(`Paper ${TAG}`);
  await expect(proved).toContainText("+0.8");
  await expect(proved).toContainText("Strong evidence");
  await expect(proved).toContainText("key");
  await expect(proved.getByRole("img", { name: "Active" })).toBeVisible();
  await expect(page.getByRole("group", { name: "Favored by", exact: true })).toContainText("-0.4");
});

test("a Paper shows its bibliography, with source and Scholar ids as links", async ({ page }) => {
  await open(page, ids[PAPER], `Paper ${TAG}`);
  await expectCommon(page, ids[PAPER]);
  const panel = properties(page);
  await expect(panel.locator('[data-field="authors"] dd')).toHaveText("Ada LovelaceAlan Turing");
  await expect(panel.locator('[data-field="publication_type"] dd')).toHaveText("inproceedings");
  await expect(panel.locator('[data-field="venue"] dd')).toHaveText("NeurIPS");
  await expect(panel.locator('[data-field="subvenue"] dd')).toHaveText("Workshop on graphs");
  await expect(panel.locator('[data-field="publish_date"] dd')).toHaveText("Dec 10, 2024");
  await expect(panel.locator('[data-field="source"] dd')).toContainText("doi:10.1000/p1");
  await expect(opens(page, "Source")).toHaveAttribute("href", "https://doi.org/10.1000/p1");
  await expect(opens(page, "Scholar cluster")).toHaveAttribute("href", "https://scholar.google.com/scholar?cluster=cluster-p1");
  await expect(opens(page, "Scholar cites")).toHaveAttribute("href", "https://scholar.google.com/scholar?cites=cites-p1");
  await expect(field(page, "abstract")).toContainText("An abstract worth reading.");
  await expect(page.getByRole("group", { name: "Proves", exact: true })).toContainText(`Belief ${TAG}`);
});

test("an Experiment shows its outcome, its code changes and its config", async ({ page }) => {
  await open(page, experiment, `Experiment ${TAG}`);
  await expectCommon(page, experiment);
  await expect(field(page, "outcome")).toContainText("Every read has a place.");
  const changes = properties(page).locator('[data-field="codechanges"] dd').getByRole("link");
  await expect(changes).toHaveText(ids[CODECHANGE].slice(0, 8));
  await expect(changes).toHaveAttribute("href", `#/lookup/${ids[CODECHANGE]}`);
  await expect(field(page, "config")).toContainText('"lr": 0.25');
});

test("a CodeChange, a WebResult and a WebSearch show their own fields", async ({ page }) => {
  await open(page, ids[CODECHANGE], `CodeChange ${TAG}`);
  await expectCommon(page, ids[CODECHANGE]);
  await expect(properties(page).locator('[data-field="sha"] dd')).toHaveText(SHA);

  await open(page, ids[WEBRESULT], `WebResult ${TAG}`);
  await expectCommon(page, ids[WEBRESULT]);
  await expect(properties(page).locator('[data-field="url"] dd')).toHaveText("https://example.com/p1");
  await expect(opens(page, "URL")).toHaveAttribute("href", "https://example.com/p1");

  await open(page, ids[WEBSEARCH], `WebSearch ${TAG}`);
  await expectCommon(page, ids[WEBSEARCH]);
  await expect(properties(page).locator('[data-field="query"] dd')).toHaveText("parity query");
  await expect(properties(page).locator('[data-field="provider"] dd')).toHaveText("arxiv");
});

test("an AgentSession shows its CLI, session id, start, end and rooms", async ({ page }) => {
  await open(page, session, `Session ${TAG}`);
  const panel = properties(page);
  await expect(panel.locator('[data-field="cli"] dd')).toHaveText("codex");
  await expect(panel.locator('[data-field="cli_session_id"] dd')).toHaveText(`sess-${TAG}`);
  await expect(panel.locator('[data-field="rooms"] dd')).toHaveText("room-p1");
  for (const name of ["started", "ended"]) {
    await expect(panel.locator(`[data-field="${name}"] dd`)).toHaveText(/^[A-Z][a-z]{2} \d{1,2}, \d{4}, \d{1,2}:\d\d [AP]M$/);
  }
});

test("a change's actor, kind and reason show in the detail's activity", async ({ page }) => {
  await open(page, ids[ARTIFACT], `Artifact ${TAG}`);
  await expect(properties(page).locator('[data-field="status"] dd')).toHaveText("Complete");
  const line = page.locator(".timeline li").filter({ hasText: `Closed in ${TAG}.` });
  await expect(line).toContainText("no-auth@localhost");
  await expect(line).toContainText(/complete/i);
  await expect(line.locator("blockquote")).toHaveText(`Closed in ${TAG}.`);
});

/** Filter the open list to this run's rows, by the tag label typed into the menu. */
async function filterToTag(page: Page) {
  await page.getByRole("button", { name: /^Filter$/ }).click();
  await page.getByRole("option", { name: "Label", exact: true }).click();
  await page.getByRole("combobox").fill(TAG);
  await page.keyboard.press("Enter");
  await page.keyboard.press("Escape");
}

test("list rows show what the old list's columns did: ref, status, type, priority, judgement, confidence, labels, owner, time", async ({
  page,
}) => {
  await page.goto("/app/#/list/Issue");
  await filterToTag(page);
  const issue = page.locator("a.row").filter({ hasText: `Issue ${TAG}` });
  await expect(issue.locator(".row-ref")).toHaveText(/^#\d+$/);
  await expect(issue.getByRole("img", { name: "P1 High" })).toBeVisible();
  await expect(issue.getByRole("img", { name: "Active" })).toBeVisible();
  await expect(issue.locator(".row-meta")).toContainText("bug");
  await expect(issue.locator(".row-meta")).toContainText(`${TAG}secondthird`);
  await expect(issue.locator(".row-owner")).toHaveAttribute("aria-label", "josh");
  await expect(issue.locator(".row-date")).toHaveAttribute("title", /^Updated [A-Z][a-z]{2} \d{1,2}, \d{4}/);

  await page.goto("/app/#/list/Belief");
  await filterToTag(page);
  const belief = page.locator("a.row").filter({ hasText: `Belief ${TAG}` });
  await expect(belief.getByRole("img", { name: "Proven" })).toBeVisible();
  await expect(belief.locator(".row-meta")).toContainText("0.70");
});

test("the old UI's links open their v2 views: #/inquiry/<uuid>, #/search?q= and palette jumps", async ({ page }) => {
  await page.goto(`/app/#/inquiry/${ids[ISSUE]}`);
  await expect(heading(page)).toHaveText(`Issue ${TAG}`);
  expect(new URL(page.url()).hash).toBe(`#/lookup/${ids[ISSUE]}`);

  const palette = page.getByRole("dialog", { name: "Command menu" });
  await page.goto(`/app/#/search?q=${encodeURIComponent(`WebSearch ${TAG}`)}`);
  await expect(heading(page)).toHaveText(`Search: WebSearch ${TAG} (1)`);
  expect(new URL(page.url()).hash).toBe(`#/search/${encodeURIComponent(`WebSearch ${TAG}`)}`);
  await page.getByRole("table").getByRole("link", { name: /^WebSearch#/ }).click();
  await expect(heading(page)).toHaveText(`WebSearch ${TAG}`);

  // The old header box took a Kind#seq or a UUID and went straight to it.
  const seq = ((await (await page.request.get(`/api/web/get/${ids[PAPER]}`)).json()) as { self: { seq: number } }).self.seq;
  for (const [typed, title] of [
    [`Paper#${seq}`, `Paper ${TAG}`],
    [ids[BELIEF], `Belief ${TAG}`],
  ]) {
    await page.keyboard.press("ControlOrMeta+k");
    await page.keyboard.type(typed);
    await expect(palette.getByRole("group", { name: "Jump to" })).toBeVisible();
    await page.keyboard.press("Enter");
    await expect(heading(page)).toHaveText(title);
  }
});
