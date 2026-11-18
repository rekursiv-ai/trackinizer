import AxeBuilder from "@axe-core/playwright";
import type { APIRequestContext, Page } from "@playwright/test";
import { resetCanvas } from "./canvasState";
import { expect, revealBar, test } from "./fixtures";

// The campaign summary visual, read from a campaign's brief Issue: the sessions tree, one
// session, and the corpus. The rows are the Artifacts the summary service files, labelled as
// it labels them. This server has no accounts and answers every audience, so the test sets
// what the profile says: a reader of the open audience gets no transcript column and item
// numbers as text; a staff reader gets the column, served here, since the sessions are not real.
// That the server refuses the transcript to the first is tested where the audience is enforced.

test.use({ canvas: true, viewport: { width: 1600, height: 1000 } });

// Each test starts on a canvas of its own: the visuals another spec left (a campaign's timeline floats
// over the summary) are not this test's.
test.beforeEach(async ({ request }) => {
  await resetCanvas(request);
});

test.afterEach(async ({ request }) => {
  await resetCanvas(request);
});

const WCAG = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"];
const LEAD = "11111111-1111-4111-8111-111111111111";
const CHILD = "22222222-2222-4222-8222-222222222222";
const step = (start: number, end: number, title: string) => ({ start, end, title, text: `${title}: the loader read its cache again.`, learned: "The cache key ignored the file's mtime." });
const session = (id: string, ref: string, title: string) => ({
  kind: "session", version: 1, session: id, ref, state: "running", as_of: "2026-10-12T10:00:00+00:00", title, ask: "Fix it.",
  overview: `${title}: the loader served stale data until its cache key included the modified time.`, outcome: "partial", items: 40,
  tool_calls: 9, errors: 1, learnings: 1, started: "2026-10-12T09:00:00+00:00",
  steps: [step(0, 9, "Read the loader"), step(10, 39, "Fix the cache")], phases: [step(0, 39, "Repair the loader")],
  beliefs: [{ id: "b0", claim: "The cache was stale.", verdict: "supported", confidence: 0.8, evidence: [{ at: 12, valence: 1 }, { at: 30, valence: -1 }] }],
  anchors: [],
});
const row = (id: string, ref: string, title: string, started: string) => ({
  session: id, ref, title, outcome: "partial", started, items: 40, errors: 1, learnings: 1,
});
const corpus = {
  kind: "campaign", version: 1, campaign: "loader-fix", issue: "Issue#1", state: "running", as_of: "2026-10-12T10:00:00+00:00",
  sessions: [row(LEAD, "AgentSession#1", "Fix the loader", "2026-10-12T09:00:00+00:00"), row(CHILD, "AgentSession#2", "Check the cache", "2026-10-12T09:30:00+00:00")],
  efforts: [{ title: "Repair the cache", text: "Both sessions repaired it.", members: [
    { session: LEAD, title: "Repair the loader", text: "Lead part." }, { session: CHILD, title: "Check", text: "Child part." }] }],
  claims: [{ claim: "The cache was stale.", status: "contested", supporting: 1, refuting: 1, members: [
    { session: LEAD, claim: "The cache was stale.", verdict: "supported" }, { session: CHILD, claim: "Stale cache", verdict: "refuted" }] }],
};

async function seed(request: APIRequestContext): Promise<number> {
  const issue = await (await request.post("/api/inquiries/issue", { data: { title: "Fix the loader campaign", idempotency_key: crypto.randomUUID() } })).json() as { id: string };
  for (const doc of [corpus, session(LEAD, "AgentSession#1", "Fix the loader"), session(CHILD, "AgentSession#2", "Check the cache")]) {
    const made = await request.post("/api/inquiries/artifact", {
      data: {
        title: "Summary", description: JSON.stringify(doc), status: "complete", idempotency_key: crypto.randomUUID(),
        labels: ["summary", `summary:issue:${issue.id}`],
      },
    });
    expect(made.ok(), await made.text()).toBe(true);
  }
  const found = await (await request.get(`/api/inquiries/${issue.id}`)).json() as { seq: number };
  return found.seq;
}

