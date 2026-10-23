import AxeBuilder from "@axe-core/playwright";
import type { APIRequestContext, Page } from "@playwright/test";
import { expect, revealBar, setPreferences, streamOpened, test } from "./fixtures";
import { post } from "./listViews";

// The panels that collapse (src/ui/panel.tsx): Peek, the
// graph's search results, the detail's rail and the console's rail. Each
// collapses by its button and comes back by its key, is kept collapsed for the
// tab across a reload, and gives its view the room; axe finds nothing wrong
// with a collapsed one in either theme. Each test's titles carry a tag of its
// own, since specs share one server.

/** A root Issue and a child that narrows it, titled with a fresh tag; their seqs and the tag. */
async function seed(request: APIRequestContext): Promise<{ root: number; child: number; tag: string }> {
  const tag = `panels-${crypto.randomUUID().slice(0, 8)}`;
  const items = [`Panels root ${tag}`, `Panels child ${tag}`].map((title) => ({ kind: "Issue", title, idempotency_key: crypto.randomUUID() }));
  const ids = (await post(request, "/api/inquiries/batch", { items, edges: [{ from_index: 1, to_index: 0, edge_kind: "narrows" }] })).ids as string[];
  const [root, child] = await Promise.all(ids.map(async (id) => (await (await request.get(`/api/inquiries/${id}`)).json()).seq as number));
  return { root: root!, child: child!, tag };
}

/** Open `hash` once the live stream is connected. */
async function open(page: Page, hash: string) {
  const subscribed = streamOpened(page);
  await page.goto(`/trax/${hash}`);
  await subscribed;
}

test("the graph: Peek collapses to a strip that keeps its ref and the selection, a reload keeps it so, and ] expands it", async ({ page, request }) => {
  const { child, tag } = await seed(request);
  await open(page, `#/graph?focus=Issue/${child}&hops=1`);
  const peek = page.getByRole("complementary", { name: "Peek" });
  await expect(peek.locator(".d-title")).toHaveText(`Panels child ${tag}`);
  await peek.getByRole("button", { name: "Collapse Peek" }).click();
  await expect(peek).toHaveClass(/panel-strip/);
  await expect(peek).toHaveText(`Issue#${child}`);
  await expect(peek.getByRole("button", { name: "Expand Peek" })).toBeFocused();
  expect((await peek.boundingBox())!.width).toBeLessThanOrEqual(41);
  const selected = () => page.evaluate(() => (window as unknown as { trackinizer: { graph(): { selected: string | null } } }).trackinizer.graph().selected);
  expect(await selected()).not.toBeNull();
  await page.reload();
  await expect(peek).toHaveText(`Issue#${child}`);
  await page.keyboard.press("]");
  await expect(peek.locator(".d-title")).toHaveText(`Panels child ${tag}`);
});

test("the graph: the search results collapse to a strip with their count, and [ brings them back once the box is left", async ({ page, request }) => {
  const { tag } = await seed(request);
  await open(page, "#/graph");
  await expect(page.locator(".graph-count")).toHaveText(/\d nodes?$/);
  const box = page.getByRole("combobox", { name: "Search the graph" });
  await box.fill(tag);
  const results = page.getByRole("complementary", { name: "Search results" });
  await expect(results.getByRole("option")).toHaveCount(2);
  await results.getByRole("button", { name: "Collapse search results" }).click();
  await expect(results).toHaveText("2 matches");
  await expect(box).toHaveAttribute("aria-expanded", "false");
  await box.press("Enter");
  await expect(page.getByRole("complementary", { name: "Peek" }).locator(".d-title")).toHaveText(new RegExp(tag));
  await page.locator(".graph-canvas").click({ position: { x: 5, y: 5 } });
  await page.keyboard.press("[");
  await expect(results.getByRole("option")).toHaveCount(2);
});

test("a list: ] collapses Peek to a strip, and Space expands it", async ({ page, request }) => {
  await seed(request);
  await open(page, "#/list/Issue");
  await expect(page.locator("[data-row]").first()).toBeVisible();
  await page.keyboard.press(" ");
  const peek = page.getByRole("complementary", { name: "Peek" });
  await expect(peek.locator(".d-title")).toBeVisible();
  await page.keyboard.press("]");
  await expect(peek).toHaveClass(/panel-strip/);
  await page.keyboard.press(" ");
  await expect(peek.locator(".d-title")).toBeVisible();
});

