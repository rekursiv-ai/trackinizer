import type { APIRequestContext } from "@playwright/test";
import { expect, test } from "./fixtures";
import { resetCanvas } from "./canvasState";

// The canvas's row: Back and Forward walk what the open tab showed, Home
// shows the graph. Each tab, in its station's group in the app's top bar,
// keeps a canvas of its own, on the server.

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

  const group = page.getByRole("banner", { name: "App" }).locator(".station-group.is-current");
  await group.locator(".station-group-title").click();
  await group.getByRole("button", { name: /^New tab in / }).click();
  await expect(page).toHaveURL(/#\/graph$/);
  await expect(graph).toHaveCount(0);
  await expect(back).toBeDisabled();
  await group.getByRole("button", { name: "Tab 1", exact: true }).click();
  await expect(graph).toBeVisible();
  await expect(page).toHaveURL(/#\/activity$/);

  // The tabs are the station's: a reload finds them, the one left open still open.
  await page.reload();
  await expect(group.getByRole("button", { name: "Tab 2", exact: true })).toBeAttached();
  await expect(group.locator('.station-tab[aria-current="true"]')).toHaveText("Tab 1");
  await expect(bar.getByRole("group", { name: "On show" })).toContainText("/Tab 1");
  await expect(graph).toBeVisible();
});
