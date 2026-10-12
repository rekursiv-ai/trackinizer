import type { Locator, Page } from "@playwright/test";
import { expect, test } from "./fixtures";
import { exposeEventSources, pushNavigate, resetCanvas } from "./canvasState";

// Chat is a column of its own beside the canvas: a third of the screen or 80
// characters wide, resized by the divider, flipped to the other side by a
// button in its header, hidden to a strip by a key and full screen by another.
// The --no-auth server this suite runs against has no assistant, so Chat cannot
// send here; an agent's navigation is the frame the script hands the page's
// stream (as live/canvas-push.spec.ts does).

test.use({ canvas: true });

test.beforeEach(async ({ request }) => {
  await resetCanvas(request);
});

async function open(page: Page, size = { width: 1280, height: 800 }): Promise<{ column: Locator; stage: Locator }> {
  await page.addInitScript(exposeEventSources);
  await page.setViewportSize(size);
  await page.goto("/app/#/list/Issue");
  const column = page.locator(".chat-column");
  await expect(column.getByRole("region", { name: "Chat" })).toBeVisible();
  await expect(page.locator(".visual-stage")).toBeVisible();
  return { column, stage: page.locator(".visual-stage") };
}

async function box(locator: Locator) {
  const found = await locator.boundingBox();
  expect(found).not.toBeNull();
  return found!;
}

test("Chat is a column beside the workbench, on the left, a third of the screen wide", async ({ page }) => {
  const { column, stage } = await open(page);
  const chat = await box(column);
  const work = await box(stage);
  // Beside the canvas, the whole height of the row it shares, and not a tile in it.
  expect(chat.x + chat.width).toBeLessThanOrEqual(work.x + 1);
  expect(Math.round(chat.height)).toBe(Math.round((await box(page.locator(".chat-row"))).height));
  expect(chat.height).toBeGreaterThanOrEqual(work.height);
  await expect(column.locator(".visual-tile")).toHaveCount(0);
  await expect(page.locator(".visual-stage .chat-panel")).toHaveCount(0);
  expect(Math.abs(chat.width - 1280 / 3)).toBeLessThan(2);
  await expect(page.locator(".visual-toolbar")).toHaveCount(0);
});

test("on a wide screen Chat is 80 characters of its own font and its lines' padding", async ({ page }) => {
  const { column } = await open(page, { width: 3000, height: 800 });
  const eighty = await column.evaluate((element) => {
    const probe = Object.assign(document.createElement("i"), { style: "display: block; width: 80ch" });
    element.append(probe);
    const { width } = probe.getBoundingClientRect();
    probe.remove();
    return width;
  });
  const shown = (await box(column)).width;
  expect(shown).toBeLessThan(3000 / 3);
  expect(Math.abs(shown - (eighty + 24))).toBeLessThan(2);
});

test("the divider resizes Chat by pointer and by arrow keys, never under 280 px, and the width survives a reload", async ({ page }) => {
  const { column } = await open(page);
  const edge = page.getByRole("separator", { name: "Resize Chat" });
  const before = (await box(column)).width;
  const at = await box(edge);
  // The width goes to the station a moment after the drag; a reload before that would lose it.
  const kept = page.waitForResponse((response) => response.url().endsWith("/layout") && response.ok());
  await page.mouse.move(at.x + at.width / 2, at.y + 300);
  await page.mouse.down();
  await page.mouse.move(at.x + at.width / 2 + 100, at.y + 300, { steps: 6 });
  await page.mouse.up();
  await kept;
  const wider = (await box(column)).width;
  expect(Math.abs(wider - before - 100)).toBeLessThan(3);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1)).toBe(true);

  await page.reload();
  await expect(column.getByRole("region", { name: "Chat" })).toBeVisible();
  expect(Math.abs((await box(column)).width - wider)).toBeLessThan(2);

  await edge.focus();
  await page.keyboard.press("ArrowRight");
  await expect.poll(async () => Math.round((await box(column)).width)).toBe(Math.round(wider) + 16);
  await page.keyboard.press("Shift+ArrowLeft");
  await expect.poll(async () => Math.round((await box(column)).width)).toBe(Math.round(wider) + 16 - 48);

  const now = await box(edge);
  await page.mouse.move(now.x + now.width / 2, now.y + 300);
  await page.mouse.down();
  await page.mouse.move(5, now.y + 300, { steps: 6 });
  await page.mouse.up();
  expect(Math.round((await box(column)).width)).toBe(280);
});

test("a button in Chat's header moves it to the other side of the workbench, and the side is kept", async ({ page }) => {
  const { column, stage } = await open(page);
  await column.getByRole("button", { name: "Move Chat to the right" }).click();
  const chat = await box(column);
  const work = await box(stage);
  expect(chat.x).toBeGreaterThanOrEqual(work.x + work.width - 1);
  const main = await box(page.locator("main.main"));
  // At the main area's right edge, inside its 1 px rule.
  expect(Math.round(chat.x + chat.width)).toBe(Math.round(main.x + main.width) - 1);
  await page.reload();
  await expect(column.getByRole("button", { name: "Move Chat to the left" })).toBeVisible();
  expect((await box(column)).x).toBeGreaterThan(640);
  await column.getByRole("button", { name: "Move Chat to the left" }).click();
  expect((await box(column)).x).toBeLessThan(640);
});

