import type { Locator, Page } from "@playwright/test";
import type { CanvasState } from "../src/api/canvases";
import { resetCanvas } from "./canvasState";
import { expect, test } from "./fixtures";

test.use({ canvas: true });

test.afterEach(async ({ request }) => {
  await resetCanvas(request);
});

type Box = { readonly x: number; readonly y: number; readonly width: number; readonly height: number };

async function boxOf(locator: Locator): Promise<Box> {
  const found = await locator.boundingBox();
  expect(found).not.toBeNull();
  return found!;
}

/** Drag the pen from (`x1`, `y1`) to (`x2`, `y2`) on the page. */
async function draw(page: Page, from: readonly [number, number], to: readonly [number, number]) {
  await page.getByRole("button", { name: "Draw a box" }).click();
  await page.mouse.move(...from);
  await page.mouse.down();
  await page.mouse.move(...to, { steps: 5 });
  await page.mouse.up();
  await page.getByRole("button", { name: "Draw a box" }).click();
}

/** Where `inner` stands in `outer`, as fractions of it: its center, and its size. */
const within = (inner: Box, outer: Box) => ({
  x: (inner.x + inner.width / 2 - outer.x) / outer.width,
  y: (inner.y + inner.height / 2 - outer.y) / outer.height,
  width: inner.width / outer.width,
  height: inner.height / outer.height,
});

test("a box drawn over a tile stays on it, at the same fractions, when a second visual narrows the tile", async ({ page, request }) => {
  const id = await resetCanvas(request);
  const made = await request.post("/api/inquiries/batch", {
    data: { items: [{ kind: "Issue", title: "Pinned box", idempotency_key: crypto.randomUUID() }], edges: [] },
  });
  expect(made.ok(), await made.text()).toBe(true);
  const { ids } = await made.json() as { ids: string[] };
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.goto("/app/#/list/Issue");
  const body = page.locator(".visual-tile-browse .visual-tile-body");
  await expect(body).toBeVisible();
  const start = await boxOf(body);

  await draw(page, [start.x + start.width * 0.3, start.y + start.height * 0.3], [start.x + start.width * 0.6, start.y + start.height * 0.5]);

  const red = page.locator("[data-mark]");
  await expect(red).toHaveCount(1);
  await expect(body.locator("[data-mark]")).toHaveCount(1);
  const before = within(await boxOf(red), await boxOf(body));
  expect(before.x).toBeCloseTo(0.45, 1);

  const state = await (await request.get(`/api/canvases/${id}`)).json() as CanvasState;
  const shown = await request.post(`/api/canvases/${id}/operations`, {
    headers: { "Idempotency-Key": crypto.randomUUID() },
    data: { revision: state.revision, operation: { kind: "show", visual_type: "trax.timeline", placement: "main", record_id: ids[0] } },
  });
  expect(shown.ok(), await shown.text()).toBe(true);
  await expect(page.locator("[data-visual-type='trax.timeline']")).toBeVisible();
  await expect.poll(async () => (await boxOf(body)).width).toBeLessThan(start.width * 0.75);

  const narrowed = await boxOf(body);
  const box = await boxOf(red);
  const after = within(box, narrowed);
  expect(after.x).toBeCloseTo(before.x, 2);
  expect(after.y).toBeCloseTo(before.y, 2);
  expect(after.width).toBeCloseTo(before.width, 2);
  expect(box.x).toBeGreaterThanOrEqual(narrowed.x - 1);
  expect(box.x + box.width).toBeLessThanOrEqual(narrowed.x + narrowed.width + 1);
  expect(box.y).toBeGreaterThanOrEqual(narrowed.y - 1);
  expect(box.y + box.height).toBeLessThanOrEqual(narrowed.y + narrowed.height + 1);
});

test("a tiny box is shown at least 24 px wide and tall, centered where it was drawn", async ({ page, request }) => {
  await resetCanvas(request);
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.goto("/app/#/list/Issue");
  const body = page.locator(".visual-tile-browse .visual-tile-body");
  await expect(body).toBeVisible();
  const area = await boxOf(body);
  const [x, y] = [area.x + 400, area.y + 300];

  // A drag just past the least a drag may cover: 12 px across and 2 px down.
  await draw(page, [x, y], [x + 12, y + 2]);

  const red = page.locator("[data-mark]");
  await expect(red).toHaveCount(1);
  const box = await boxOf(red);
  expect(box.width).toBeGreaterThanOrEqual(24);
  expect(box.height).toBeGreaterThanOrEqual(24);
  expect(box.x + box.width / 2).toBeCloseTo(x + 6, 0);
  expect(box.y + box.height / 2).toBeCloseTo(y + 1, 0);
});