test("a detail: the rail's button collapses the rail, the text takes the width, and ] brings it back", async ({ page, request }) => {
  const { root, tag } = await seed(request);
  await open(page, `#/ref/Issue/${root}`);
  const children = page.getByRole("region", { name: "Children" });
  await expect(children.getByRole("link", { name: new RegExp(`Panels child ${tag}`) })).toBeVisible();
  const wide = (await page.locator(".d-top").boundingBox())!.width;
  await page.getByRole("button", { name: "Collapse parents, children and properties" }).click();
  await expect(children).toHaveCount(0);
  expect((await page.locator(".d-top").boundingBox())!.width).toBeGreaterThan(wide + 300);
  await page.keyboard.press("]");
  await expect(children).toBeVisible();
});

test("the console: the rail's button collapses the rail and the feed takes its width; [ brings it back", async ({ page }) => {
  await open(page, "#/console");
  const rail = page.getByRole("complementary", { name: "Views and filters" });
  await expect(rail).toBeVisible();
  const narrow = (await page.locator(".console-stream").boundingBox())!.width;
  await page.getByRole("button", { name: "Collapse views and filters" }).click();
  await expect(rail).toBeHidden();
  expect((await page.locator(".console-stream").boundingBox())!.width).toBeGreaterThan(narrow + 240);
  await page.keyboard.press("[");
  await expect(rail).toBeVisible();
});

for (const theme of ["dark", "light"] as const) {
  test(`axe finds nothing wrong with collapsed panels, ${theme} theme`, async ({ page, request }) => {
    await setPreferences(page.request, { theme });
    await page.emulateMedia({ reducedMotion: "reduce" });
    const { root, child, tag } = await seed(request);
    await open(page, `#/graph?focus=Issue/${child}&hops=1`);
    await expect(page.getByRole("complementary", { name: "Peek" }).locator(".d-title")).toHaveText(`Panels child ${tag}`);
    await page.keyboard.press("]");
    await page.getByRole("combobox", { name: "Search the graph" }).fill(tag);
    await page.getByRole("button", { name: "Collapse search results" }).click();
    await expect(page.getByRole("complementary", { name: "Search results" })).toHaveText("2 matches");
    await audit(page, theme, "graph, Peek and results collapsed");
    // The same document under another hash: the stream stays open, so nothing to wait for but the view.
    await page.goto(`/trax/#/ref/Issue/${root}`);
    await page.getByRole("button", { name: "Collapse parents, children and properties" }).click();
    await expect(page.getByRole("region", { name: "Children" })).toHaveCount(0);
    await audit(page, theme, "detail, rail collapsed");
    await page.goto("/trax/#/console");
    await page.getByRole("button", { name: "Collapse views and filters" }).click();
    await expect(page.getByRole("complementary", { name: "Views and filters" })).toBeHidden();
    await audit(page, theme, "console, its rail collapsed", { feed: false });
    // The top bar's account menu, open over the page: its skins, accounts and Sign out.
    await (await revealBar(page)).getByRole("button", { name: /^Account: / }).click();
    await expect(page.getByRole("dialog", { name: "Account" })).toBeVisible();
    await audit(page, theme, "console, the account menu open", { feed: false });
  });
}

/**
 * `feed: false` leaves the console's feed out: it draws every session other specs left on this server, which makes
 * axe's pass take longer than the test has, and its lines are audited by a11y.spec.ts's console test, with its own.
 */
async function audit(page: Page, theme: "dark" | "light", view: string, { feed = true }: { feed?: boolean } = {}) {
  await expect(page.locator("html"), "the theme applied").toHaveCSS("color-scheme", theme);
  const axe = new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]);
  const { violations } = await (feed ? axe : axe.exclude(".console-feed")).analyze();
  const found = violations.flatMap((violation) => violation.nodes.map((node) => `${violation.id} at ${node.target.join(" ")}: ${node.failureSummary}`));
  expect.soft(found, `axe violations on ${view}, ${theme} theme`).toEqual([]);
}
