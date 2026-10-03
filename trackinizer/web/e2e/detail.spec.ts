import { readFileSync } from "node:fs";
import type { APIRequestContext, Page } from "@playwright/test";
import { expect, failedResource, test } from "./fixtures";
import { DETAIL_CHILDREN as CHILDREN, readHubs } from "./hubs";

// One hub with 61 children, one of which also narrows a second parent, and a
// Belief with a proving Paper, made once by hubs.setup.ts. Seqs depend on what
// other specs created, so pages are opened by id or by a seq read back.
let ids: string[] = [];

test.beforeAll(({}, info) => {
  ids = readHubs(info).detail;
});

const heading = (page: Page) => page.getByRole("heading", { level: 1 });
const group = (page: Page, name: string) => page.getByRole("group", { name, exact: true });
/** The rail's section `name`, "Parents" or "Children". */
const section = (page: Page, name: string) => page.getByRole("region", { name, exact: true });

/**
 * Make `items` (each a kind, a title and any other fields) joined by `edges`
 * (by index), in one batch, so they are among the newest the graph draws.
 */
async function create(
  request: APIRequestContext,
  items: { kind: string; title: string; [field: string]: unknown }[],
  edges: { from_index: number; to_index: number; edge_kind: string }[] = [],
): Promise<string[]> {
  const response = await request.post("/api/inquiries/batch", {
    data: { items: items.map((item) => ({ ...item, idempotency_key: crypto.randomUUID() })), edges },
  });
  expect(response.ok(), await response.text()).toBe(true);
  return (await response.json()).ids;
}

test("a hub's detail renders every child, its Markdown and its activity", async ({ page, request }) => {
  const { self } = await (await request.get(`/api/web/get/${ids[0]}`)).json();
  await page.goto(`/app/#/ref/Issue/${self.seq}`);
  await expect(heading(page)).toHaveText("Detail hub");
  const children = section(page, "Children");
  await children.getByRole("button", { name: `Show all ${CHILDREN}` }).click();
  await expect(children.locator(".rail-peer")).toHaveCount(CHILDREN);
  await expect(children.locator(".rail-edges").first().getByRole("img", { name: "P0 Critical" })).toBeVisible();

  const description = page.locator('[data-field="description"]');
  await expect(description.getByRole("link", { name: "Belief#1" })).toHaveAttribute("href", "#/ref/Belief/1");
  await expect(description.getByRole("link", { name: "the plan" })).toHaveAttribute("href", "https://example.com/plan");
  await expect(description.locator("code")).toHaveText("Issue#1");

  const properties = page.getByRole("complementary", { name: "Properties" });
  await expect(properties.locator('[data-field="priority"] dd')).toContainText("P1 High");
  await expect(properties.locator('[data-field="issue_kind"] dd')).toHaveText("No type");
  await expect(page.locator(".timeline")).toContainText(/librarian \d+ upstream changes/);
});

test("a lookup link opens by id, with evidence confidence beside the author's", async ({ page }) => {
  await page.goto(`/app/#/lookup/${ids[CHILDREN + 2]}`);
  await expect(heading(page)).toHaveText("Detail belief");
  const properties = page.getByRole("complementary", { name: "Properties" });
  await expect(properties.locator('[data-field="confidence"] dd')).toHaveText("0.90");
  await expect(properties.locator('[data-field="evidence_confidence"] dd')).toHaveText(/^0\.\d\d$/);
  await expect(group(page, "Proved by").getByRole("link")).toContainText("Detail paper");
});

test("a child names both of its parents, and a relation opens its inquiry", async ({ page }) => {
  await page.goto(`/app/#/lookup/${ids[1]}`);
  await expect(heading(page)).toHaveText("Hub child 0");
  const parents = section(page, "Parents");
  await expect(parents.locator(".rail-peer")).toHaveCount(2);

  await parents.locator(".rail-link", { hasText: "Second parent" }).click();
  await expect(heading(page)).toHaveText("Second parent");
  await page.goBack();
  await expect(heading(page)).toHaveText("Hub child 0");
  await page.keyboard.press("Escape");
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Issues");
});

test("Show in graph opens the graph focused on the inquiry, two hops out", async ({ page, request }) => {
  const [parent, id] = await create(
    request,
    [
      { kind: "Issue", title: "Graph parent" },
      { kind: "Issue", title: "Shown in the graph" },
    ],
    [{ from_index: 1, to_index: 0, edge_kind: "narrows" }],
  );
  const { self } = await (await request.get(`/api/web/get/${id}`)).json();
  await page.goto(`/app/#/lookup/${id}`);
  await expect(heading(page)).toHaveText("Shown in the graph");
  await page.getByRole("link", { name: "Show in graph" }).click();
  await expect.poll(() => new URL(page.url()).hash).toBe(`#/graph?focus=Issue/${self.seq}&hops=2`);
  await expect(page.locator(".graph-count")).toHaveText(/\d nodes?$/);
  // The canvas cannot be read; `trackinizer.graph()` reports what it draws, selects and focuses on.
  type Drawn = { focus: string | null; selected: string | null; nodes: { id: string }[] };
  const drawn = () => page.evaluate(() => (window as unknown as { trackinizer: { graph(): Drawn } }).trackinizer.graph());
  await expect.poll(async () => (await drawn()).focus).toBe(id);
  expect((await drawn()).nodes.map((node) => node.id)).toContain(parent);
  await expect(page.getByRole("group", { name: "Hops" }).getByRole("button", { name: /^2 hops/ })).toHaveAttribute("aria-pressed", "true");
  // Opened on the focus, the graph selects it too, in Peek.
  expect((await drawn()).selected).toBe(id);
  await expect(page.getByRole("complementary", { name: "Peek" })).toContainText(`Issue#${self.seq}`);
});

