import type { APIRequestContext, Page } from "@playwright/test";
import { expect, test } from "./fixtures";
import { resetCanvas } from "./canvasState";

// A phone shows Chat or the canvas, one at a time, by a switch along the bottom;
// Chat first. On the canvas every tile is full width, stacked, and the stage
// scrolls down. Beyond a phone's width there is no switch.

test.use({ canvas: true });

test.afterEach(async ({ request }) => {
  await resetCanvas(request);
});

/** A canvas holding the page and the context graph of a fresh Issue floating over it. */
async function floatGraph(request: APIRequestContext): Promise<void> {
  const ok = async <T>(response: { ok(): boolean; text(): Promise<string>; json(): Promise<unknown> }) => {
    expect(response.ok(), await response.text()).toBe(true);
    return (await response.json()) as T;
  };
  await ok(await request.put("/api/me/visual-canvas", { data: { enabled: true } }));
  const state = await ok<{ id: string; revision: number }>(await request.post("/api/canvases"));
  const { ids } = await ok<{ ids: string[] }>(await request.post("/api/inquiries/batch", {
    data: { items: [{ kind: "Issue", title: `Phone ${crypto.randomUUID().slice(0, 8)}`, idempotency_key: crypto.randomUUID() }], edges: [] },
  }));
  await ok(await request.post(`/api/canvases/${state.id}/operations`, {
    headers: { "Idempotency-Key": crypto.randomUUID() },
    data: { revision: state.revision, operation: { kind: "show", visual_type: "trax.subgraph", placement: "floating", record_id: ids[0] } },
  }));
}

async function open(page: Page, size: { width: number; height: number }) {
  await page.setViewportSize(size);
  await page.goto("/app/#/list/Issue");
  await expect(page.locator(".chat-row")).toBeVisible();
}

test("a phone shows Chat first and the switch swaps it with the canvas", async ({ page, request }) => {
  await floatGraph(request);
  await open(page, { width: 390, height: 844 });
  const chat = page.getByRole("region", { name: "Chat" });
  const switcher = page.getByRole("group", { name: "Chat or canvas" });
  await expect(chat).toBeVisible();
  await expect(page.locator(".visual-stage")).toBeHidden();
  await expect(switcher.getByRole("button", { name: "Chat" })).toHaveAttribute("aria-pressed", "true");
  await switcher.getByRole("button", { name: "Canvas" }).click();
  await expect(page.locator(".visual-stage")).toBeVisible();
  await expect(chat).toBeHidden();
  await expect(switcher).toBeInViewport();
  await switcher.getByRole("button", { name: "Chat" }).click();
  await expect(chat).toBeVisible();
  await expect(page.locator(".visual-stage")).toBeHidden();
});

test("on the canvas every tile is full width and stacked, and the stage scrolls down", async ({ page, request }) => {
  await floatGraph(request);
  await open(page, { width: 390, height: 600 });
  await page.getByRole("group", { name: "Chat or canvas" }).getByRole("button", { name: "Canvas" }).click();
  const stage = page.locator(".visual-stage");
  await expect(stage).toBeVisible();
  const tiles = page.locator(".visual-stage .visual-tile:not(.visual-browse-slot)");
  await expect(page.locator(".visual-tile-floating")).toBeVisible();
  await expect(tiles).toHaveCount(2);
  const boxes = [];
  for (const tile of await tiles.all()) {
    const found = (await tile.boundingBox())!;
    expect(Math.round(found.width), "full width").toBe(390);
    boxes.push(found);
  }
  boxes.sort((a, b) => a.y - b.y || a.x - b.x);
  expect(boxes[1]!.y, "stacked, not overlapping").toBeGreaterThanOrEqual(boxes[0]!.y + boxes[0]!.height - 1);
  const scroll = await stage.evaluate((element) => {
    element.scrollTop = element.scrollHeight;
    return { scrolls: element.scrollHeight > element.clientHeight, moved: element.scrollTop > 0, sideways: element.scrollWidth > element.clientWidth };
  });
  expect(scroll).toEqual({ scrolls: true, moved: true, sideways: false });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1)).toBe(true);
  await expect(tiles.last()).toBeInViewport();
});

test("beyond a phone's width there is no switch, and Chat and the canvas stand side by side", async ({ page, request }) => {
  await floatGraph(request);
  await open(page, { width: 1280, height: 800 });
  await expect(page.getByRole("group", { name: "Chat or canvas" })).toHaveCount(0);
  await expect(page.getByRole("region", { name: "Chat" })).toBeVisible();
  await expect(page.locator(".visual-stage")).toBeVisible();
});
