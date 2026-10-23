import AxeBuilder from "@axe-core/playwright";
import type { Page } from "@playwright/test";
import { mkdirSync } from "node:fs";
import { expect, test } from "./fixtures";

// The top bar's Machines menu, between the pin and the Workbench: the person's own
// machines, how to connect one, and the notes the server's deployment set
// (TRACKINIZER_COMPUTE, which playwright.config.ts gives the e2e server). Each test
// leaves a screenshot for a person to look at.

/** Where the screenshots go; outside the checkout, as every generated file does. */
const SHOTS = "/opt/scratch/artifacts/trax-hosted/ui/machines-menu";

/** Open the bar and the Machines menu on the issue list, at a laptop's size. */
async function openMenu(page: Page) {
  await page.setViewportSize({ width: 1280, height: 800 });
  await page.goto("/app/#/list/Issue");
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  await page.mouse.move(640, 1);
  const bar = page.getByRole("banner", { name: "App" });
  await expect(bar).toHaveClass(/topbar-auto-open/);
  await bar.getByRole("button", { name: /^Machines/ }).click();
  const menu = page.getByRole("dialog", { name: "Machines" });
  await expect(menu.getByText("Your machines")).toBeVisible();
  return menu;
}

test("on this server, without sign-in, the menu says no machine connects and lists the server's notes", async ({ page }) => {
  const menu = await openMenu(page);
  await expect(menu.getByText(/runs without sign-in/)).toBeVisible();
  const provided = menu.getByRole("region", { name: /^Provided by / });
  await expect(provided.getByRole("listitem")).toHaveText([/^GPU jobs/, /^Workspace box/]);
  mkdirSync(SHOTS, { recursive: true });
  await page.screenshot({ path: `${SHOTS}/menu-no-auth.png` });
});

test("signed in, the menu lists your machines, offers Allow, folds the steps, and lists the server's notes", async ({ page }) => {
  // This server runs without sign-in, where no machine connects, so the page is
  // shown a signed-in writer, and the machines a person with two would have.
  await page.route("**/api/me/profile", async (route) => {
    const response = await route.fetch();
    await route.fulfill({ response, json: { ...(await response.json()), email: "ada@example.com", role: "writer" } });
  });
  await page.route("**/api/terminals/machines*", (route) => route.fulfill({ json: {
    machines: [
      { name: "laptop", status: "online", terminal_ready: true, confirmed: true },
      { name: "workstation", status: "online", terminal_ready: true, confirmed: false },
      { name: "old-desk", status: "offline", terminal_ready: false, confirmed: true },
    ],
    terminals: [],
  } }));
  const menu = await openMenu(page);
  await expect(page.getByRole("button", { name: "Machines, 2 online" })).toBeVisible();
  await expect(menu.locator(".machines-row")).toHaveText([/laptop\s*Online/, /workstation\s*Waiting for you to allow it\s*Allow/, /old-desk\s*Offline/]);
  await expect(menu.locator("details.machines-connect")).not.toHaveAttribute("open");
  await menu.getByText("Connect a machine").click();
  await expect(menu.getByText(/keep it connected in tmux or as a user service/)).toBeVisible();
  await expect(menu.getByRole("region", { name: /^Provided by / }).getByRole("listitem")).toHaveText([/^GPU jobs/, /^Workspace box/]);
  const { violations } = await new AxeBuilder({ page }).include(".machines-menu").withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
  expect(violations.map((each) => `${each.id}: ${each.nodes.map((node) => node.target.join(" ")).join(", ")}`)).toEqual([]);
  mkdirSync(SHOTS, { recursive: true });
  await page.screenshot({ path: `${SHOTS}/menu.png` });
});
