import { mkdirSync } from "node:fs";
import type { APIRequestContext, Locator, Page } from "@playwright/test";
import { expect, test } from "./fixtures";
import { resetCanvas } from "./canvasState";

// A floating tile (the context graph, floated) moves by its whole top bar, a
// click on the bar folds it to the bar, and where it stands and whether it is
// folded survive a reload. Chat is docked, and floats only while it stands aside
// (canvas-chat-aside.spec.ts), so it is not the tile here.

test.use({ canvas: true });

test.afterEach(async ({ request }) => {
  await resetCanvas(request);
});

const SHOTS = "/opt/scratch/artifacts/trax-hosted/ui/chatwindow";

/** A canvas holding the page and the context graph of a fresh Issue floating over it. */
async function floatGraph(request: APIRequestContext): Promise<void> {
  const ok = async <T>(response: { ok(): boolean; text(): Promise<string>; json(): Promise<unknown> }) => {
    expect(response.ok(), await response.text()).toBe(true);
    return (await response.json()) as T;
  };
  await ok(await request.put("/api/me/visual-workspace", { data: { enabled: true } }));
  let state = await ok<{ id: string; revision: number; visuals: { id: string; type: string }[] }>(await request.post("/api/workspaces"));
  const apply = async (operation: object) => {
    state = await ok(await request.post(`/api/workspaces/${state.id}/operations`, {
      headers: { "Idempotency-Key": crypto.randomUUID() }, data: { revision: state.revision, operation },
    }));
  };
  const { ids } = await ok<{ ids: string[] }>(await request.post("/api/inquiries/batch", {
    data: { items: [{ kind: "Issue", title: `Float ${crypto.randomUUID().slice(0, 8)}`, idempotency_key: crypto.randomUUID() }], edges: [] },
  }));
  for (const visual of state.visuals.filter((held) => held.type !== "trax.browse")) await apply({ kind: "hide", instance_id: visual.id });
  await apply({ kind: "show", visual_type: "trax.subgraph", placement: "floating", record_id: ids[0] });
}

async function open(page: Page, theme: "dark" | "light" = "dark"): Promise<{ tile: Locator; bar: Locator }> {
  await page.addInitScript((choice) => {
    try {
      localStorage.setItem("trackinizer.theme", choice);
    } catch {
      // about:blank has no storage.
    }
  }, theme);
  await page.setViewportSize({ width: 1280, height: 800 });
  await page.goto("/app/#/list/Issue");
  const tile = page.locator(".visual-tile-floating");
  await expect(tile.getByRole("region", { name: "Context graph" })).toBeVisible();
  return { tile, bar: tile.locator(".visual-tile-toolbar") };
}

async function box(locator: Locator) {
  const found = await locator.boundingBox();
  expect(found).not.toBeNull();
  return found!;
}

/** Press on `title`, move by (dx, dy) in steps, release. */
async function dragBy(page: Page, title: Locator, dx: number, dy: number): Promise<void> {
  const at = await box(title);
  const x = at.x + at.width / 2;
  const y = at.y + at.height / 2;
  await page.mouse.move(x, y);
  await page.mouse.down();
  await page.mouse.move(x + dx / 2, y + dy / 2, { steps: 6 });
  await page.mouse.move(x + dx, y + dy, { steps: 6 });
  await page.mouse.up();
}

test("dragging the tile by its title bar moves it, and a drag does not fold it", async ({ page, request }) => {
  await floatGraph(request);
  const { tile, bar } = await open(page);
  const before = await box(tile);
  await dragBy(page, bar.locator("span"), -300, 120);
  const after = await box(tile);
  expect(Math.round(after.x - before.x)).toBe(-300);
  expect(Math.round(after.y - before.y)).toBe(120);
  expect(after.height).toBe(before.height);
  await expect(tile).not.toHaveClass(/visual-tile-collapsed/);
  await expect(tile.getByRole("region", { name: "Context graph" })).toBeVisible();
});

