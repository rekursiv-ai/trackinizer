import type { Locator, Page } from "@playwright/test";
import { expect, setPreferences, test } from "./fixtures";
import { resetCanvas } from "./canvasState";

// The app's one bar runs along the top, over Chat and the canvas: the logo, Go,
// Search and New, the stations as tab groups each holding its canvas tabs, the
// Workbench's visuals, and the account. There is no sidebar.

test.use({ canvas: true });

test.beforeEach(async ({ request }) => {
  await resetCanvas(request);
});

async function open(page: Page, width = 1280): Promise<{ bar: Locator; chat: Locator; stage: Locator }> {
  await page.setViewportSize({ width, height: 800 });
  await page.goto("/app/#/list/Issue");
  await expect(page.locator(".chat-column").getByRole("region", { name: "Chat" })).toBeVisible();
  return { bar: page.getByRole("banner", { name: "App" }), chat: page.locator(".chat-column"), stage: page.locator(".chat-stage") };
}

async function box(locator: Locator) {
  const found = await locator.boundingBox();
  expect(found).not.toBeNull();
  return found!;
}

/** The name of the station on show, as its group's title holds it. */
async function current(bar: Locator): Promise<string> {
  return (await bar.locator(".station-group.is-current").getAttribute("aria-label"))!;
}

test("the bar spans the top over Chat, at the left, and the canvas; nothing is left of Chat", async ({ page }) => {
  const { bar, chat, stage } = await open(page);
  const top = await box(bar);
  const column = await box(chat);
  const canvas = await box(stage);
  expect(top.y + top.height).toBeLessThanOrEqual(column.y);
  expect(column.x + column.width).toBeLessThanOrEqual(canvas.x + 1);
  expect(column.x).toBeLessThan(16);
  await expect(page.getByRole("navigation", { name: "Sidebar" })).toHaveCount(0);
  await expect(page.locator(".canvas-tabs")).toHaveCount(0);
});

test("the account is its avatar at rest; the pointer on it slides out the email and role, and a click opens its menu", async ({ page }) => {
  const { bar } = await open(page);
  const account = bar.getByRole("button", { name: /^Account: / });
  const reveal = account.locator(".account-reveal");
  await page.mouse.move(640, 500);
  await expect.poll(async () => (await box(reveal)).width).toBeLessThan(2);
  await account.hover();
  await expect.poll(async () => (await box(reveal)).width).toBeGreaterThan(80);
  await expect(reveal.locator(".role-chip")).toBeVisible();
  await account.click();
  const menu = page.getByRole("dialog", { name: "Account" });
  await expect(menu.getByRole("group", { name: "Skin" })).toBeVisible();
  // This server runs with --no-auth: nobody signs in, so the menu offers no other account and no Sign out.
  await expect(menu.locator(".topbar-item .label")).toHaveText(["Your settings", "Admin"]);
  await expect(menu.getByRole("button", { name: "Sign out" })).toHaveCount(0);
  await page.keyboard.press("Escape");
  await expect(menu).toHaveCount(0);
  await expect(account).toBeFocused();
});

test("a station's tabs slide out gradually while the pointer rests on its group, and fold once it leaves the bar", async ({ page }) => {
  const { bar } = await open(page);
  const group = bar.locator(".station-group.is-current");
  const tabs = group.locator(".station-group-tabs");
  await page.mouse.move(640, 500);
  await expect.poll(async () => (await box(tabs)).width).toBe(0);
  await group.locator(".station-group-title").hover();
  // The width springs open over a few frames, not at once.
  const early = (await box(tabs)).width;
  await expect.poll(async () => (await box(tabs)).width).toBeGreaterThan(early);
  const full = await tabs.evaluate((element) => element.firstElementChild!.scrollWidth);
  await expect.poll(async () => Math.round((await box(tabs)).width)).toBe(full);
  await page.mouse.move(640, 500);
  await expect.poll(async () => (await box(tabs)).width).toBe(0);
});

