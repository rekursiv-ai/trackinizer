import type { Page } from "@playwright/test";
import { expect, revealBar, test } from "./fixtures";

const heading = (page: Page) => page.getByRole("heading", { level: 1 });
const hash = (page: Page) => new URL(page.url()).hash;

/** The top bar's Go menu, opened; a page's entry in it is named by its label and keys. */
async function go(page: Page) {
  const menu = page.getByRole("navigation", { name: "Go to" });
  if (!(await menu.isVisible())) await (await revealBar(page)).getByRole("button", { name: "Go to" }).click();
  return menu;
}

test("a deep link opens its view, and an old UI link lands on its v2 route", async ({ page }) => {
  await page.goto("/app/#/list/Belief");
  await expect(heading(page)).toHaveText("Beliefs");
  await expect((await go(page)).getByRole("link", { name: "Beliefs" })).toHaveAttribute("aria-current", "page");

  await page.goto("/app/#/recent");
  await expect(heading(page)).toHaveText("Activity");
  expect(hash(page)).toBe("#/activity");
});

test("Back then Forward keeps the URL and the view in agreement", async ({ page }) => {
  await page.goto("/app/#/activity");
  await (await go(page)).getByRole("link", { name: "Issues" }).click();
  await expect(heading(page)).toHaveText("Issues");
  await (await go(page)).getByRole("link", { name: "Papers" }).click();
  await expect(heading(page)).toHaveText("Papers");

  await page.goBack();
  await expect(heading(page)).toHaveText("Issues");
  expect(hash(page)).toBe("#/list/Issue");
  await page.goForward();
  await expect(heading(page)).toHaveText("Papers");
  expect(hash(page)).toBe("#/list/Paper");
});

test("⌘K toggles the palette, and a search link opens the search page, not the palette", async ({ page }) => {
  await page.goto("/app/#/list/Issue");
  await expect(heading(page)).toHaveText("Issues");
  const palette = page.getByRole("dialog", { name: "Command menu" });
  await page.keyboard.press("ControlOrMeta+k");
  await expect(palette).toBeVisible();
  await expect(palette.getByRole("combobox", { name: "Command" })).toBeFocused();
  await page.keyboard.press("ControlOrMeta+k");
  await expect(palette).toBeHidden();

  await page.goto("/app/#/search/what%3F");
  await expect(heading(page)).toHaveText(/^Search: what\? \(\d+\)$/);
  await expect(page.getByRole("searchbox", { name: "Search query" })).toHaveValue("what?");
  await expect(palette).toBeHidden();
});

test("on a phone, Go's pages are out of the keyboard's reach until it opens, and in it once opened (WEB-07)", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto("/app/#/activity");
  await expect(heading(page)).toHaveText("Activity");
  const inGo = () => page.evaluate(() => document.activeElement?.closest(".go-menu") !== null);
  for (let step = 0; step < 12; step++) {
    await page.keyboard.press("Tab");
    expect(await inGo(), `Tab ${step + 1}`).toBe(false);
  }
  const issues = (await go(page)).getByRole("link", { name: "Issues" });
  await expect(issues).toBeVisible();
  await issues.focus();
  expect(await inGo()).toBe(true);
});

test("a bar that comes on its own, such as offline, moves none of the rows (the paused bar's class)", async ({
  page,
  context,
  request,
}) => {
  const seeded = await request.post("/api/inquiries/batch", {
    data: { items: [{ kind: "Issue", title: "Shell bar row", idempotency_key: "5e11ba70-0000-4000-8000-000000000001" }], edges: [] },
  });
  expect(seeded.ok(), await seeded.text()).toBe(true);
  await page.goto("/app/#/list/Issue");
  const first = page.locator("a.row").first();
  await expect(first).toBeVisible();
  const before = await first.boundingBox();
  await context.setOffline(true);
  await expect(page.locator(".bar.offline")).toBeVisible();
  expect(await first.boundingBox()).toEqual(before);
  await context.setOffline(false);
  await expect(page.locator(".bar.offline")).toHaveCount(0);
});
