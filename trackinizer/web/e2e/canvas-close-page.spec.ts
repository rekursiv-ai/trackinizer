import type { APIRequestContext } from "@playwright/test";
import { expect, test } from "./fixtures";
import { resetCanvas } from "./canvasState";

// The page (Browse) closes like any other tile, and the user's own navigation
// shows it again.

test.use({ canvas: true });

test.afterEach(async ({ request }) => {
  await resetCanvas(request);
});

/** A canvas holding the page and the context graph of a fresh Issue docked beside it. */
async function pageAndGraph(request: APIRequestContext): Promise<void> {
  const ok = async <T>(response: { ok(): boolean; text(): Promise<string>; json(): Promise<unknown> }) => {
    expect(response.ok(), await response.text()).toBe(true);
    return (await response.json()) as T;
  };
  const state = await ok<{ id: string; revision: number }>(await request.post("/api/workspaces/new"));
  const { ids } = await ok<{ ids: string[] }>(await request.post("/api/inquiries/batch", {
    data: { items: [{ kind: "Issue", title: `Close ${crypto.randomUUID().slice(0, 8)}`, idempotency_key: crypto.randomUUID() }], edges: [] },
  }));
  await ok(await request.post(`/api/workspaces/${state.id}/operations`, {
    headers: { "Idempotency-Key": crypto.randomUUID() },
    data: { revision: state.revision, operation: { kind: "show", visual_type: "trax.subgraph", placement: "side", record_id: ids[0] } },
  }));
}

test("a closed page comes back with the user's next navigation", async ({ page, request }) => {
  await pageAndGraph(request);
  await page.goto("/app/#/list/Issue");
  const browse = page.locator(".visual-tile-browse");
  await expect(browse).toBeVisible();

  await browse.getByTitle("Dismiss visual").click();
  await expect(browse).toHaveCount(0);
  await expect(page.locator(".visual-tile-side")).toBeVisible();

  await page.evaluate(() => { location.hash = "#/activity"; });
  await expect(browse).toBeVisible();
  expect(new URL(page.url()).hash).toBe("#/activity");
});

test("the Workbench section offers a closed page and brings it back", async ({ page, request }) => {
  await pageAndGraph(request);
  await page.goto("/app/#/list/Issue");
  const browse = page.locator(".visual-tile-browse");
  await browse.getByTitle("Dismiss visual").click();
  await expect(browse).toHaveCount(0);

  await page.getByRole("button", { name: "Workbench", exact: true }).first().click();
  const option = page.getByRole("checkbox", { name: /Browse/ });
  await expect(option).not.toBeChecked();
  // The box follows the canvas once the server has answered, so a click is not a check.
  await option.click();
  await expect(browse).toBeVisible();
  await expect(option).toBeChecked();
});
