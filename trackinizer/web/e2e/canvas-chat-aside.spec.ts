import { mkdirSync } from "node:fs";
import type { Locator, Page } from "@playwright/test";
import { expect, test } from "./fixtures";
import { exposeEventSources, pushNavigate, resetCanvas } from "./canvasState";

// Chat is docked beside the page. When the assistant moves the page it stands
// aside: the same tile floats over the page folded to a filled bar, opens under
// the pointer, folds when the pointer and the keyboard have left it, and a dock
// button on its bar puts it back, at either side of the page. A navigation is
// an agent's operation, which the --no-auth server this suite runs against
// refuses, so the script hands the page's stream the frame the server would
// send (as live/canvas-push.spec.ts does).

test.use({ canvas: true });

test.beforeEach(async ({ request }) => {
  await resetCanvas(request);
});

const SHOTS = "/opt/scratch/artifacts/trackinizer-web/canvas-chat/web/aside";

async function open(page: Page, theme: "dark" | "light" = "dark"): Promise<{ tile: Locator; bar: Locator; message: Locator }> {
  await page.addInitScript(exposeEventSources);
  await page.addInitScript((choice) => {
    try {
      localStorage.setItem("trackinizer.theme", choice);
    } catch {
      // about:blank has no storage.
    }
  }, theme);
  await page.setViewportSize({ width: 1280, height: 800 });
  await page.goto("/app/#/list/Issue");
  // By its panel's class, not its role: folded, Chat's panel is not drawn, and has no role to find it by.
  const tile = page.locator(".visual-tile:has(.chat-panel)");
  const message = tile.getByRole("textbox", { name: "Message" });
  await expect(message).toBeVisible();
  return { tile, bar: tile.locator(".visual-tile-toolbar"), message };
}

async function box(locator: Locator) {
  const found = await locator.boundingBox();
  expect(found).not.toBeNull();
  return found!;
}

/** Take the pointer off Chat, onto the page. */
async function leave(page: Page): Promise<void> {
  await page.mouse.move(500, 600);
}