/** Make the page's profile read these audiences; everything else is the server's own answer. */
async function readAs(page: Page, audiences: string[]) {
  await page.route("**/api/me/profile", async (route) => {
    const profile = await (await route.fetch()).json() as object;
    // This server has no accounts: its one synthetic user filed the rows, so it is the trusted one.
    await route.fulfill({ json: { ...profile, read_audiences: audiences, summary_accounts: ["no-auth@localhost"] } });
  });
}

async function open(page: Page, seq: number) {
  await page.goto(`/app/#/ref/Issue/${seq}`);
  await (await revealBar(page)).getByRole("button", { name: "Workbench", exact: true }).click();
  const option = page.locator(".workbench-menu").getByRole("checkbox", { name: /Campaign summary/ });
  // Showing a visual is a write to the station; the box checks when the server's canvas comes back.
  await option.click();
  await expect(option).toBeChecked();
}

for (const theme of ["dark", "light"] as const) {
  test(`a campaign's summary reads in the ${theme} theme, as a reader without the transcript`, async ({ page, request }, testInfo) => {
    await page.addInitScript((choice) => {
      try {
        localStorage.setItem("trackinizer.theme", choice);
      } catch {
        // about:blank has no storage.
      }
    }, theme);
    await page.emulateMedia({ reducedMotion: "reduce" });
    const seq = await seed(request);
    await readAs(page, ["open"]);

    await open(page, seq);

    const table = page.getByRole("table");
    await expect(table.getByRole("button", { name: "Fix the loader" })).toBeVisible();
    await expect(table.getByRole("button", { name: /Check the cache/ })).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath(`sessions-${theme}.png`) });

    await table.getByRole("button", { name: "Fix the loader" }).click();
    const article = page.getByRole("article", { name: "Fix the loader" });
    await expect(article.getByRole("region", { name: "Steps" })).toContainText("Read the loader");
    await expect(article.getByRole("region", { name: "Raw transcript" })).toHaveCount(0);
    await expect(article.getByRole("button", { name: "#12" })).toHaveCount(0);
    await expect(article.getByText("#12")).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath(`session-${theme}.png`) });

    await page.getByRole("tab", { name: "Corpus" }).click();
    await expect(page.getByRole("region", { name: "Efforts" })).toContainText("Repair the cache");
    await page.getByRole("region", { name: "Beliefs" }).locator("summary").first().click();
    await page.screenshot({ path: testInfo.outputPath(`corpus-${theme}.png`) });

    await expect(page.locator("html")).toHaveCSS("color-scheme", theme);
    const { violations } = await new AxeBuilder({ page }).include(".sum").withTags(WCAG).analyze();
    const found = violations.flatMap((violation) =>
      violation.nodes.map((node) => `${violation.id} at ${node.target.join(" ")}: ${node.failureSummary?.replace(/\s+/g, " ")}`));
    expect(found, "axe violations on the summary").toEqual([]);
  });
}

test("a staff reader sees the transcript beside the steps, and a span points it at its records", async ({ page, request }) => {
  const seq = await seed(request);
  await readAs(page, ["open", "staff"]);
  const records = (after: number) => Array.from({ length: 40 }, (_, i) => ({
    idx: after + 1 + i, kind: i % 2 ? "AssistantMessage" : "UserMessage", payload: {}, text: `record ${after + 1 + i}`,
    context_id: null, timestamp: null, model: null, ciphertext: null,
  }));
  await page.route("**/api/sessions/*/parts", (route) => route.fulfill({ json: { parts: [{ part: 0, name: "a.jsonl", format: "claude", records: 100, metadata: {} }] } }));
  await page.route("**/api/sessions/*/records**", (route) => route.fulfill({
    json: { part: 0, records: records(Number(new URL(route.request().url()).searchParams.get("after_idx"))) },
  }));

  await open(page, seq);
  const table = page.getByRole("table");
  await expect(table.getByRole("button", { name: "Check the cache" })).toBeVisible();
  await table.getByRole("button", { name: "Fix the loader" }).click();
  const raw = page.getByRole("region", { name: "Raw transcript" });
  await expect(raw).toContainText("record 0");

  // The e2e rows carry no anchors, so the items are text here too; the unit tests open them.
  await expect(page.getByRole("article").getByText("#12")).toBeVisible();
});