test("the shortcut hides Chat to a strip in the accent colour; the strip shows it, and a reload shows it again", async ({ page }) => {
  const { column, stage } = await open(page);
  const shown = await box(column);
  const strip = page.getByRole("button", { name: "Show Chat" });
  await expect(strip).toHaveCount(0);
  await page.locator(".visual-canvas").click({ position: { x: 4, y: 4 } });
  await page.keyboard.press("Control+Backslash");
  await expect(column).toBeHidden();
  await expect(strip).toBeVisible();
  const thin = await box(strip);
  expect(Math.round(thin.width)).toBe(16);
  expect(Math.round(thin.height)).toBe(Math.round(shown.height));
  // The workbench takes the room Chat left, less the strip.
  expect(Math.round((await box(stage)).width)).toBeGreaterThan(Math.round(shown.width));
  const fill = await strip.evaluate((element) => {
    const probe = document.createElement("i");
    probe.style.background = "var(--accent)";
    document.body.append(probe);
    const accent = getComputedStyle(probe).backgroundColor;
    probe.remove();
    return { strip: getComputedStyle(element).backgroundColor, accent };
  });
  expect(fill.strip).toBe(fill.accent);

  await strip.click();
  await expect(column).toBeVisible();
  await expect(strip).toHaveCount(0);

  await page.keyboard.press("Control+Backslash");
  await expect(column).toBeHidden();
  await page.reload();
  await expect(column).toBeVisible();
});

test("the Hide button in Chat's header folds it to its strip, and Pin stands it beside the page again", async ({ page }) => {
  const { column } = await open(page);
  await column.getByRole("button", { name: "Hide Chat" }).click();
  await expect(column).toBeHidden();
  const strip = page.getByRole("button", { name: "Show Chat" });
  await expect(strip).toBeVisible();
  await strip.hover();
  await column.getByRole("button", { name: "Pin Chat beside the page" }).click();
  await expect(strip).toHaveCount(0);
  await expect(column.getByRole("button", { name: "Hide Chat" })).toBeVisible();
});

test("a workbench of three columns hides Chat, and closing one gives it back", async ({ page, request }) => {
  const issue = (await (await request.post("/api/inquiries/issue", {
    data: { title: `Three columns ${crypto.randomUUID().slice(0, 8)}`, idempotency_key: crypto.randomUUID() },
  })).json()) as { id: string };
  let station = (await (await request.post("/api/workspaces")).json()) as { id: string; revision: number };
  for (const [type, placement] of [["trax.timeline", "left"], ["trax.subgraph", "side"]] as const) {
    const answer = await request.post(`/api/workspaces/${station.id}/operations`, {
      headers: { "Idempotency-Key": crypto.randomUUID() },
      data: { revision: station.revision, operation: { kind: "show", visual_type: type, placement, record_id: issue.id } },
    });
    expect(answer.ok(), await answer.text()).toBe(true);
    station = (await answer.json()) as typeof station;
  }
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.goto(`/app/#/lookup/${issue.id}`);
  const strip = page.getByRole("button", { name: "Show Chat" });
  await expect(strip).toBeVisible();
  await expect(page.locator(".chat-column")).toBeHidden();
  await page.locator(".visual-side-column-left").getByTitle("Dismiss visual").click();
  await expect(page.locator(".chat-column").getByRole("region", { name: "Chat" })).toBeVisible();
  await expect(strip).toHaveCount(0);
});

test("Alt+Enter takes Chat over the whole main area, the workbench kept; Esc and Alt+Enter return it", async ({ page }) => {
  const { column, stage } = await open(page);
  const main = await box(page.locator("main.main"));
  await page.locator(".visual-canvas").click({ position: { x: 4, y: 4 } });
  await page.keyboard.press("Alt+Enter");
  await expect(stage).toBeHidden();
  const full = await box(column);
  // The main area less its rule on each side.
  expect(Math.round(full.width)).toBe(Math.round(main.width) - 2);
  await page.keyboard.press("Escape");
  await expect(stage).toBeVisible();
  expect(Math.round((await box(column)).width)).toBeLessThan(Math.round(main.width) / 2);
  await page.keyboard.press("Alt+Enter");
  await expect(stage).toBeHidden();
  await page.keyboard.press("Alt+Enter");
  await expect(stage).toBeVisible();
});

test("the palette lists Chat's commands with their keys", async ({ page }) => {
  await open(page);
  await page.keyboard.press("Control+k");
  const palette = page.getByRole("dialog");
  await palette.getByRole("combobox").fill("chat");
  await expect(palette.getByText("Hide Chat")).toBeVisible();
  await expect(palette.getByText("Chat full screen")).toBeVisible();
  await expect(palette.getByText("Move Chat to the other side")).toBeVisible();
});

test("an agent that moves the page covers nothing: Chat and the workbench stay where they stood", async ({ page }) => {
  const { column, stage } = await open(page);
  const [chat, work] = [await box(column), await box(stage)];
  await pushNavigate(page, "#/activity");
  await expect(page).toHaveURL(/#\/activity$/);
  await expect(column.getByRole("region", { name: "Chat" })).toBeVisible();
  expect(await box(column)).toEqual(chat);
  expect(await box(stage)).toEqual(work);
});

test("on a phone Chat stands under the page at full width, with no sideways scroll", async ({ page }) => {
  const { column, stage } = await open(page, { width: 390, height: 844 });
  const [chat, work] = [await box(column), await box(stage)];
  expect(Math.round(chat.width)).toBe(390);
  expect(chat.y).toBeGreaterThanOrEqual(work.y + work.height - 1);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1)).toBe(true);
  await expect(column.locator(".station-name")).toBeInViewport();
  await expect(page.getByRole("separator", { name: "Resize Chat" })).toBeHidden();
});
