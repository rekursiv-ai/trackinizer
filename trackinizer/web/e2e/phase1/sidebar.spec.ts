import { expect, revealBar, test } from "../fixtures";

// Phase 1's manual row starts with "every sidebar entry" and "each kind's list:
// tabs, Filter, Group, Sort". The pages are now the top bar's Go menu, and
// Your settings and Admin are the account menu's. The kinds come from the
// server, so the expected list is read from /api/meta/enums, never written down here.

test("every page in Go opens its view, and each kind's list has its tabs and tools; Settings and Admin open from the account", async ({ page, request }) => {
  const { inquiry_kind_all: kinds } = (await (await request.get("/api/meta/enums")).json()) as {
    inquiry_kind_all: string[];
  };
  await page.goto("/app/#/activity");
  const go = page.getByRole("navigation", { name: "Go to" });
  // Graph, Console, Activity, then one Browse entry per kind in the server's order.
  const hashes = ["#/graph", "#/console", "#/activity", ...kinds.map((kind) => `#/list/${kind}`)];
  await (await revealBar(page)).getByRole("button", { name: "Go to" }).click();
  await expect(go.getByRole("link")).toHaveCount(hashes.length);
  await page.keyboard.press("Escape");

  for (const [n, hash] of hashes.entries()) {
    await (await revealBar(page)).getByRole("button", { name: "Go to" }).click();
    const entry = go.getByRole("link").nth(n);
    const name = (await entry.locator(".label").textContent())!.trim();
    await entry.click();
    await expect(go).toHaveCount(0);
    // The console adds its open view's name: "Console · Untitled view".
    const named = name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    await expect(page.getByRole("heading", { level: 1 })).toHaveText(new RegExp(`^${named}( · .+)?$`));
    expect(new URL(page.url()).hash).toBe(hash);
    await (await revealBar(page)).getByRole("button", { name: "Go to" }).click();
    await expect(go.getByRole("link").nth(n)).toHaveAttribute("aria-current", "page");
    await page.keyboard.press("Escape");
    if (!hash.startsWith("#/list/")) continue;
    const tabs = page.getByRole("navigation", { name: "Status" });
    await expect(tabs.getByRole("button")).toHaveText(["Active", "Closed", "All"]);
    for (const tool of [/^Filter$/, /^Group: /, /^Sort: /]) {
      await expect(page.getByRole("button", { name: tool })).toBeVisible();
    }
    await expect(page.getByTitle("The same query from the CLI")).toHaveText(new RegExp(`^trax ${hash.slice("#/list/".length).toLowerCase()}`));
  }

  // Settings and Admin (the e2e server's user is an admin) open as a sheet over the page.
  for (const [name, hash] of [["Your settings", "#/settings"], ["Admin", "#/admin"]] as const) {
    await (await revealBar(page)).getByRole("button", { name: /^Account: / }).click();
    await page.getByRole("dialog", { name: "Account" }).getByRole("link", { name }).click();
    await expect(page.getByRole("complementary", { name }).getByRole("heading", { level: 1 })).toHaveText(name);
    expect(new URL(page.url()).hash).toBe(hash);
  }
});
