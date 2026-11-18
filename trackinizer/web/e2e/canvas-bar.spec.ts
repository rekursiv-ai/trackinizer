import type { APIRequestContext } from "@playwright/test";
import { expect, test } from "./fixtures";
import { resetCanvas } from "./canvasState";

// The canvas's row: Back and Forward walk what the open tab showed, Home
// shows the graph. Each tab in the canvas toolbar keeps its own canvas on the server.

test.use({ canvas: true });

test.afterEach(async ({ request }) => {
  await resetCanvas(request);
});

/** A new station holding the page and the context graph of a fresh Issue docked at its right. */
async function pageAndGraph(request: APIRequestContext): Promise<void> {
  const ok = async <T>(response: { ok(): boolean; text(): Promise<string>; json(): Promise<unknown> }) => {
    expect(response.ok(), await response.text()).toBe(true);
    return (await response.json()) as T;
  };
  const state = await ok<{ id: string; revision: number }>(await request.post("/api/workspaces/new"));
  const { ids } = await ok<{ ids: string[] }>(await request.post("/api/inquiries/batch", {
    data: { items: [{ kind: "Issue", title: `Bar ${crypto.randomUUID().slice(0, 8)}`, idempotency_key: crypto.randomUUID() }], edges: [] },
  }));
  await ok(await request.post(`/api/workspaces/${state.id}/operations`, {
    headers: { "Idempotency-Key": crypto.randomUUID() },
    data: { revision: state.revision, operation: { kind: "show", visual_type: "trax.subgraph", placement: "side", record_id: ids[0] } },
  }));
}

test("Back, Forward and Home move the canvas, and each tab keeps its own", async ({ page, request }) => {
  await pageAndGraph(request);
  await page.goto("/app/#/list/Issue");
  const graph = page.locator(".visual-tile-side");
  await expect(graph).toBeVisible();
  const bar = page.getByRole("toolbar", { name: "Canvas" });
  const back = bar.getByRole("button", { name: "Back" });
  const forward = bar.getByRole("button", { name: "Forward" });
  await expect(back).toBeDisabled();

  await page.evaluate(() => { location.hash = "#/activity"; });
  await back.click();
  await expect(page).toHaveURL(/#\/list\/Issue$/);
  await forward.click();
  await expect(page).toHaveURL(/#\/activity$/);

  await bar.getByRole("button", { name: "Home" }).click();
  await expect(page).toHaveURL(/#\/graph$/);
  await expect(graph).toHaveCount(0);
  await back.click();
  await expect(graph).toBeVisible();
  await expect(page).toHaveURL(/#\/activity$/);

  await bar.getByRole("button", { name: "New tab" }).click();
  await expect(page).toHaveURL(/#\/graph$/);
  await expect(graph).toHaveCount(0);
  await expect(back).toBeDisabled();
  await bar.getByRole("button", { name: "Tab 1", exact: true }).click();
  await expect(graph).toBeVisible();
  await expect(page).toHaveURL(/#\/activity$/);

  // The tabs are the station's: a reload finds them, the one left open still open.
  await page.reload();
  await expect(bar.getByRole("button", { name: "Tab 2", exact: true })).toBeAttached();
  await expect(bar.locator('.canvas-tab[aria-current="true"]')).toHaveText("Tab 1");
  await expect(graph).toBeVisible();
});

test("tab controls stay beside their tabs and new names are edited inline", async ({ page, request }) => {
  await pageAndGraph(request);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/app/#/list/Issue");
  const bar = page.getByRole("toolbar", { name: "Canvas" });
  await expect(bar).toBeVisible();
  await expect(page.locator(".topbar")).toHaveClass(/topbar-auto-hidden/);
  const main = await page.locator(".main").boundingBox();
  expect(main!.y).toBeCloseTo(900 - main!.y - main!.height, 0);
  const tabs = bar.getByRole("navigation", { name: "Canvas tabs" });
  const add = bar.getByRole("button", { name: "New tab" });
  const clear = bar.getByRole("button", { name: "Clear canvas" });
  const home = bar.getByRole("button", { name: "Home" });
  for (const width of [1440, 800]) {
    await page.setViewportSize({ width, height: 900 });
    const strip = await tabs.boundingBox();
    const plus = await add.boundingBox();
    const trash = await clear.boundingBox();
    const house = await home.boundingBox();
    expect(strip).not.toBeNull();
    expect(plus).not.toBeNull();
    expect(trash).not.toBeNull();
    expect(plus!.x - (strip!.x + strip!.width)).toBeLessThan(8);
    expect(trash!.x - (house!.x + house!.width)).toBeGreaterThanOrEqual(0);
    expect(trash!.x - (house!.x + house!.width)).toBeLessThan(8);
    expect(strip!.x).toBeGreaterThanOrEqual(trash!.x + trash!.width);
    expect(Math.abs(plus!.y + plus!.height / 2 - trash!.y - trash!.height / 2)).toBeLessThan(1);
  }
  await add.click();
  const name = bar.getByRole("textbox", { name: "Tab name" });
  await expect(name).toBeFocused();
  await name.fill("Results");
  const saved = page.waitForResponse((response) => response.url().endsWith("/layout") && response.request().method() === "PUT"
    && response.request().postData()?.includes('"Results"') === true && response.ok());
  await name.press("Enter");
  await saved;
  await expect(bar.getByRole("button", { name: "Results", exact: true })).toBeVisible();
  await page.reload();
  await expect(bar.getByRole("button", { name: "Results", exact: true })).toBeVisible();

  await page.setViewportSize({ width: 1440, height: 900 });
  await page.getByRole("button", { name: "Open app bar" }).click();
  const streams = page.getByRole("navigation", { name: "Workstreams" });
  const newStream = page.getByRole("button", { name: "New workstream" });
  const strip = await streams.boundingBox();
  const plus = await newStream.boundingBox();
  expect(plus!.x - (strip!.x + strip!.width)).toBeLessThan(8);
  await newStream.click();
  const streamName = streams.getByRole("textbox", { name: "Workstream name" });
  await expect(streamName).toBeFocused();
  await streamName.fill("Review work");
  await streamName.press("Enter");
  await expect(streams.getByRole("button", { name: "Review work", exact: true })).toBeVisible();
});