test("the tile follows the pointer on every frame, inside the page", async ({ page, request }) => {
  await floatGraph(request);
  const { tile, bar } = await open(page);
  const title = await box(bar.locator("span"));
  const start = await box(tile);
  await page.evaluate((startLeft) => {
    const seen: { off: number; late: number }[] = [];
    const pointer = { pressed: false, pressX: 0, x: 0, stamp: 0 };
    Object.assign(window, { followed: seen });
    addEventListener("pointerdown", (event) => { pointer.pressed = true; pointer.pressX = event.clientX; }, true);
    addEventListener("pointermove", (event) => {
      if (!pointer.pressed) return;
      pointer.x = event.clientX;
      pointer.stamp = event.timeStamp;
      requestAnimationFrame(() => {
        const now = document.querySelector(".visual-tile-floating")!.getBoundingClientRect();
        // By the frame the tile stands where the latest move put the pointer.
        seen.push({ off: Math.abs(now.left - startLeft - (pointer.x - pointer.pressX)), late: performance.now() - pointer.stamp });
      });
    }, true);
  }, start.x);
  await dragBy(page, bar.locator("span"), -200, 60);
  const seen = await page.evaluate(() => (window as unknown as { followed: { off: number; late: number }[] }).followed);
  expect(seen.length).toBeGreaterThanOrEqual(12);
  expect(Math.max(...seen.map((frame) => frame.off)), "the tile's distance from the pointer's place at a frame").toBeLessThanOrEqual(1);
  const lates = seen.map((frame) => frame.late).sort((a, b) => a - b);
  test.info().annotations.push({ type: "frame-latency-ms", description: `median ${lates[Math.floor(lates.length / 2)]!.toFixed(1)}, max ${lates.at(-1)!.toFixed(1)}, moves ${lates.length}` });
  expect(lates.at(-1)!).toBeLessThan(100);
  const end = await box(tile);
  expect(Math.round(end.x - start.x)).toBe(-200);
  expect(title.width).toBeGreaterThan(0);
});

test("the tile stays inside the page however far it is dragged", async ({ page, request }) => {
  await floatGraph(request);
  const { tile, bar } = await open(page);
  await dragBy(page, bar.locator("span"), -2000, -2000);
  const stage = await box(page.locator(".visual-stage"));
  const corner = await box(tile);
  expect(corner.x).toBeGreaterThanOrEqual(stage.x - 1);
  expect(corner.y).toBeGreaterThanOrEqual(stage.y - 1);
  await dragBy(page, bar.locator("span"), 3000, 3000);
  const far = await box(tile);
  expect(far.x + far.width).toBeLessThanOrEqual(stage.x + stage.width + 1);
  expect(far.y + far.height).toBeLessThanOrEqual(stage.y + stage.height + 1);
});

test("a click on the title bar folds the tile to its bar, and the next click opens it", async ({ page, request }) => {
  await floatGraph(request);
  const { tile, bar } = await open(page);
  const open_ = await box(tile);
  const place = { x: open_.x, y: open_.y };
  await bar.locator("span").click();
  await expect(tile).toHaveClass(/visual-tile-collapsed/);
  await expect(tile.getByRole("region", { name: "Context graph" })).toBeHidden();
  const folded = await box(tile);
  expect(folded.height).toBeLessThan(60);
  expect(folded.height).toBeCloseTo((await box(bar)).height + 2, 0);
  expect({ x: folded.x, y: folded.y }).toEqual(place);
  await bar.locator("span").click();
  await expect(tile).not.toHaveClass(/visual-tile-collapsed/);
  await expect(tile.getByRole("region", { name: "Context graph" })).toBeVisible();
  expect((await box(tile)).height).toBe(open_.height);
});

