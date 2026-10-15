import type { Locator, Page } from "@playwright/test";
import { expect, setPreferences, test } from "./fixtures";
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

async function open(page: Page, options: { theme?: "dark" | "light"; style?: "default" | "terminal" } = {}): Promise<{ column: Locator; stage: Locator; strip: Locator }> {
  await page.addInitScript(exposeEventSources);
  await setPreferences(page.request, { theme: options.theme ?? "dark", chat_style: options.style ?? "default" });
  await page.setViewportSize({ width: 1280, height: 800 });
  await page.goto("/app/#/list/Issue");
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
  expect(Math.round((await box(strip)).width)).toBe(16);
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
  await column.getByRole("button", { name: "Pin Chat beside the page" }).click();
  await leave(page);
  await page.waitForTimeout(500);
  await expect(column).toBeVisible();
  await expect(strip).toHaveCount(0);
  expect(await box(stage)).toEqual(beside);
  await expect(column.getByRole("button", { name: "Pin Chat beside the page" })).toHaveCount(0);
});

test("the keyboard inside the opened Chat holds it open until it leaves", async ({ page }) => {
  const { column, strip } = await open(page);
  await pushChat(page, true);
  await strip.hover();
  await expect(column).toBeVisible();
  // The suite's server has no assistant, so the message box is off; a button takes the keyboard.
  await column.locator(".station-name").focus();
  await leave(page);
  await page.waitForTimeout(700);
  await expect(column).toBeVisible();
  // A click on the workbench, beyond the opened Chat, takes the keyboard out of it.
  await page.mouse.click(900, 500);
  await expect(column).toBeHidden();
});

test("the opened Chat lies over the folded sidebar rail, so no rail icon shows through it", async ({ page }) => {
  const { column, strip } = await open(page);
  await expect(page.locator(".app")).toHaveClass(/sidebar-collapsed/);
  await pushChat(page, true);
  await strip.hover();
  await expect(column).toBeVisible();
  const rail = await box(page.locator(".sidebar-dock"));
  const columnBox = await box(column);
  expect(columnBox.x + columnBox.width).toBeGreaterThan(rail.x + rail.width);
  // Down the middle of the rail, every point is Chat's.
  const owners = await page.evaluate(({ x, top, bottom }) => {
    const found: string[] = [];
    for (let y = top + 20; y < bottom - 20; y += 40) {
      const at = document.elementFromPoint(x, y);
      found.push(at?.closest(".chat-column") ? "chat" : at?.closest(".sidebar-dock") ? "rail" : "other");
    }
    return found;
  }, { x: rail.x + rail.width / 2, top: rail.y, bottom: rail.y + rail.height });
  expect(new Set(owners)).toEqual(new Set(["chat"]));
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
  for (const style of ["default", "terminal"] as const) {
    test(`the strip is the accent colour in the ${theme} theme and the ${style} chat style`, async ({ page }) => {
      const { strip } = await open(page, { theme, style });
      await pushChat(page, true);
      await expect(strip).toBeVisible();
      const fill = await strip.evaluate((element) => {
        const probe = document.createElement("i");
        probe.style.background = "var(--accent)";
        document.body.append(probe);
        const accent = getComputedStyle(probe).backgroundColor;
        probe.remove();
        return { strip: getComputedStyle(element).backgroundColor, accent };
      });
      expect(fill.strip).toBe(fill.accent);
    });
  }
}