test("Chat is docked beside the page, and stands aside as a folded bar when the assistant moves the page", async ({ page }) => {
  const { tile, bar, message } = await open(page);
  const stage = await box(page.locator(".visual-stage"));
  const docked = await box(tile);
  await expect(tile).toHaveClass(/visual-tile-side/);
  // The whole height of the stage, at its right edge, and the page beside it.
  expect(Math.round(docked.x + docked.width)).toBe(Math.round(stage.x + stage.width));
  expect(Math.round(docked.height)).toBe(Math.round(stage.height));
  const strip = page.locator(".visual-main-strip");
  expect(Math.round((await box(strip)).width + docked.width)).toBeLessThanOrEqual(Math.round(stage.width));

  await leave(page);
  await pushNavigate(page, "#/activity");
  await expect(page).toHaveURL(/#\/activity$/);
  await expect(tile).toHaveClass(/visual-tile-aside/);
  await expect(tile).toHaveClass(/visual-tile-collapsed/);
  await expect(message).toBeHidden();
  // A bar over the page, which now has the stage's whole width.
  expect((await box(tile)).height).toBeLessThan(60);
  expect(Math.round((await box(strip)).width)).toBe(Math.round(stage.width));
  // Filled with the accent, not the tile's own background.
  const fill = await bar.evaluate((element) => {
    const probe = document.createElement("i");
    probe.style.background = "var(--accent)";
    document.body.append(probe);
    const accent = getComputedStyle(probe).backgroundColor;
    probe.remove();
    return { bar: getComputedStyle(element).backgroundColor, accent };
  });
  expect(fill.bar).toBe(fill.accent);
});

test("Chat aside opens under the pointer, folds when the pointer leaves, and folds at once when the assistant moves the page again", async ({ page }) => {
  const { tile, bar, message } = await open(page);
  await leave(page);
  await pushNavigate(page, "#/activity");
  await expect(message).toBeHidden();

  await bar.locator("span").hover();
  await expect(message).toBeVisible();
  expect((await box(tile)).height).toBeGreaterThan(200);
  await leave(page);
  await expect(message).toBeHidden();

  await bar.locator("span").hover();
  await expect(message).toBeVisible();
  await pushNavigate(page, "#/list/Issue");
  await expect(message).toBeHidden();
});

test("the keyboard in Chat holds it open, what was open in it survives a fold, and docking puts the same Chat back", async ({ page }) => {
  const { tile, bar } = await open(page);
  // The suite's server has no assistant, so the message box is off; History takes the keyboard instead.
  const menu = tile.getByRole("menu", { name: "History" });
  await leave(page);
  await pushNavigate(page, "#/activity");
  await bar.locator("span").hover();
  await tile.getByRole("button", { name: "History" }).click();
  await expect(menu).toBeVisible();
  await leave(page);
  // Longer than the wait before a fold: the keyboard is still in it.
  await page.waitForTimeout(600);
  await expect(menu).toBeVisible();
  // A click on the page takes the keyboard out, and Chat folds.
  await page.locator(".visual-toolbar-label").click();
  await expect(menu).toBeHidden();

  await bar.locator("span").hover();
  await expect(menu).toBeVisible();
  await bar.getByRole("button", { name: "Dock Chat at the right" }).click();
  await expect(tile).toHaveClass(/visual-tile-side/);
  await expect(tile).not.toHaveClass(/visual-tile-floating/);
  // The same Chat, never mounted again: its History is still open.
  await expect(menu).toBeVisible();
  const stage = await box(page.locator(".visual-stage"));
  const docked = await box(tile);
  expect(Math.round(docked.x + docked.width)).toBe(Math.round(stage.x + stage.width));
  expect(Math.round(docked.height)).toBe(Math.round(stage.height));
});

test("the toolbar's Chat docks a Chat that stands aside, and a reload starts docked", async ({ page }) => {
  const { tile, message } = await open(page);
  await leave(page);
  await pushNavigate(page, "#/activity");
  await expect(tile).toHaveClass(/visual-tile-aside/);
  await page.locator(".visual-toolbar-actions").getByRole("button", { name: "Chat", exact: true }).click();
  await expect(tile).toHaveClass(/visual-tile-side/);
  await expect(message).toBeVisible();

  await pushNavigate(page, "#/list/Issue");
  await expect(tile).toHaveClass(/visual-tile-aside/);
  await page.reload();
  await expect(message).toBeVisible();
  await expect(tile).toHaveClass(/visual-tile-side/);
});

test("a dock button stands Chat at the left of the page at once, and brings it back from aside to the right", async ({ page }) => {
  const { tile, bar, message } = await open(page);
  const stage = await box(page.locator(".visual-stage"));
  await bar.getByRole("button", { name: "Dock Chat at the left" }).click();
  await expect(tile).toHaveClass(/visual-tile-left/);
  await expect(message).toBeVisible();
  const left = await box(tile);
  expect(Math.round(left.x)).toBe(Math.round(stage.x));
  expect(Math.round(left.height)).toBe(Math.round(stage.height));
  // The page starts where Chat's column, its rule included, ends.
  const column = await box(page.locator(".visual-side-column-left"));
  expect(Math.round((await box(page.locator(".visual-main-strip"))).x)).toBe(Math.round(column.x + column.width));
  await expect(bar.getByRole("button", { name: "Dock Chat at the left" })).toBeDisabled();

  await leave(page);
  await pushNavigate(page, "#/activity");
  await expect(tile).toHaveClass(/visual-tile-aside/);
  await bar.getByRole("button", { name: "Dock Chat at the right" }).click();
  await expect(tile).toHaveClass(/visual-tile-side/);
  await expect(message).toBeVisible();
  const right = await box(tile);
  expect(Math.round(right.x + right.width)).toBe(Math.round(stage.x + stage.width));
});

test("Chat dragged out of its column floats open under the pointer, and dropped at the left edge it docks there", async ({ page }) => {
  const { tile, bar, message } = await open(page);
  const stage = await box(page.locator(".visual-stage"));
  const title = await box(bar.locator("span"));
  await page.mouse.move(title.x + 20, title.y + title.height / 2);
  await page.mouse.down();
  await page.mouse.move(stage.x + 400, stage.y + 200, { steps: 8 });
  await expect(page.locator(".visual-drag-ghost")).toHaveText("Chat");
  await page.mouse.up();
  await expect(tile).toHaveClass(/visual-tile-aside/);
  // Open while the pointer that dropped it is on it; folded once the pointer has left.
  await expect(message).toBeVisible();
  expect(Math.round((await box(tile)).x - stage.x)).toBe(340);
  await leave(page);
  await expect(message).toBeHidden();

  const folded = await box(bar.locator("span"));
  await page.mouse.move(folded.x + 20, folded.y + folded.height / 2);
  await page.mouse.down();
  await page.mouse.move(stage.x + 8, stage.y + 300, { steps: 8 });
  await expect(page.locator('.visual-snap[data-zone="left"]')).toBeVisible();
  await page.mouse.up();
  await expect(tile).toHaveClass(/visual-tile-left/);
  await expect(message).toBeVisible();
  expect(Math.round((await box(tile)).x)).toBe(Math.round(stage.x));
});

test("the edge between Chat's column and the page resizes the column, and the width survives a reload", async ({ page }) => {
  const { tile } = await open(page);
  const edge = page.getByRole("separator", { name: "Resize the right column" });
  const before = await box(tile);
  const at = await box(edge);
  await page.mouse.move(at.x + at.width / 2, at.y + 300);
  await page.mouse.down();
  await page.mouse.move(at.x + at.width / 2 - 150, at.y + 300, { steps: 6 });
  await page.mouse.up();
  const wider = await box(tile);
  expect(Math.abs(wider.width - before.width - 150)).toBeLessThan(3);
  // The page gave up the room: nothing overflows sideways.
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1)).toBe(true);
  await page.reload();
  await expect(tile.getByRole("textbox", { name: "Message" })).toBeVisible();
  expect(Math.abs((await box(tile)).width - wider.width)).toBeLessThan(2);
  // So the next spec starts from the column's own width.
  await page.evaluate(() => localStorage.removeItem("trackinizer.v2.canvas.sizes"));
});