test("the bar's buttons still work, and the fold button is the keyboard's way to fold", async ({ page, request }) => {
  await floatGraph(request);
  const { tile, bar } = await open(page);
  const before = await box(tile);
  await bar.getByRole("button", { name: "Focus" }).click();
  await expect(tile).toHaveClass(/visual-tile-focused/);
  await expect(tile).not.toHaveClass(/visual-tile-collapsed/);
  expect(await box(tile)).toEqual(before);
  await bar.getByRole("button", { name: "Collapse Context graph" }).focus();
  await page.keyboard.press("Enter");
  await expect(tile).toHaveClass(/visual-tile-collapsed/);
  await expect(bar.getByRole("button", { name: "Expand Context graph" })).toHaveAttribute("aria-expanded", "false");
  await page.keyboard.press("Enter");
  await expect(tile).not.toHaveClass(/visual-tile-collapsed/);
});

test("the move handle and its arrow keys still move the tile", async ({ page, request }) => {
  await floatGraph(request);
  const { tile, bar } = await open(page);
  const handle = bar.getByRole("button", { name: "Move Context graph" });
  const before = await box(tile);
  await dragBy(page, handle, -100, 40);
  const dragged = await box(tile);
  expect(Math.round(dragged.x - before.x)).toBe(-100);
  expect(Math.round(dragged.y - before.y)).toBe(40);
  // A press on the handle that does not move leaves the tile open.
  await handle.click();
  await expect(tile).not.toHaveClass(/visual-tile-collapsed/);
  await handle.focus();
  await page.keyboard.press("ArrowLeft");
  expect(Math.round((await box(tile)).x - dragged.x)).toBe(-16);
});

test("where the tile stands and whether it is folded survive a reload", async ({ page, request }) => {
  await floatGraph(request);
  const { tile, bar } = await open(page);
  await dragBy(page, bar.locator("span"), -250, 90);
  const moved = await box(tile);
  await page.reload();
  await expect(tile.getByRole("region", { name: "Context graph" })).toBeVisible();
  const reloaded = await box(tile);
  expect(Math.round(reloaded.x)).toBe(Math.round(moved.x));
  expect(Math.round(reloaded.y)).toBe(Math.round(moved.y));

  await bar.locator("span").click();
  await expect(tile).toHaveClass(/visual-tile-collapsed/);
  await page.reload();
  await expect(tile).toHaveClass(/visual-tile-collapsed/);
  const folded = await box(tile);
  expect(Math.round(folded.x)).toBe(Math.round(moved.x));
  expect(Math.round(folded.y)).toBe(Math.round(moved.y));
});

test("after a fold held the tile inside the page, a drag starts from where it is drawn", async ({ page, request }) => {
  await floatGraph(request);
  const { tile, bar } = await open(page);
  await bar.locator("span").click();
  await dragBy(page, bar.locator("span"), 0, 2000);
  // Unfolded at the bottom, the page holds the tile higher than it was saved.
  await bar.locator("span").click();
  await expect(tile).not.toHaveClass(/visual-tile-collapsed/);
  const held = await box(tile);
  const title = await box(bar.locator("span"));
  await page.mouse.move(title.x + title.width / 2, title.y + title.height / 2);
  await page.mouse.down();
  await page.mouse.move(title.x + title.width / 2 + 10, title.y + title.height / 2, { steps: 4 });
  const during = await box(tile);
  expect(Math.abs(during.x - held.x - 10)).toBeLessThan(2);
  expect(Math.abs(during.y - held.y)).toBeLessThan(2);
  await page.mouse.up();
  const after = await box(tile);
  expect(Math.round(after.x - held.x)).toBe(10);
  expect(Math.round(after.y)).toBe(Math.round(held.y));
});

test("an arrow key after a fold held the tile inside the page moves it from where it is drawn", async ({ page, request }) => {
  await floatGraph(request);
  const { tile, bar } = await open(page);
  await bar.locator("span").click();
  await dragBy(page, bar.locator("span"), 0, 2000);
  await bar.locator("span").click();
  const held = await box(tile);
  await tile.getByRole("button", { name: "Move Context graph" }).press("ArrowUp");
  await expect.poll(async () => Math.round(held.y - (await box(tile)).y)).toBe(16);
});

