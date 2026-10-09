import { mkdirSync } from "node:fs";
import type { APIRequestContext, Page } from "@playwright/test";
import { expect, test } from "./fixtures";
import { resetCanvas } from "./canvasState";

// The canvas lays Chat and the other visuals out without sideways scroll at
// 1280 x 800, and usably at 390 px (a phone).

test.use({ canvas: true });

/** What a spec leaves on the shared canvas would change the layout of the next one. */
test.afterEach(async ({ request }) => {
  await resetCanvas(request);
});

const SHOTS = "/opt/scratch/artifacts/trackinizer-web/canvas-chat/web/layout";

type Operation = { kind: "show"; visual_type: string; record_id?: string; placement?: "main" | "side" | "floating" };

async function ok<T>(response: { ok(): boolean; text(): Promise<string>; json(): Promise<unknown> }): Promise<T> {
  expect(response.ok(), await response.text()).toBe(true);
  return (await response.json()) as T;
}

/** A canvas holding exactly the visuals `operations` show, over a fresh Issue. */
async function arrange(request: APIRequestContext, operations: (issue: string, artifact: string) => Operation[]): Promise<void> {
  await ok(await request.put("/api/me/visual-workspace", { data: { enabled: true } }));
  const { ids } = await ok<{ ids: string[] }>(await request.post("/api/inquiries/batch", {
    data: { items: [{ kind: "Issue", title: `Layout ${crypto.randomUUID().slice(0, 8)}`, idempotency_key: crypto.randomUUID() }], edges: [] },
  }));
  const published = await ok<{ artifact_id: string }>(await request.post("/api/artifacts/content", {
    headers: { "Idempotency-Key": crypto.randomUUID() },
    data: {
      issue_id: ids[0], title: "Layout artifact", summary: "A report.", format: "structured",
      sections: [{ title: "Finding", summary: "One finding.", details: "Details.", findings: [] }],
      citations: [{ record_id: ids[0] }],
    },
  }));
  let state = await ok<{ id: string; revision: number; visuals: { id: string; type: string }[] }>(await request.post("/api/workspaces"));
  // Drop every visual but the page, so each case starts from the same canvas.
  for (const visual of state.visuals.filter((held) => held.type !== "trax.browse")) {
    state = await ok(await request.post(`/api/workspaces/${state.id}/operations`, {
      headers: { "Idempotency-Key": crypto.randomUUID() }, data: { revision: state.revision, operation: { kind: "hide", instance_id: visual.id } },
    }));
  }
  for (const operation of operations(ids[0]!, published.artifact_id)) {
    state = await ok(await request.post(`/api/workspaces/${state.id}/operations`, {
      headers: { "Idempotency-Key": crypto.randomUUID() }, data: { revision: state.revision, operation },
    }));
  }
}

/** What overflows sideways: the page, the stage, and each tile with its content. */
async function overflow(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const wide = (element: Element, name: string) => (element.scrollWidth > element.clientWidth + 1 ? [`${name} ${element.scrollWidth}>${element.clientWidth}`] : []);
    return [
      ...wide(document.documentElement, "page"),
      ...[...document.querySelectorAll(".visual-stage")].flatMap((element) => wide(element, "stage")),
      ...[...document.querySelectorAll(".visual-tile")].flatMap((element) => wide(element, `tile ${element.querySelector(".visual-tile-toolbar span")?.textContent ?? "page"}`)),
    ];
  });
}

const CASES: { name: string; operations: (issue: string, artifact: string) => Operation[] }[] = [
  { name: "chat-side", operations: () => [{ kind: "show", visual_type: "trax.chat", placement: "side" }] },
  { name: "chat-side-timeline-main", operations: (issue) => [
    { kind: "show", visual_type: "trax.chat", placement: "side" },
    { kind: "show", visual_type: "trax.timeline", placement: "main", record_id: issue }] },
  { name: "chat-side-subgraph-side", operations: (issue) => [
    { kind: "show", visual_type: "trax.chat", placement: "side" },
    { kind: "show", visual_type: "trax.subgraph", placement: "side", record_id: issue }] },
  { name: "chat-main-subgraph-main", operations: (issue) => [
    { kind: "show", visual_type: "trax.chat", placement: "main" },
    { kind: "show", visual_type: "trax.subgraph", placement: "main", record_id: issue }] },
];