test("on a phone, Chat aside is a header under the page that Expand opens, with no sideways scroll", async ({ page }) => {
  const { tile, bar, message } = await open(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await pushNavigate(page, "#/activity");
  await expect(tile).toHaveClass(/visual-tile-aside/);
  await expect.poll(async () => Math.round((await box(tile)).height)).toBeLessThan(40);
  expect(Math.round((await box(tile)).width)).toBeLessThanOrEqual(390);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1)).toBe(true);
  await bar.getByRole("button", { name: "Expand", exact: true }).click();
  await message.scrollIntoViewIfNeeded();
  await expect(message).toBeInViewport();
  await bar.getByRole("button", { name: "Dock Chat at the right" }).click();
  await expect(tile).toHaveClass(/visual-tile-side/);
});

for (const theme of ["light", "dark"] as const) {
  test(`screenshots of Chat docked, aside and folded, and aside and open, ${theme} theme`, async ({ page }) => {
    mkdirSync(SHOTS, { recursive: true });
    const { bar, message } = await open(page, theme);
    await page.screenshot({ path: `${SHOTS}/chat-docked-${theme}.png` });
    await leave(page);
    await pushNavigate(page, "#/activity");
    await expect(message).toBeHidden();
    await page.screenshot({ path: `${SHOTS}/chat-aside-folded-${theme}.png` });
    await bar.locator("span").hover();
    await expect(message).toBeVisible();
    await page.screenshot({ path: `${SHOTS}/chat-aside-open-${theme}.png` });
    await bar.getByRole("button", { name: "Dock Chat at the left" }).click();
    await expect(page.locator(".visual-side-column-left")).toBeVisible();
    await leave(page);
    await page.screenshot({ path: `${SHOTS}/chat-docked-left-${theme}.png` });
  });
}