test("the rail's graph preview draws two hops round the inquiry; a click opens the graph there, the inquiry selected in Peek", async ({
  page,
  request,
}) => {
  // A grandparent, a parent, the inquiry and its child: the grandparent is two hops out.
  const [, , id] = await create(
    request,
    [
      { kind: "Issue", title: "Preview grandparent" },
      { kind: "Issue", title: "Preview parent" },
      { kind: "Issue", title: "Previewed" },
      { kind: "Issue", title: "Preview child" },
    ],
    [
      { from_index: 1, to_index: 0, edge_kind: "narrows" },
      { from_index: 2, to_index: 1, edge_kind: "narrows" },
      { from_index: 3, to_index: 2, edge_kind: "narrows" },
    ],
  );
  const { self } = await (await request.get(`/api/web/get/${id}`)).json();
  await page.goto(`/app/#/lookup/${id}`);
  await expect(heading(page)).toHaveText("Previewed");
  const preview = page.getByRole("link", { name: `Open in graph: Issue#${self.seq}, 2 hops` });
  await expect(preview.locator("[data-id]")).toHaveCount(4);
  await expect(preview.locator(".rail-graph-caption")).toHaveText("4 nodes within 2 hops");
  await preview.click();
  await expect.poll(() => new URL(page.url()).hash).toBe(`#/graph?focus=Issue/${self.seq}&hops=2`);
  await expect(page.locator(".graph-count")).toHaveText(/\d nodes?$/);
  type Drawn = { focus: string | null; selected: string | null };
  const drawn = () => page.evaluate(() => (window as unknown as { trackinizer: { graph(): Drawn } }).trackinizer.graph());
  await expect.poll(async () => (await drawn()).selected).toBe(id);
  expect((await drawn()).focus).toBe(id);
  const peek = page.getByRole("complementary", { name: "Peek" });
  await expect(peek.getByRole("heading", { level: 1 })).toHaveText("Previewed");
  // In the graph's Peek the preview would lead where it is: it is left out.
  await expect(peek.getByRole("region", { name: "Parents" })).toBeVisible();
  await expect(peek.getByRole("link", { name: /^Open in graph/ })).toHaveCount(0);
});

test("narrow, the rail stacks under the title and text, above other relations", async ({ page, request }) => {
  const [, belief] = await create(
    request,
    [
      { kind: "Issue", title: "Narrow parent" },
      { kind: "Belief", title: "Narrow belief", description: "What the belief says, in a line." },
      { kind: "Paper", title: "Narrow paper" },
    ],
    [
      { from_index: 1, to_index: 0, edge_kind: "produced_by" },
      { from_index: 2, to_index: 1, edge_kind: "proves" },
    ],
  );
  await page.setViewportSize({ width: 700, height: 900 });
  await page.goto(`/app/#/lookup/${belief}`);
  await expect(heading(page)).toHaveText("Narrow belief");
  const parents = section(page, "Parents");
  await expect(parents.locator(".rail-peer")).toHaveCount(1);
  const [title, description, rail, other] = await Promise.all([
    heading(page).boundingBox(),
    page.locator('[data-field="description"]').boundingBox(),
    parents.getByRole("heading", { name: "Parents" }).boundingBox(),
    page.getByRole("region", { name: /^Other relations/ }).boundingBox(),
  ]);
  expect(title!.y + title!.height).toBeLessThanOrEqual(description!.y);
  expect(description!.y + description!.height).toBeLessThanOrEqual(rail!.y);
  // Wide, the rail stands beside the text, its Parents level with the title; narrow, it stacks under the text.
  expect(rail!.y + rail!.height).toBeLessThanOrEqual(other!.y);
});

test("a description that is a job's JSON result shows as a JSON view, its metrics a table; code takes its colours", async ({
  page,
  request,
  allowErrors,
}) => {
  const result = readFileSync(new URL("../src/markdown/testdata/job_result.json", import.meta.url), "utf8");
  const [artifact, issue] = await create(request, [
    { kind: "Artifact", title: "A job's result", description: result },
    { kind: "Issue", title: "A rerun", description: "Rerun it:\n\n```python\ndef rerun(seed: int) -> None:\n    return None\n```" },
  ]);
  // An Artifact's detail asks for its published content, and this one has none:
  // the 404 shows nothing on the page, but the browser logs it.
  allowErrors(failedResource(`/api/artifacts/${artifact}/content`, 404));
  await page.goto(`/app/#/lookup/${artifact}`);
  await expect(heading(page)).toHaveText("A job's result");
  const json = page.locator('[data-field="description"]').getByRole("group", { name: "JSON" });
  await expect(json.getByText('"MEASURED"')).toBeVisible();
  // The dark theme's --code-key.
  await expect(json.locator(".jv-key").first()).toHaveCSS("color", "rgb(121, 192, 255)");
  const table = json.getByRole("table");
  await expect(table.getByRole("row", { name: "train_sec 600.0" })).toBeVisible();
  await expect(table.getByRole("cell", { name: "600.0" })).toHaveCSS("text-align", "right");
  await json.getByRole("button", { name: "payload" }).click();
  await expect(table).toBeHidden();

  // The highlighter's chunk and its stylesheet load with the first code block.
  await page.goto(`/app/#/lookup/${issue}`);
  await expect(heading(page)).toHaveText("A rerun");
  await expect(page.locator('[data-field="description"] code .hljs-keyword').first()).toHaveCSS("color", "rgb(255, 123, 114)");
});
