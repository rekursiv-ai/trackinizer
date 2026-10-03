import { readFileSync } from "node:fs";
import AxeBuilder from "@axe-core/playwright";
import type { APIRequestContext, Page } from "@playwright/test";
import { expect, failedResource, test } from "./fixtures";
import { openView, post } from "./listViews";

// axe's WCAG 2.0 and 2.1 A and AA rules on each main view, in both themes. Each
// test seeds what its view shows, titled with a tag made here, and fails on any
// violation, naming its rule, the element and what is wrong. The tag is digits:
// a hex one can hold a word another spec searches the shared server for ("c4").

for (const theme of ["dark", "light"] as const) {
  test.describe(`${theme} theme`, () => {
    const tag = String(crypto.getRandomValues(new Uint32Array(1))[0]);
    // axe checks the colours only of what a scroll box shows, so the window is
    // tall enough to show each view's whole content: the console's feed, a
    // transcript.
    test.use({ viewport: { width: 1280, height: 3000 } });
    test.beforeEach(async ({ page }) => {
      // The pre-paint snippet in index.html and login.html reads this key.
      await page.addInitScript((choice) => {
        try {
          localStorage.setItem("trackinizer.theme", choice);
        } catch {
          // about:blank has no storage.
        }
      }, theme);
      // Reduced motion ends every transition at once, so a colour axe reads is
      // the one the element settles on, not a frame of a fade.
      await page.emulateMedia({ reducedMotion: "reduce" });
    });

    test("the graph, grouped by root, then with Peek open", async ({ page, request }) => {
      await batch(
        request,
        [
          { kind: "Issue", title: `A11y graph root ${tag}` },
          { kind: "Issue", title: `A11y graph child ${tag}` },
          { kind: "Paper", title: `A11y graph paper ${tag}` },
          { kind: "Belief", title: `A11y graph belief ${tag}` },
        ],
        [
          { from_index: 1, to_index: 0, edge_kind: "narrows" },
          { from_index: 2, to_index: 3, edge_kind: "proves" },
        ],
      );
      const subscribed = page.waitForResponse((response) => response.url().includes("/api/web/subscribe"));
      await page.goto("/app/#/graph");
      await subscribed;
      await expect(page.locator(".graph-count")).toHaveText(/\d nodes?$/);
      const root = page.getByRole("listbox", { name: "Roots" }).getByRole("option", { name: new RegExp(`A11y graph root ${tag}`) });
      await expect(root).toBeVisible();
      await audit(page, theme, "graph, grouped");

      await root.click();
      await expect(page.getByRole("complementary", { name: "Peek" }).locator(".d-title")).toHaveText(`A11y graph root ${tag}`);
      await audit(page, theme, "graph, grouped, with Peek");
    });

    test("the console", async ({ page, request }) => {
      const actors = agentsOnEveryColour(tag);
      await Promise.all(actors.map((actor) => session(request, `A11y console ${tag}`, { actor, rooms: [`a11y-${tag}`] })));
      const subscribed = page.waitForResponse((response) => response.url().includes("/api/web/subscribe"));
      await page.goto("/app/#/console");
      await subscribed;
      for (const actor of actors) await expect(page.locator(".console-actor", { hasText: actor }).first()).toBeVisible();
      const colours = await page
        .locator(".console-actor")
        .evaluateAll((all, names) => new Set(all.filter((name) => names.includes(name.textContent!)).map((name) => name.className)).size, actors);
      expect(colours, "the names cover every colour").toBe(8);
      await audit(page, theme, "console");
    });

    test("an Issue list in List, Streams, Outline and Columns", async ({ page, request }) => {
      const label = `a11y-${tag}`;
      const issue = (title: string, extra: object = {}) => ({ kind: "Issue", title: `${title} ${label}`, labels: [label], ...extra });
      await batch(
        request,
        [
          issue("Goal", { priority: 0 }),
          issue("Step one", { status: "complete" }),
          issue("Step two", { priority: 10, owner: "a11y-agent" }),
          issue("Loose"),
        ],
        [
          { from_index: 1, to_index: 0, edge_kind: "narrows" },
          { from_index: 2, to_index: 0, edge_kind: "narrows" },
        ],
      );
      await openView(page, label, "List");
      const views = page.getByRole("group", { name: "View" });
      for (const view of ["List", "Streams", "Outline"]) {
        await views.getByRole("button", { name: view }).click();
        await expect(views.getByRole("button", { name: view })).toHaveAttribute("aria-pressed", "true");
        await expect(page.getByText(`Loose ${label}`)).toBeVisible();
        await audit(page, theme, `Issue list, ${view}`);
      }
      await views.getByRole("button", { name: "Columns" }).click();
      await page.locator(".c-item", { hasText: `Goal ${label}` }).click();
      await expect(page.locator(".c-item", { hasText: `Step two ${label}` })).toBeVisible();
      await audit(page, theme, "Issue list, Columns");
    });

    test("an Issue's detail with relations, the graph preview and a JSON description", async ({ page, request }) => {
      const result = readFileSync(new URL("../src/markdown/testdata/job_result.json", import.meta.url), "utf8");
      const [id] = await batch(
        request,
        [
          { kind: "Issue", title: `A11y detail ${tag}`, description: result, priority: 10 },
          { kind: "Issue", title: `A11y detail parent ${tag}` },
          { kind: "Issue", title: `A11y detail child ${tag}` },
          { kind: "Issue", title: `A11y detail blocker ${tag}` },
          { kind: "Artifact", title: `A11y detail artifact ${tag}` },
        ],
        [
          { from_index: 0, to_index: 1, edge_kind: "narrows" },
          { from_index: 2, to_index: 0, edge_kind: "narrows" },
          { from_index: 0, to_index: 3, edge_kind: "requires" },
          { from_index: 4, to_index: 0, edge_kind: "produced_by" },
        ],
      );
      await page.goto(`/app/#/lookup/${id}`);
      await expect(page.getByRole("heading", { level: 1 })).toHaveText(`A11y detail ${tag}`);
      await expect(page.locator('[data-field="description"]').getByRole("group", { name: "JSON" }).getByRole("table")).toBeVisible();
      await expect(page.getByRole("link", { name: /^Open in graph:/ }).locator(".rail-graph-caption")).toHaveText(/nodes within 2 hops$/);
      await audit(page, theme, "Issue detail");
    });

    test("an AgentSession's detail with its transcript", async ({ page, request }) => {
      const seq = await seqOf(request, await session(request, `A11y transcript ${tag}`));
      await page.goto(`/app/#/ref/AgentSession/${seq}`);
      const transcript = page.getByRole("region", { name: /^Transcript/ });
      await expect(transcript.getByRole("heading", { level: 2 })).toHaveText(`Transcript ${RECORDS.length} records`);
      // The raw record and the bookkeeping shown too, each drawn in its own colours.
      await transcript.locator(".turn").first().getByRole("button", { name: "Raw" }).click();
      await expect(transcript.locator(".turn-json")).toBeVisible();
      await transcript.getByRole("button", { name: /^Show \d+ bookkeeping records?$/ }).click();
      // The run of tool steps folds into one line; opened, with every step and
      // the reasoning open too, axe reads each one's colours: the highlighted
      // code, the diff's tinted rows, ANSI colours.
      await transcript.locator(".tr-group > .tr-step > details > summary").click();
      await expect(transcript.locator(".tr-group .turn-tool")).toHaveCount(4);
      const closed = transcript.locator(".turn > .tr-step > details:not([open]) > summary");
      while (await closed.count()) await closed.first().click();
      const steps = transcript.locator(".turn-tool > .tr-step > details[open]");
      await expect(steps).toHaveCount(4);
      await expect(transcript.getByRole("button", { name: "Show 20 more lines" })).toBeVisible();
      await expect(steps.locator(".tr-diff .d-hunk")).toBeVisible();
      await expect(steps.locator(".tr-diff [class^=hljs-]").first()).toBeVisible();
      await expect(steps.locator(".a-green")).toHaveText("1 passed");
      await expect(steps.locator(".tr-search").getByRole("link", { name: "Contrast" })).toBeVisible();
      await audit(page, theme, "AgentSession transcript");
    });

    test("an Experiment's detail with its metrics", async ({ page, request }) => {
      const [id] = await batch(request, [{ kind: "Experiment", title: `A11y metrics ${tag}` }]);
      const points = Array.from({ length: 20 }, (_, step) => ({ key: "loss", step, value: 2 - step / 20 }));
      expect((await request.post(`/api/experiments/${id}/metrics`, { data: { points } })).ok()).toBe(true);
      await page.goto(`/app/#/lookup/${id}`);
      await expect(page.getByRole("region", { name: /^Metrics/ }).locator('[data-metric="loss"] .r')).toBeVisible();
      await audit(page, theme, "Experiment metrics");
    });

    test("Activity", async ({ page, request }) => {
      await batch(request, [{ kind: "Issue", title: `A11y activity ${tag}` }]);
      await page.goto("/app/#/activity");
      await expect(page.getByRole("heading", { level: 1 })).toHaveText("Activity");
      await expect(page.locator(".feed-row").first()).toBeVisible();
      await audit(page, theme, "Activity");
    });

    test("Settings, with a revoked token", async ({ page, request }) => {
      const name = `a11y-${tag}`;
      const made = await post(request, "/api/me/tokens", { name, role: "viewer" });
      expect((await request.post(`/api/me/tokens/${made.id}/revoke`)).ok()).toBe(true);
      await page.goto("/app/#/settings");
      await expect(page.getByRole("heading", { level: 1 })).toHaveText("Your settings");
      await expect(page.getByRole("region", { name: "API tokens" }).getByRole("row").filter({ hasText: name })).toContainText("revoked");
      await audit(page, theme, "Settings");
    });

    test("Admin", async ({ page }) => {
      await page.goto("/app/#/admin");
      await expect(page.getByRole("heading", { level: 1 })).toHaveText("Admin");
      await expect(page.getByRole("region", { name: "Users" }).getByRole("row").filter({ hasText: "no-auth@localhost" })).toBeVisible();
      await audit(page, theme, "Admin");
    });

    test("Search", async ({ page, request }) => {
      await batch(request, [
        { kind: "Issue", title: `A11y found ${tag} issue` },
        { kind: "Belief", title: `A11y found ${tag} belief`, judgement: "proven" },
        { kind: "Paper", title: `A11y found ${tag} paper` },
      ]);
      await page.goto(`/app/#/search/${encodeURIComponent(`A11y found ${tag}`)}`);
      await expect(page.getByRole("heading", { level: 1 })).toHaveText(new RegExp(`^Search: A11y found ${tag} \\(\\d+\\)$`));
      await audit(page, theme, "Search");
    });

    test("the create form", async ({ page }) => {
      await page.goto("/app/#/new/Issue");
      const form = page.getByRole("dialog", { name: "New issue" });
      await expect(form.getByRole("textbox", { name: "Title" })).toBeFocused();
      await audit(page, theme, "create form");
    });

    test("the palette, open, then with results", async ({ page, request }) => {
      await batch(request, [{ kind: "Issue", title: `A11y palette ${tag}` }]);
      await page.goto("/app/#/activity");
      await expect(page.getByRole("heading", { level: 1 })).toHaveText("Activity");
      await page.keyboard.press("ControlOrMeta+k");
      const palette = page.getByRole("dialog", { name: "Command menu" });
      await expect(palette.getByRole("combobox", { name: "Command" })).toBeFocused();
      await expect(palette.getByRole("option").first()).toBeVisible();
      await audit(page, theme, "palette");

      await page.keyboard.type(`A11y palette ${tag}`);
      await expect(palette.getByRole("group", { name: /^From the server · as of / }).getByRole("option")).toHaveText([
        new RegExp(`A11y palette ${tag}`),
      ]);
      await audit(page, theme, "palette, with results");
    });

    test("the sign-in page", async ({ page, allowErrors }) => {
      // This server has no Google sign-in, and its check answers 404 by design.
      allowErrors(failedResource("/auth/login/ready", 404));
      await page.goto("/auth/login_page?next=%2Fapp%2F");
      await expect(page.getByRole("status")).toHaveText("Sign-in is not configured on this server.");
      await audit(page, theme, "sign-in page");
    });

    test("the sign-in page, offering Google sign-in", async ({ page }) => {
      // The test answers the check itself, so the page shows its button.
      await page.route("**/auth/login/ready", (route) => route.fulfill({ status: 204 }));
      await page.goto("/auth/login_page?next=%2Fapp%2F");
      await expect(page.getByRole("link", { name: "Sign in with Google" })).toBeVisible();
      await audit(page, theme, "sign-in page, offering Google sign-in");
    });
  });
}

