import type { APIRequestContext, Page } from "@playwright/test";
import { expect, test } from "./fixtures";

// The rail's acceptance list, on a copy of a real record's shape: an Issue that
// both narrows and was produced by one parent, with an Experiment and an
// AgentSession it produced, a long description, and no cost recorded. Titles
// carry this run's tag, since specs share one server.

const TAG = `rail-${crypto.randomUUID().slice(0, 8)}`;
const DESCRIPTION = Array.from(
  { length: 12 },
  (_, k) => `## Section ${k + 1}\n\n${"Spatial pretrained control with public-feedback adaptation. ".repeat(12)}`,
).join("\n\n");

let parent = { id: "", seq: 0 };
let self = { id: "", seq: 0 };
let experiment = { id: "", seq: 0 };
let session = { id: "", seq: 0 };

async function post(request: APIRequestContext, path: string, data: object): Promise<{ [key: string]: unknown }> {
  const response = await request.post(path, { data });
  expect(response.ok(), await response.text()).toBe(true);
  return response.json();
}

async function seqOf(request: APIRequestContext, id: string): Promise<{ id: string; seq: number }> {
  return { id, seq: (await (await request.get(`/api/inquiries/${id}`)).json()).seq };
}

test.beforeAll(async ({ request }) => {
  const items = [
    { kind: "Issue", title: `ARC3 Uber-TL pickup ${TAG}` },
    { kind: "Issue", title: `Test spatial pretrained control ${TAG}`, description: DESCRIPTION, priority: 10, issue_kind: ["task"] },
    { kind: "Experiment", title: `spc001 ${TAG}` },
  ].map((item) => ({ ...item, idempotency_key: crypto.randomUUID() }));
  const edges = [
    { from_index: 1, to_index: 0, edge_kind: "narrows" },
    { from_index: 2, to_index: 1, edge_kind: "produced_by" },
  ];
  const ids = (await post(request, "/api/inquiries/batch", { items, edges })).ids as string[];
  // A session starts through the sessions route, as `trax run` starts one.
  const started = await post(request, "/api/sessions/start", {
    cli: "claude",
    title: `Session ${TAG}`,
    cli_session_id: `sess-${TAG}`,
    idempotency_key: crypto.randomUUID(),
  });
  await post(request, `/api/edges/${started.id}/produced_by/${ids[1]}`, {});
  const closed = await request.put(`/api/inquiries/${ids[0]}/status`, { data: { value: "complete" } });
  expect(closed.ok(), await closed.text()).toBe(true);
  [parent, self, experiment, session] = await Promise.all(
    [ids[0]!, ids[1]!, ids[2]!, String(started.id)].map((id) => seqOf(request, id)),
  );
});

const section = (page: Page, name: string) => page.getByRole("region", { name, exact: true });

async function open(page: Page) {
  await page.goto(`/app/#/lookup/${self.id}`);
  await expect(page.getByRole("heading", { level: 1 })).toHaveText(`Test spatial pretrained control ${TAG}`);
}

test("the first desktop screen shows the title, the scoped cost, the parent and the outputs, and the rail stays as the text scrolls", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await open(page);
  const parents = section(page, "Parents");
  const children = section(page, "Children");
  // Each peer once, with every edge joining it, read from this Issue.
  await expect(parents.locator(".rail-link")).toHaveText([`ARC3 Uber-TL pickup ${TAG} Issue#${parent.seq}`]);
  await expect(parents.locator(".edge-name")).toHaveText(["narrows", "produced_by"]);
  await expect(children.locator(".rail-link")).toHaveText([
    `Session ${TAG} AgentSession#${session.seq}`,
    `spc001 ${TAG} Experiment#${experiment.seq}`,
  ]);
  await expect(children.locator(".edge-name")).toHaveText(["produces", "produces"]);
  await expect(parents.getByRole("img", { name: "Complete" })).toBeVisible();
  const firstScreen = [
    page.getByRole("heading", { level: 1 }),
    page.locator(".d-cost"),
    parents.locator(".rail-peer"),
    ...[0, 1].map((n) => children.locator(".rail-peer").nth(n)),
  ];
  for (const shown of firstScreen) await expect(shown).toBeInViewport({ ratio: 1 });
  await expect(page.locator(".d-cost")).toHaveText("Cost of this issue none recorded");

  await page.locator('[data-field="description"]').getByRole("heading", { name: "Section 12" }).scrollIntoViewIfNeeded();
  await expect(page.getByRole("heading", { level: 1 })).not.toBeInViewport();
  await expect(parents.locator(".rail-peer")).toBeInViewport({ ratio: 1 });
  await expect(children.locator(".rail-peer").nth(1)).toBeInViewport({ ratio: 1 });
});

test("on a phone the sections follow the title and text, and nothing scrolls sideways", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await open(page);
  const tops = await Promise.all(
    [
      page.getByRole("heading", { level: 1 }),
      page.locator('[data-field="description"]'),
      section(page, "Parents"),
      section(page, "Children"),
      page.getByRole("complementary", { name: "Properties" }),
    ].map(async (element) => (await element.boundingBox())!.y),
  );
  expect(tops).toEqual([...tops].sort((a, b) => a - b));
  expect(
    await page.evaluate(() =>
      [document.documentElement, document.querySelector(".d-scroll")!].map((box) => box.scrollWidth - box.clientWidth),
    ),
  ).toEqual([0, 0]);
});

test("the keyboard reaches a parent, then each of its edges and that edge's actions", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await open(page);
  const parents = section(page, "Parents");
  await parents.getByRole("button", { name: "Add parent" }).focus();
  await page.keyboard.press("Tab");
  await expect(parents.locator(".rail-link")).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(parents.getByRole("link", { name: `narrows Issue#${parent.seq}`, exact: true })).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(page.getByRole("button", { name: `Annotate narrows Issue#${parent.seq}` })).toBeFocused();
});