test("a tab of another station opens that station with its own tabs, and the canvas row names it", async ({ page, request }) => {
  const { bar } = await open(page);
  const before = await current(bar);
  // Another station, made with two tabs: the second open is what the list names.
  const made = await (await request.post("/api/workspaces/new")).json() as { id: string; name: string };
  await request.put(`/api/workspaces/${made.id}/layout`, { data: { tabs: [{ id: "a", name: "Graph", page: "#/graph" }, { id: "b", name: "Issues", page: "#/list/Issue" }], tab: "a" } });
  await page.reload();
  const other = bar.getByRole("group", { name: made.name });
  await other.locator(".station-group-title").hover();
  await other.getByRole("button", { name: "Issues", exact: true }).click();
  await expect.poll(() => current(bar)).toBe(made.name);
  await expect(page.getByRole("group", { name: "On show" })).toHaveText(`${made.name}/Issues`);
  await expect(bar.getByRole("group", { name: before }).locator(".station-group-title")).not.toHaveAttribute("aria-current", "true");
});

test("the open station's + makes a tab in its group, and × closes it", async ({ page }) => {
  const { bar } = await open(page);
  const group = bar.locator(".station-group.is-current");
  await group.locator(".station-group-title").click();
  await group.getByRole("button", { name: /^New tab in / }).click();
  await expect(group.getByRole("button", { name: "Tab 2", exact: true })).toBeVisible();
  await expect(page.getByRole("group", { name: "On show" })).toContainText("/Tab 2");
  await group.getByRole("button", { name: "Close Tab 2" }).click();
  await expect(group.getByRole("button", { name: "Tab 2", exact: true })).toHaveCount(0);
});

test("the Workbench menu opens on the canvas's visual toggles, and they work", async ({ page }) => {
  const { bar } = await open(page);
  await bar.getByRole("button", { name: "Workbench" }).click();
  const menu = page.locator(".workbench-menu");
  const browse = menu.getByRole("checkbox", { name: /Browse/ });
  await expect(browse).toBeVisible();
  await expect(browse).toBeChecked();
  await expect(menu.getByRole("textbox", { name: "Artifact link" })).toBeVisible();
  await expect(menu.locator(".workbench-slot :focus")).toHaveCount(1);
});

test("Settings opens from the account menu over the page, beside Chat", async ({ page }) => {
  const { bar, chat } = await open(page);
  await bar.getByRole("button", { name: /^Account: / }).click();
  await page.getByRole("dialog", { name: "Account" }).getByRole("link", { name: "Your settings" }).click();
  const sheet = page.getByRole("complementary", { name: "Your settings" });
  await expect(sheet.getByRole("heading", { level: 1 })).toHaveText("Your settings");
  const column = await box(chat);
  expect((await box(sheet)).x).toBeGreaterThanOrEqual(column.x + column.width);
});

test("a skin chosen in Appearance shows at once, and a reload keeps it, from the user's preferences", async ({ page }) => {
  const { bar } = await open(page);
  try {
    await bar.getByRole("button", { name: /^Account: / }).click();
    await page.getByRole("group", { name: "Skin" }).getByRole("button", { name: "DOS 80s" }).click();
    await expect(page.locator("html")).toHaveAttribute("data-skin", "dos");
    await expect(page.locator("body")).toHaveCSS("background-color", "rgb(0, 0, 0)");
    await page.reload();
    await expect(page.locator("html")).toHaveAttribute("data-skin", "dos");
  } finally {
    await setPreferences(page.request, { skin: null });
  }
});

test("on a phone the bar fits the width, its controls each take a press, and Go reaches every page", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 800 });
  await page.goto("/app/#/list/Issue");
  const bar = page.getByRole("banner", { name: "App" });
  await expect(bar).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  // Every control of the bar takes its own press: none lies under another.
  await bar.getByRole("button", { name: "All stations" }).click();
  await expect(page.getByRole("dialog", { name: "Stations" })).toBeVisible();
  await page.keyboard.press("Escape");
  await bar.getByRole("button", { name: "Go to" }).click();
  const go = page.getByRole("navigation", { name: "Go to" });
  await go.getByRole("link", { name: /^Activity/ }).click();
  await expect(page).toHaveURL(/#\/activity$/);
  await expect(go).toHaveCount(0);
});