test("a drag cut off by the tile leaving the page does not freeze every tile", async ({ page, request }) => {
  await floatGraph(request);
  const { tile, bar } = await open(page);
  const title = await box(bar.locator("span"));
  await page.mouse.move(title.x + 20, title.y + 10);
  await page.mouse.down();
  await page.mouse.move(title.x + 50, title.y + 10, { steps: 4 });
  // The tile is hidden under the pointer (an agent or another tab does the same), so no release reaches its bar.
  await page.evaluate(() => document.querySelector<HTMLElement>("[title='Dismiss visual']")!.click());
  await expect(page.locator(".visual-tile-floating")).toHaveCount(0);
  await page.mouse.up();
  await floatGraph(request);
  await expect(tile.getByRole("region", { name: "Context graph" })).toBeVisible();
  const before = await box(tile);
  await dragBy(page, bar.locator("span"), -100, 50);
  const after = await box(tile);
  expect(Math.round(after.x - before.x)).toBe(-100);
  expect(Math.round(after.y - before.y)).toBe(50);
});

test("a tile a narrow window pushed in returns to its saved place when the window grows back", async ({ page, request }) => {
  await floatGraph(request);
  const { tile, bar } = await open(page);
  await dragBy(page, bar.locator("span"), -200, 0);
  const placed = await box(tile);
  await page.setViewportSize({ width: 390, height: 800 });
  await page.setViewportSize({ width: 1280, height: 800 });
  await expect.poll(async () => Math.round((await box(tile)).x)).toBe(Math.round(placed.x));
  await page.setViewportSize({ width: 1000, height: 700 });
  await page.setViewportSize({ width: 1280, height: 800 });
  await expect.poll(async () => Math.round((await box(tile)).x)).toBe(Math.round(placed.x));
});

test("a smaller window holds a saved place inside the page", async ({ page, request }) => {
  await floatGraph(request);
  const { tile, bar } = await open(page);
  await dragBy(page, bar.locator("span"), 2000, 0);
  await page.setViewportSize({ width: 1000, height: 700 });
  const stage = await box(page.locator(".visual-stage"));
  await expect.poll(async () => {
    const held = await box(tile);
    return held.x + held.width <= stage.x + stage.width + 1;
  }).toBe(true);
});

test("a dock button stands the floating tile at that side of the page at once", async ({ page, request }) => {
  await floatGraph(request);
  const { bar } = await open(page);
  const stage = await box(page.locator(".visual-stage"));
  await bar.getByRole("button", { name: "Dock Context graph at the left" }).click();
  await expect(page.locator(".visual-tile-floating")).toHaveCount(0);
  const docked = page.locator(".visual-tile", { has: page.getByRole("region", { name: "Context graph" }) });
  await expect(docked).toHaveClass(/visual-tile-left/);
  expect(Math.round((await box(docked)).x)).toBe(Math.round(stage.x));
  await docked.getByRole("button", { name: "Dock Context graph at the right" }).click();
  await expect(docked).toHaveClass(/visual-tile-side/);
  const right = await box(docked);
  expect(Math.round(right.x + right.width)).toBe(Math.round(stage.x + stage.width));
});

for (const theme of ["light", "dark"] as const) {
  test(`screenshots of the tile open, folded and moved, ${theme} theme`, async ({ page, request }) => {
    mkdirSync(SHOTS, { recursive: true });
    await floatGraph(request);
    const { tile, bar } = await open(page, theme);
    await page.screenshot({ path: `${SHOTS}/float-default-${theme}.png` });
    await dragBy(page, bar.locator("span"), -420, 40);
    await page.screenshot({ path: `${SHOTS}/float-moved-${theme}.png` });
    await bar.locator("span").click();
    await expect(tile).toHaveClass(/visual-tile-collapsed/);
    await page.screenshot({ path: `${SHOTS}/float-folded-${theme}.png` });
  });
}
