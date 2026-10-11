import type { Locator, Page } from "@playwright/test";
import { expect, test } from "./fixtures";
import { resetCanvas } from "./canvasState";

// The navigation sidebar stands opposite Chat as a rail of icons. Hovering the
// rail opens it over the workbench, a pin (⌘B) opens it for good, and the
// canvas's controls are its Workbench section, in place of the canvas's old
// toolbar row.

test.use({ canvas: true });

test.beforeEach(async ({ request }) => {
  await resetCanvas(request);
});

async function open(page: Page): Promise<{ sidebar: Locator; app: Locator; main: Locator }> {
  await page.setViewportSize({ width: 1280, height: 800 });
  await page.goto("/app/#/list/Issue");
  await expect(page.locator(".chat-column").getByRole("region", { name: "Chat" })).toBeVisible();
  return { sidebar: page.getByRole("navigation", { name: "Sidebar" }), app: page.locator(".app"), main: page.locator("main.main") };
}

async function box(locator: Locator) {
  const found = await locator.boundingBox();
  expect(found).not.toBeNull();
  return found!;
}

async function forget(page: Page): Promise<void> {
  await page.evaluate(() => localStorage.removeItem("trackinizer.chat.side"));
}

test("the sidebar starts as a rail at the right, opposite Chat, and the canvas has no toolbar row", async ({ page }) => {
  const { sidebar, app, main } = await open(page);
  await expect(app).toHaveClass(/sidebar-collapsed/);
  await expect(app).toHaveClass(/sidebar-right/);
  const rail = await box(sidebar);
  const work = await box(main);
  expect(Math.round(rail.width)).toBe(48);
  expect(rail.x).toBeGreaterThanOrEqual(work.x + work.width - 1);
  expect((await box(page.locator(".chat-column"))).x).toBeLessThan(work.x + work.width / 2);
  await expect(page.locator(".visual-toolbar")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Configure" })).toHaveCount(0);
  await expect(sidebar.getByRole("link", { name: "Issues" })).toHaveAttribute("title", "Issues");
});

test("hovering the rail opens the sidebar over the workbench, which keeps its width; leaving folds it", async ({ page }) => {
  const { sidebar, app, main } = await open(page);
  const before = await box(main);
  await sidebar.hover();
  await expect(app).toHaveClass(/sidebar-peek/);
  const wide = await box(sidebar);
  expect(Math.round(wide.width)).toBe(244);
  await expect(sidebar.getByRole("link", { name: "Issues" }).locator(".label")).toBeVisible();
  expect(await box(main)).toEqual(before);
  // Over the page: the pointer on the sidebar's left part is on the workbench's content, not under it.
  await page.mouse.move(wide.x + 10, wide.y + 300);
  expect(await page.evaluate(([x, y]) => document.elementFromPoint(x!, y!)?.closest("nav.sidebar") !== null, [wide.x + 10, wide.y + 300])).toBe(true);
  await page.mouse.move(300, 400);
  await expect(app).not.toHaveClass(/sidebar-peek/);
  expect(Math.round((await box(sidebar)).width)).toBe(48);
});

test("the pin button, or Ctrl+B, opens the sidebar for good and takes room from the workbench; again, a rail", async ({ page }) => {
  const { sidebar, app, main } = await open(page);
  const rail = await box(main);
  await sidebar.getByRole("button", { name: "Expand sidebar" }).click();
  await page.mouse.move(300, 400);
  await expect(app).not.toHaveClass(/sidebar-collapsed/);
  expect((await box(main)).width).toBeLessThan(rail.width - 150);
  await page.keyboard.press("Control+b");
  await expect(app).toHaveClass(/sidebar-collapsed/);
  expect((await box(main)).width).toBeGreaterThan(rail.width - 2);
});

test("flipping Chat's side moves the sidebar to the other", async ({ page }) => {
  const { sidebar, main } = await open(page);
  await page.locator(".chat-column").getByRole("button", { name: "Move Chat to the right" }).click();
  const work = await box(main);
  expect((await box(sidebar)).x).toBeLessThan(work.x);
  await expect(page.locator(".app")).not.toHaveClass(/sidebar-right/);
  await forget(page);
});

/** The middle of `locator`, in whole pixels. */
async function middle(locator: Locator): Promise<[number, number]> {
  const { x, y, width, height } = await box(locator);
  return [Math.round(x + width / 2), Math.round(y + height / 2)];
}

for (const side of ["right", "left"] as const) {
  test(`at the ${side}, the pin button stays put as the rail opens and is pinned, over Search and then New, each a row, above Graph`, async ({ page }) => {
    const { sidebar, app } = await open(page);
    if (side === "left") await page.locator(".chat-column").getByRole("button", { name: "Move Chat to the right" }).click();
    const pin = sidebar.locator(".panel-toggle");
    const rail = await middle(pin);
    await sidebar.hover();
    await expect(app).toHaveClass(/sidebar-peek/);
    expect(await middle(pin)).toEqual(rail);
    await pin.click();
    await expect(app).not.toHaveClass(/sidebar-collapsed/);
    expect(await middle(pin)).toEqual(rail);
    // Search, then New on a row of its own under it, then Graph; each as wide as the other.
    const search = await box(sidebar.getByRole("button", { name: "Search" }));
    const create = await box(sidebar.getByRole("button", { name: /^New / }));
    const header = await box(pin);
    expect(search.y).toBeGreaterThanOrEqual(header.y + header.height);
    expect(create.y).toBeGreaterThanOrEqual(search.y + search.height);
    expect(Math.round(create.width)).toBe(Math.round(search.width));
    await expect(sidebar.getByRole("button", { name: /^New / })).toContainText("New issue");
    expect((await box(sidebar.getByRole("link", { name: "Graph" }))).y).toBeGreaterThanOrEqual(create.y + create.height);
    await forget(page);
  });
}

/** Where each entry's icon stands, by entry: its middle, in whole pixels. Hidden entries are left out. */
async function iconPlaces(sidebar: Locator): Promise<Record<string, [number, number]>> {
  return sidebar.evaluate((nav) => {
    const places: Record<string, [number, number]> = {};
    for (const entry of nav.querySelectorAll<HTMLElement>("a.nav-item, .sidebar-search, .sidebar-new, .panel-toggle, .theme-btn, .me-row")) {
      const icon = entry.querySelector<HTMLElement>("svg, .avatar");
      const box = icon?.getBoundingClientRect();
      if (!box || box.width === 0) continue;
      const name = entry.classList.contains("panel-toggle") ? "pin" : entry.getAttribute("aria-label") ?? entry.textContent!.trim();
      places[name] = [Math.round(box.x + box.width / 2), Math.round(box.y + box.height / 2)];
    }
    return places;
  });
}

for (const side of ["right", "left"] as const) {
  test(`at the ${side}, every icon stands where it stood in the rail, the rail opened or pinned`, async ({ page }) => {
    const { sidebar, app } = await open(page);
    if (side === "left") await page.locator(".chat-column").getByRole("button", { name: "Move Chat to the right" }).click();
    const rail = await iconPlaces(sidebar);
    expect(Object.keys(rail)).toEqual(expect.arrayContaining(["pin", "Search", "New issue", "Graph", "Issues", "Your settings", "Admin", "Signed in"]));
    await sidebar.hover();
    await expect(app).toHaveClass(/sidebar-peek/);
    expect(await iconPlaces(sidebar)).toEqual(rail);
    await sidebar.locator(".panel-toggle").click();
    await page.mouse.move(700, 400);
    await expect(app).not.toHaveClass(/sidebar-collapsed/);
    expect(await iconPlaces(sidebar)).toEqual(rail);
    await forget(page);
  });
}

test("Settings and Admin stand in the sidebar's foot, over the signed-in user", async ({ page }) => {
  const { sidebar } = await open(page);
  const foot = sidebar.locator(".sidebar-foot");
  await expect(foot.getByRole("link", { name: "Your settings" })).toBeVisible();
  await expect(foot.getByRole("link", { name: "Admin" })).toBeVisible();
  const admin = await box(foot.getByRole("link", { name: "Admin" }));
  expect((await box(foot.getByRole("group", { name: "Signed in" }))).y).toBeGreaterThanOrEqual(admin.y + admin.height);
});

test("pinned open, the Workbench section starts folded, and its heading unfolds it", async ({ page }) => {
  const { sidebar, app } = await open(page);
  await sidebar.getByRole("button", { name: "Expand sidebar" }).click();
  await page.mouse.move(300, 400);
  await expect(app).not.toHaveClass(/sidebar-collapsed/);
  const heading = sidebar.locator(".nav-section-h", { hasText: "Workbench" });
  await expect(heading).toHaveAttribute("aria-expanded", "false");
  await expect(sidebar.getByRole("checkbox", { name: /Browse/ })).toBeHidden();
  await heading.click();
  await expect(heading).toHaveAttribute("aria-expanded", "true");
  await expect(sidebar.getByRole("checkbox", { name: /Browse/ })).toBeVisible();
});

test("the rail shows one Workbench icon that opens the sidebar on the visual toggles; there they work", async ({ page }) => {
  const { sidebar, app } = await open(page);
  const icon = sidebar.getByRole("button", { name: "Workbench", exact: true }).and(page.locator(".workbench-rail"));
  await expect(icon).toBeVisible();
  await expect(sidebar.locator(".workbench-slot")).toBeHidden();
  await icon.click();
  await expect(app).toHaveClass(/sidebar-peek/);
  const browse = sidebar.getByRole("checkbox", { name: /Browse/ });
  await expect(browse).toBeVisible();
  await expect(browse).toBeDisabled();
  await expect(sidebar.getByRole("textbox", { name: "Artifact link" })).toBeVisible();
  await expect(sidebar.locator(".workbench-slot :focus")).toHaveCount(1);
});

test("Settings has no canvas, so no Workbench section", async ({ page }) => {
  const { sidebar } = await open(page);
  await page.goto("/app/#/settings");
  await expect(page.getByRole("heading", { name: "Your settings" })).toBeVisible();
  await expect(sidebar.locator(".workbench-rail")).toBeHidden();
  await expect(sidebar.getByRole("button", { name: "Workbench" })).toHaveCount(0);
});
