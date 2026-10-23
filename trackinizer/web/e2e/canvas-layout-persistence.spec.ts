import type { APIRequestContext, Locator, Page } from "@playwright/test";
import { expect, test } from "./fixtures";
import { resetCanvas } from "./canvasState";

// A canvas keeps its layout in trax, as fractions of the stage (Chat's width in
// ch), so a second screen opens the same canvas with the same layout fitted to
// its own size, and writes nothing for the fit.

test.use({ canvas: true });

type Layout = { chat_ch?: number | null; columns?: { left?: number | null; side?: number | null } | null };

/** A canvas holding the page and the context graph of a fresh Issue docked at the right; its id. */
async function dockGraph(request: APIRequestContext): Promise<string> {
  const canvas = await resetCanvas(request);
  const read = async <T>(response: { ok(): boolean; text(): Promise<string>; json(): Promise<unknown> }) => {
    expect(response.ok(), await response.text()).toBe(true);
    return (await response.json()) as T;
  };
  const { ids } = await read<{ ids: string[] }>(await request.post("/api/inquiries/batch", {
    data: { items: [{ kind: "Issue", title: `Layout ${crypto.randomUUID().slice(0, 8)}`, idempotency_key: crypto.randomUUID() }], edges: [] },
  }));
  const state = await read<{ revision: number }>(await request.get(`/api/canvases/${canvas}`));
  await read(await request.post(`/api/canvases/${canvas}/operations`, {
    headers: { "Idempotency-Key": crypto.randomUUID() },
    data: { revision: state.revision, operation: { kind: "show", visual_type: "trax.subgraph", placement: "side", record_id: ids[0] } },
  }));
  return canvas;
}

async function layoutOf(request: APIRequestContext, canvas: string): Promise<{ layout: Layout | null; revision: number }> {
  const response = await request.get(`/api/canvases/${canvas}`);
  expect(response.ok()).toBe(true);
  return response.json();
}

async function box(locator: Locator) {
  const found = await locator.boundingBox();
  expect(found).not.toBeNull();
  return found!;
}

/** Drag `edge` by `dx` pixels. */
async function dragEdge(page: Page, edge: Locator, dx: number): Promise<void> {
  const at = await box(edge);
  await page.mouse.move(at.x + at.width / 2, at.y + 200);
  await page.mouse.down();
  await page.mouse.move(at.x + at.width / 2 + dx, at.y + 200, { steps: 6 });
  await page.mouse.up();
}

async function open(page: Page, width: number): Promise<void> {
  await page.setViewportSize({ width, height: 900 });
  await page.goto("/trax/#/list/Issue");
  await expect(page.locator(".chat-column").getByRole("region", { name: "Chat" })).toBeVisible();
  await expect(page.getByRole("separator", { name: "Resize the right column" })).toBeVisible();
}

/** One character of Chat's font, in pixels. */
async function chPixels(page: Page): Promise<number> {
  return page.locator(".chat-column").evaluate((column) => {
    const probe = Object.assign(document.createElement("i"), { style: "display: block; width: 100ch" });
    column.append(probe);
    const { width } = probe.getBoundingClientRect();
    probe.remove();
    return width / 100;
  });
}

test("a canvas's layout follows it to a narrower screen in a fresh browser: the same fractions, Chat in ch, nothing written for the fit", async ({ page, browser, request }) => {
  const canvas = await dockGraph(request);
  await open(page, 1600);
  const stage = page.locator(".visual-stage");
  const chat = page.locator(".chat-column");

  // Chat 60 px wider and the right column 60 px wider.
  await dragEdge(page, page.getByRole("separator", { name: "Resize Chat" }), 60);
  await dragEdge(page, page.getByRole("separator", { name: "Resize the right column" }), -60);
  const widths = { chat: (await box(chat)).width, column: (await box(page.locator(".visual-side-column:not(.visual-side-column-left)"))).width, stage: (await box(stage)).width };
  await expect.poll(async () => (await layoutOf(request, canvas)).layout).toMatchObject({ chat_ch: expect.any(Number), columns: { side: expect.any(Number) } });
  const kept = await layoutOf(request, canvas);
  const layout = kept.layout!;
  // Fractions of the stage, and Chat in ch of its own font.
  expect(Math.abs(layout.columns!.side! - widths.column / widths.stage)).toBeLessThan(0.005);
  expect(Math.abs(layout.chat_ch! * (await chPixels(page)) - widths.chat)).toBeLessThan(2);

  // A second screen: a fresh context, a narrower window, the same canvas.
  const second = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  try {
    const other = await second.newPage();
    await open(other, 1280);
    const stage2 = (await box(other.locator(".visual-stage"))).width;
    const fitted = (await box(other.locator(".visual-side-column:not(.visual-side-column-left)"))).width;
    // The column is the stored fraction of this stage, held between the least a column takes and seven tenths of the stage.
    const expected = Math.max(260, Math.min(layout.columns!.side! * stage2, stage2 * 0.7));
    expect(Math.abs(fitted - expected)).toBeLessThan(2);
    const chat2 = (await box(other.locator(".chat-column"))).width;
    const row2 = (await box(other.locator(".chat-row"))).width;
    const wanted = Math.max(280, Math.min(layout.chat_ch! * (await chPixels(other)), row2 * 0.7));
    expect(Math.abs(chat2 - wanted)).toBeLessThan(2);
    // Fitting is not a change: the canvas holds what it held, and nothing wrote to it.
    await other.waitForTimeout(1_000);
    expect((await layoutOf(request, canvas)).layout).toEqual(layout);
  } finally {
    await second.close();
  }
});
