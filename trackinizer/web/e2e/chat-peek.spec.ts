import type { Locator, Page } from "@playwright/test";
import { expect, revealBar, setPreferences, test } from "./fixtures";
import { exposeEventSources, pushChat, resetCanvas } from "./canvasState";

// An agent hides Chat's column with a `chat` frame (the --no-auth server this
// suite runs against refuses an agent's operation, so the script hands the
// page's stream the frame the server would send, as chat-column.spec.ts does
// for a navigation). Hidden, Chat leaves a strip; resting the pointer on it
// opens Chat over the workbench, which does not reflow; Pin stands it beside
// the workbench again.

test.use({ canvas: true });

test.beforeEach(async ({ request }) => {
  await resetCanvas(request);
});

async function open(page: Page, options: { theme?: "dark" | "light" } = {}): Promise<{ column: Locator; stage: Locator; strip: Locator }> {
  await page.addInitScript(exposeEventSources);
  await setPreferences(page.request, { theme: options.theme ?? "dark" });
  await page.setViewportSize({ width: 1280, height: 800 });
  await page.goto("/trax/#/list/Issue");
  const column = page.locator(".chat-column");
  await expect(column.getByRole("region", { name: "Chat" })).toBeVisible();
  return { column, stage: page.locator(".visual-stage"), strip: page.getByRole("button", { name: "Show Chat" }) };
}

async function box(locator: Locator) {
  const found = await locator.boundingBox();
  expect(found).not.toBeNull();
  return found!;
}

/** Take the pointer off Chat and its strip, onto the workbench. */
async function leave(page: Page): Promise<void> {
  await page.mouse.move(900, 500);
}

test("a chat frame hides Chat to its strip and a later one shows it again, the side kept", async ({ page }) => {
  const { column, stage, strip } = await open(page);
  const beside = await box(stage);
  await pushChat(page, true);
  await expect(column).toBeHidden();
  await expect(strip).toBeVisible();
  expect(Math.round((await box(strip)).width)).toBe(24);
  expect((await box(stage)).width).toBeGreaterThan(beside.width);
  await pushChat(page, false);
  await expect(column).toBeVisible();
  await expect(strip).toHaveCount(0);
  expect(await box(stage)).toEqual(beside);
});

test("resting on the strip opens Chat over the workbench at its column width, with no reflow; leaving folds it", async ({ page }) => {
  const { column, stage, strip } = await open(page);
  const width = (await box(column)).width;
  await pushChat(page, true);
  await leave(page);
  await expect(column).toBeHidden();
  const hiddenStage = await box(stage);
  await strip.hover();
  await expect(column).toBeVisible();
  expect(Math.abs((await box(column)).width - width)).toBeLessThan(2);
  expect(await box(stage)).toEqual(hiddenStage);
  await leave(page);
  await expect(column).toBeHidden();
});

test("Pin stands the opened Chat beside the workbench again, and it stays when the pointer leaves", async ({ page }) => {
  const { column, stage, strip } = await open(page);
  const beside = await box(stage);
  await pushChat(page, true);
  await strip.hover();
  // A press anywhere in the opened Chat pins it, so the ⋯ that opens the menu pins it too.
  await column.getByRole("button", { name: "More chat actions" }).click();
  await leave(page);
  await page.waitForTimeout(500);
  await expect(column).toBeVisible();
  await expect(strip).toHaveCount(0);
  expect(await box(stage)).toEqual(beside);
  await expect(column.getByRole("button", { name: "Pin Chat beside the page" })).toHaveCount(0);
  await expect(column.getByRole("button", { name: "Hide Chat" })).toBeVisible();
});

test("the keyboard inside the opened Chat holds it open until it leaves", async ({ page }) => {
  const { column, strip } = await open(page);
  await pushChat(page, true);
  await strip.hover();
  await expect(column).toBeVisible();
  // The suite's server has no assistant, so the message box is off; a button takes the keyboard.
  await column.getByRole("button", { name: "Move Chat to the right" }).focus();
  await leave(page);
  await page.waitForTimeout(700);
  await expect(column).toBeVisible();
  // A click on the workbench, beyond the opened Chat, takes the keyboard out of it.
  await page.mouse.click(900, 500);
  await expect(column).toBeHidden();
});

test("the opened Chat lies under the top bar, which stays whole above it", async ({ page }) => {
  const { column, strip } = await open(page);
  // The bar folds when the pointer leaves it, so it is kept open while the pointer rests on the strip.
  await (await revealBar(page)).getByRole("button", { name: "Keep app bar open" }).click();
  await pushChat(page, true);
  await strip.hover();
  await expect(column).toBeVisible();
  const bar = await box(page.getByRole("banner", { name: "App" }));
  expect((await box(column)).y).toBeGreaterThanOrEqual(bar.y + bar.height - 1);
  // Across the bar, every point is the bar's.
  const owners = await page.evaluate(({ y, left, right }) => {
    const found: string[] = [];
    for (let x = left + 20; x < right - 20; x += 60) found.push(document.elementFromPoint(x, y)?.closest(".topbar") ? "bar" : "other");
    return found;
  }, { y: bar.y + bar.height / 2, left: bar.x, right: bar.x + bar.width });
  expect(new Set(owners)).toEqual(new Set(["bar"]));
});

test("a click anywhere in the opened Chat pins it beside the workbench, and it stays when the pointer leaves", async ({ page }) => {
  const { column, stage, strip } = await open(page);
  const beside = await box(stage);
  await pushChat(page, true);
  await strip.hover();
  await expect(column).toBeVisible();
  const opened = await box(column);
  await page.mouse.click(opened.x + opened.width / 2, opened.y + opened.height / 2);
  await leave(page);
  await page.waitForTimeout(500);
  await expect(column).toBeVisible();
  await expect(column).not.toHaveClass(/chat-column-peek/);
  await expect(strip).toHaveCount(0);
  expect(await box(stage)).toEqual(beside);
});

test("a hidden Chat shown by the hide key or the strip is a column; a hide key press hides it again", async ({ page }) => {
  const { column, strip } = await open(page);
  await pushChat(page, true);
  await strip.click();
  await expect(column).toBeVisible();
  await expect(page.getByRole("separator", { name: "Resize Chat" })).toBeVisible();
  await page.locator(".visual-canvas").click({ position: { x: 4, y: 4 } });
  await page.keyboard.press("Control+Backslash");
  await expect(column).toBeHidden();
});

for (const theme of ["dark", "light"] as const) {
  test(`the strip's line toward the workbench is the accent colour in the ${theme} theme`, async ({ page }) => {
    const { strip } = await open(page, { theme });
    await pushChat(page, true);
    await expect(strip).toBeVisible();
    // Chat is at the left, so the line is on the strip's right.
    const line = await strip.evaluate((element) => {
      const probe = document.createElement("i");
      probe.style.color = "var(--accent)";
      document.body.append(probe);
      const accent = getComputedStyle(probe).color;
      probe.remove();
      const style = getComputedStyle(element);
      return { accent, color: style.borderRightColor, width: style.borderRightWidth };
    });
    expect([line.color, line.width]).toEqual([line.accent, "2px"]);
  });
}