const WCAG = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"];

/**
 * Run axe on the page as it stands, in `theme`, and fail the test on any
 * violation, one line per element: the rule, the element and what is wrong.
 * Softly, so a test's later views still run and report theirs.
 */
async function audit(page: Page, theme: "dark" | "light", view: string) {
  await expect(page.locator("html"), "the theme applied").toHaveCSS("color-scheme", theme);
  const { violations } = await new AxeBuilder({ page }).withTags(WCAG).analyze();
  const found = violations.flatMap((violation) =>
    violation.nodes.map((node) => `${violation.id} at ${node.target.join(" ")}: ${node.failureSummary?.replace(/\s+/g, " ")}`),
  );
  expect.soft(found, `axe violations on ${view}, ${theme} theme`).toEqual([]);
}

/**
 * Eight agent names made from `tag`, one on each of the console's colours, which
 * it picks by this hash of the name. The server suffixes a name another session
 * holds ("#2"), which moves its colour, so each test names its own.
 */
function agentsOnEveryColour(tag: string): string[] {
  const colour = (name: string) => [...name].reduce((hash, char) => (hash * 31 + char.charCodeAt(0)) >>> 0, 0) % 8;
  const names: string[] = [];
  for (let n = 0; names.length < 8; n++) {
    const name = `a11y-${tag}-${n}`;
    if (!names.some((other) => colour(other) === colour(name))) names.push(name);
  }
  return names;
}

