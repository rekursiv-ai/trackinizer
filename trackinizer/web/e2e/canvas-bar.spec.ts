import type { APIRequestContext } from "@playwright/test";
import { expect, test } from "./fixtures";
import { resetCanvas } from "./canvasState";

// The canvas's row: Back and Forward walk what the canvas showed, Home shows
// the graph, and the strip lists the records the canvas worked on.

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

test("Back, Forward and Home move the canvas, and the strip shows or closes what it worked on", async ({ page, request }) => {
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

  // The graph's Issue is listed, pressed while shown. Its pane's own close takes it off and the
  // page stays; the chip, unpressed, shows it again.
  const worked = bar.getByRole("navigation", { name: "Worked on" }).getByRole("button");
  await expect(worked).toHaveCount(1);
  await expect(worked).toHaveAttribute("aria-pressed", "true");
  await graph.locator('[title="Dismiss visual"]').click();
  await expect(graph).toHaveCount(0);
  await expect(worked).toHaveAttribute("aria-pressed", "false");
  await expect(page.locator(".visual-tile-browse")).toBeVisible();
  // Shown again from the strip, it stands where the catalog puts it, not where it was closed.
  await worked.click();
  await expect(page.locator('.visual-tile[data-visual-type="trax.subgraph"]')).toBeVisible();
  await expect(worked).toHaveAttribute("aria-pressed", "true");
});

test("the strip stands after Clear, beside Home, and new workstreams are named inline", async ({ page, request }) => {
  await pageAndGraph(request);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/app/#/list/Issue");
  const bar = page.getByRole("toolbar", { name: "Canvas" });
  await expect(bar).toBeVisible();
  await expect(page.locator(".topbar")).toHaveClass(/topbar-auto-hidden/);
  const main = await page.locator(".main").boundingBox();
  expect(main!.y).toBeCloseTo(900 - main!.y - main!.height, 0);
  const worked = bar.getByRole("navigation", { name: "Worked on" });
  const clear = bar.getByRole("button", { name: "Clear canvas" });
  const home = bar.getByRole("button", { name: "Home" });
  for (const width of [1440, 800]) {
    await page.setViewportSize({ width, height: 900 });
    const strip = await worked.boundingBox();
    const trash = await clear.boundingBox();
    const house = await home.boundingBox();
    expect(strip).not.toBeNull();
    expect(trash).not.toBeNull();
    expect(trash!.x - (house!.x + house!.width)).toBeGreaterThanOrEqual(0);
    expect(trash!.x - (house!.x + house!.width)).toBeLessThan(8);
    expect(strip!.x).toBeGreaterThanOrEqual(trash!.x + trash!.width);
  }

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

test("the canvas holds three record panes; a fourth takes the least recent one's place, and the strip leads with them", async ({ page, request }) => {
  const ok = async <T>(response: { ok(): boolean; text(): Promise<string>; json(): Promise<unknown> }) => {
    expect(response.ok(), await response.text()).toBe(true);
    return (await response.json()) as T;
  };
  const station = await ok<{ id: string; revision: number }>(await request.post("/api/workspaces/new"));
  const tag = crypto.randomUUID().slice(0, 6);
  const titles = ["A", "B", "C", "D"].map((letter) => `Pane ${letter} ${tag}`);
  const { ids } = await ok<{ ids: string[] }>(await request.post("/api/inquiries/batch", {
    data: { items: titles.map((title) => ({ kind: "Issue", title, idempotency_key: crypto.randomUUID() })), edges: [] },
  }));
  let revision = station.revision;
  for (const id of ids) {
    const state = await ok<{ revision: number }>(await request.post(`/api/workspaces/${station.id}/operations`, {
      headers: { "Idempotency-Key": crypto.randomUUID() },
      data: { revision, operation: { kind: "show", visual_type: "trax.timeline", record_id: id, beside: true } },
    }));
    revision = state.revision;
  }
  await page.goto("/app/#/list/Issue");
  const panes = page.locator('.visual-tile[data-visual-type="trax.timeline"]');
  await expect(panes).toHaveCount(3);
  const chips = page.getByRole("navigation", { name: "Worked on" }).getByRole("button");
  const pressed = async () => Promise.all((await chips.all()).map(async (chip) => [
    (await chip.textContent())?.replace(/^Lineage Issue#\d+ /, ""), await chip.getAttribute("aria-pressed"),
  ]));
  // D took A's place; the panes lead the strip in their order, then A.
  await expect.poll(pressed).toEqual([[titles[3], "true"], [titles[1], "true"], [titles[2], "true"], [titles[0], "false"]]);

  // Showing A again replaces B, the least recent of the three, in its place.
  await chips.filter({ hasText: titles[0] }).click();
  await expect(panes).toHaveCount(3);
  await expect.poll(pressed).toEqual([[titles[3], "true"], [titles[0], "true"], [titles[2], "true"], [titles[1], "false"]]);
});