for (const { name, operations } of CASES) {
  test(`at 1280 x 800 the canvas has no sideways scroll with ${name}`, async ({ page, request }) => {
    mkdirSync(SHOTS, { recursive: true });
    await arrange(request, operations);
    await page.setViewportSize({ width: 1280, height: 800 });
    await page.goto("/app/#/list/Issue");
    await expect(page.getByRole("region", { name: "Chat" })).toBeVisible();
    await page.waitForTimeout(500);
    await page.screenshot({ path: `${SHOTS}/${name}-1280.png` });
    expect(await overflow(page)).toEqual([]);
  });

  test(`at 390 px the canvas is usable with ${name}: no sideways scroll, and the message box reachable`, async ({ page, request }) => {
    await arrange(request, operations);
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto("/app/#/list/Issue");
    await expect(page.getByRole("region", { name: "Chat" })).toBeVisible();
    await page.waitForTimeout(500);
    await page.screenshot({ path: `${SHOTS}/${name}-390.png` });
    expect(await overflow(page)).toEqual([]);
    const box = page.getByRole("textbox", { name: "Message" });
    await box.scrollIntoViewIfNeeded();
    const bounds = (await box.boundingBox())!;
    expect(bounds.x).toBeGreaterThanOrEqual(0);
    expect(bounds.x + bounds.width).toBeLessThanOrEqual(390);
    expect(bounds.y + bounds.height).toBeLessThanOrEqual(844);
    // Chat and the page, the two a phone mostly shows, fit the screen together: the send button is on it too.
    if (name === "chat-side") {
      const send = (await page.getByRole("button", { name: "Send message" }).boundingBox())!;
      expect(send.y + send.height).toBeLessThanOrEqual(844);
    }
    await expect(page.getByRole("button", { name: "Send message" })).toBeVisible();
  });
}

const FIVE = (issue: string, artifact: string): Operation[] => [
  { kind: "show", visual_type: "trax.chat", placement: "side" },
  { kind: "show", visual_type: "trax.timeline", placement: "main", record_id: issue },
  { kind: "show", visual_type: "trax.artifact", placement: "main", record_id: artifact },
  { kind: "show", visual_type: "trax.subgraph", placement: "side", record_id: issue },
];

for (const screen of [{ width: 1280, height: 800 }, { width: 1440, height: 900 }]) {
  test(`at ${screen.width} px the page with four visuals has no sideways scroll, and Chat's controls are all reachable`, async ({ page, request }) => {
    mkdirSync(SHOTS, { recursive: true });
    await arrange(request, FIVE);
    await page.setViewportSize(screen);
    await page.goto("/app/#/list/Issue");
    await expect(page.getByRole("region", { name: "Chat" })).toBeVisible();
    await expect(page.getByRole("region", { name: "Context graph" })).toBeVisible();
    await page.waitForTimeout(500);
    await page.screenshot({ path: `${SHOTS}/five-${screen.width}.png` });
    expect(await overflow(page)).toEqual([]);
    for (const name of ["History", "Clear chat"]) {
      await expect(page.getByRole("button", { name, exact: true }), `Chat's ${name}`).toBeInViewport({ ratio: 1 });
    }
    await expect(page.getByRole("textbox", { name: "Message" })).toBeInViewport({ ratio: 1 });
  });
}

test("with Chat beside the page, an Issue's title stays readable beside its label chips", async ({ page, request }) => {
  // The page narrowed as far as the default canvas narrows it: Chat and a second main visual beside it.
  await arrange(request, (issue) => [
    { kind: "show", visual_type: "trax.chat", placement: "side" },
    { kind: "show", visual_type: "trax.timeline", placement: "main", record_id: issue }]);
  const title = "A title long enough that its labels cannot take all of the row";
  const created = await request.post("/api/inquiries/issue", {
    data: { title, labels: ["org:rekursiv,arc3,ac3,engineering", "chat", "trackinizer", "webui"], idempotency_key: crypto.randomUUID() },
  });
  expect(created.ok(), await created.text()).toBe(true);
  for (const width of [1280, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/app/#/list/Issue");
    const row = page.locator("a.row", { hasText: title }).first();
    await expect(row).toBeVisible();
    const shown = await row.locator(".row-title").evaluate((element) => element.clientWidth);
    expect(shown, `the title's room at ${width} px`).toBeGreaterThanOrEqual(110);
  }
});

for (const theme of ["dark", "light"] as const) {
  test(`the context graph's zoom buttons take the app's colours, ${theme} theme`, async ({ page, request }) => {
    await arrange(request, (issue) => [{ kind: "show", visual_type: "trax.subgraph", placement: "side", record_id: issue }]);
    await page.addInitScript((choice) => {
      try {
        localStorage.setItem("trackinizer.theme", choice);
      } catch {
        // about:blank has no storage.
      }
    }, theme);
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto("/app/#/list/Issue");
    await expect(page.getByRole("group", { name: "Zoom" })).toBeVisible();
    const colours = await page.evaluate(() => {
      const probe = (token: string) => {
        const element = Object.assign(document.createElement("div"), { style: `background: var(${token}); color: var(${token})` });
        document.body.append(element);
        const { backgroundColor } = getComputedStyle(element);
        element.remove();
        return backgroundColor;
      };
      const group = document.querySelector(".graph-zoom")!;
      const button = group.querySelector("button")!;
      return {
        overlay: probe("--surface-overlay"), muted: probe("--muted"),
        group: getComputedStyle(group).backgroundColor, buttonInk: getComputedStyle(button).color,
      };
    });
    expect(colours.group, "the zoom group's background").toBe(colours.overlay);
    expect(colours.buttonInk, "a zoom button's glyph colour").toBe(colours.muted);
  });
}