/** Make `items` joined by `edges` (by index), in one request; their ids in order. */
async function batch(
  request: APIRequestContext,
  items: { kind: string; title: string; [field: string]: unknown }[],
  edges: { from_index: number; to_index: number; edge_kind: string }[] = [],
): Promise<string[]> {
  const data = { items: items.map((item) => ({ ...item, idempotency_key: crypto.randomUUID() })), edges };
  return (await post(request, "/api/inquiries/batch", data)).ids as string[];
}

async function seqOf(request: APIRequestContext, id: string): Promise<number> {
  return (await (await request.get(`/api/web/get/${id}`)).json()).self.seq;
}

/**
 * A session's turns: a user turn, reasoning, shell calls with short (coloured),
 * long and failed output, an edit's diff of Python, web search results,
 * bookkeeping, and an answer in Markdown.
 */
const RECORDS = [
  { kind: "UserMessage", payload: { content: "Check Issue#1 and report." }, text: "Check Issue#1 and report." },
  { kind: "Thinking", payload: { content: "Read the issue, then run its test." } },
  { kind: "ToolCall", payload: { call_id: "c1", name: "Bash", arguments: { command: "pytest -x" } } },
  { kind: "ShellCommandResult", payload: { call_id: "c1", stdout: "\u001b[32m1 passed\u001b[0m in 0.12s\n", stderr: "", exit_code: 0 } },
  { kind: "ToolCall", payload: { call_id: "c2", name: "Bash", arguments: { command: "pytest" } } },
  {
    kind: "ShellCommandResult",
    payload: { call_id: "c2", stdout: Array.from({ length: 80 }, (_, n) => `line ${n}`).join("\n"), stderr: "1 failed\n", exit_code: 1 },
  },
  {
    kind: "FileEditResult",
    payload: {
      path: "a.py",
      edits: [{ before: "x = 'old'  # was\n", after: "x = 2.5  # now\n", lead: "@@ -3,3 +3,3 @@\n def f():\n", trail: " return x\n", start: 4, count: 1, bare: [] }],
    },
  },
  {
    kind: "WebSearchResults",
    payload: { query: "wcag contrast", content: [{ url: "https://example.com/wcag", title: "Contrast", snippet: "4.5:1 for text." }] },
  },
  { kind: "TokenUsage", payload: { info: { input_tokens: 3 } } },
  {
    kind: "AssistantMessage",
    payload: { content: "## Done\n\nIt passes; see [the plan](https://example.com/plan).\n\n| Check | Result |\n|---|---|\n| pytest | passed |\n\n```python\nprint(1)\n```" },
  },
];

/** Start a session titled `title` and capture `RECORDS` in it, as `trax run` does; its id. */
async function session(request: APIRequestContext, title: string, extra: object = {}): Promise<string> {
  const { id } = await post(request, "/api/sessions/start", { cli: "claude", title, ...extra });
  const name = "a11y.jsonl";
  const manifest = { name, metadata: {}, ir_id: crypto.randomUUID(), format: "claude", records: RECORDS.length };
  await post(request, `/api/sessions/${id}/records`, { name, manifest, records: RECORDS.map((record, idx) => ({ idx, ...record })) });
  return id as string;
}
