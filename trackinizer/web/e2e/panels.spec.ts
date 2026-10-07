import AxeBuilder from "@axe-core/playwright";
import type { APIRequestContext, Page } from "@playwright/test";
import { expect, test, streamOpened } from "./fixtures";
import { post } from "./listViews";

// The panels that collapse (src/ui/panel.tsx): the app's sidebar, Peek, the
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
  await page.goto(`/app/${hash}`);
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

test("the app's sidebar: its button collapses it to a rail of its entries' icons and the graph takes the width; a reload keeps it so, and ⌘B or Ctrl+B expands it", async ({ page }) => {
  await open(page, "#/graph");
  await expect(page.locator(".graph-count")).toHaveText(/\d nodes?$/);
  const sidebar = page.getByRole("navigation", { name: "Sidebar" });
  const entries = sidebar.locator("a.nav-item");
  const names = (await entries.allTextContents()).map((name) => name.trim());
  const wide = (await sidebar.boundingBox())!.width;
  const canvas = page.locator(".graph-canvas");
  const narrow = (await canvas.boundingBox())!.width;
  await sidebar.getByRole("button", { name: "Collapse sidebar" }).click();
  const expand = sidebar.getByRole("button", { name: "Expand sidebar" });
  await expect(expand).toBeFocused();
  const rail = (await sidebar.boundingBox())!;
  expect(rail.width).toBeGreaterThanOrEqual(48);
  expect(rail.width).toBeLessThanOrEqual(56);
  // The graph takes the width the rail gave, and draws in all of it.
  expect((await canvas.boundingBox())!.width).toBeCloseTo(narrow + wide - rail.width, 0);
  await expect.poll(async () => (await canvas.locator("canvas").first().boundingBox())!.width).toBeCloseTo((await canvas.boundingBox())!.width, 0);
  // Every entry in its order: its icon alone, inside the rail, named and titled as its label was.
  await expect(entries).toHaveCount(names.length);
  for (const [n, name] of names.entries()) {
    const entry = entries.nth(n);
    await expect(entry).toHaveAccessibleName(name);
    await expect(entry).toHaveAttribute("title", name);
    await expect(entry.locator(".ic")).toBeVisible();
    await expect(entry.locator(".label")).toBeHidden();
    const box = (await entry.boundingBox())!;
    expect(box.x + box.width, name).toBeLessThanOrEqual(rail.x + rail.width);
  }
  await expect(sidebar.getByRole("link", { name: "Graph" })).toHaveAttribute("aria-current", "page");
  for (const heading of await sidebar.locator(".nav-section-h").all()) await expect(heading).toBeHidden();
  // The signed-in user as their avatar, the email in its tooltip.
  const me = sidebar.getByRole("group", { name: "Signed in" });
  await expect(me.locator(".avatar")).toBeVisible();
  await expect(me).toHaveAttribute("title", /@/);
  expect((await me.boundingBox())!.width).toBeLessThanOrEqual(rail.width);
  // The keyboard walks the rail top to bottom, each stop's focus showing.
  const stops = [sidebar.getByRole("button", { name: "Search" }), sidebar.getByRole("button", { name: /^New / }), ...names.map((_, n) => entries.nth(n))];
  for (const stop of stops) {
    await page.keyboard.press("Tab");
    await expect(stop).toBeFocused();
    await expect(stop).toHaveCSS("outline-style", "solid");
  }
  await page.reload();
  await expect(entries.first()).toHaveAttribute("title", names[0]!);
  expect((await sidebar.boundingBox())!.width).toBe(rail.width);
  await page.keyboard.press("ControlOrMeta+b");
  await expect(sidebar.getByRole("link", { name: "Graph" }).locator(".label")).toBeVisible();
  expect((await sidebar.boundingBox())!.width).toBe(wide);
});

test("on a phone, a collapsed sidebar is still the drawer, whole, with no collapse button", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.addInitScript(() => sessionStorage.setItem("trackinizer.v2.panel.app.sidebar", "true"));
  await open(page, "#/graph");
  const sidebar = page.getByRole("navigation", { name: "Sidebar" });
  await expect(sidebar).toBeHidden();
  // The stream answers before the app has drawn, so wait for the page itself.
  await expect(page.locator(".main")).toBeVisible();
  expect((await page.locator(".main").boundingBox())!.width).toBe(390);
  await page.getByRole("button", { name: "Open navigation" }).first().click();
  await expect(sidebar.getByRole("link", { name: "Console" }).locator(".label")).toBeVisible();
  await expect(sidebar.getByRole("group", { name: "Signed in" }).locator(".me-email")).toBeVisible();
  expect((await sidebar.boundingBox())!.width).toBeGreaterThan(200);
  await expect(sidebar.getByRole("button", { name: /sidebar$/ })).toHaveCount(0);
});

for (const theme of ["dark", "light"] as const) {
  test(`axe finds nothing wrong with collapsed panels, ${theme} theme`, async ({ page, request }) => {
    await page.addInitScript((choice) => {
      try {
        localStorage.setItem("trackinizer.theme", choice);
      } catch {
        // about:blank has no storage.
      }
    }, theme);
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
    await page.goto(`/app/#/ref/Issue/${root}`);
    await page.getByRole("button", { name: "Collapse parents, children and properties" }).click();
    await expect(page.getByRole("region", { name: "Children" })).toHaveCount(0);
    await audit(page, theme, "detail, rail collapsed");
    await page.goto("/app/#/console");
    await page.getByRole("button", { name: "Collapse views and filters" }).click();
    await expect(page.getByRole("complementary", { name: "Views and filters" })).toBeHidden();
    await page.getByRole("button", { name: "Collapse sidebar" }).click();
    await expect(page.getByRole("navigation", { name: "Sidebar" }).getByRole("link", { name: "Console" })).toHaveAttribute("title", "Console");
    await audit(page, theme, "console, its rail and the sidebar collapsed");
  });
}

async function audit(page: Page, theme: "dark" | "light", view: string) {
  await expect(page.locator("html"), "the theme applied").toHaveCSS("color-scheme", theme);
  const { violations } = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
  const found = violations.flatMap((violation) => violation.nodes.map((node) => `${violation.id} at ${node.target.join(" ")}: ${node.failureSummary}`));
  expect.soft(found, `axe violations on ${view}, ${theme} theme`).toEqual([]);
}
