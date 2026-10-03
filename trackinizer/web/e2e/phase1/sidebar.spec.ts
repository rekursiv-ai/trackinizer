import { expect, test } from "../fixtures";

// Phase 1's manual row starts with "every sidebar entry" and "each kind's list:
// tabs, Filter, Group, Sort". The entries come from the server's kinds, so the
// expected list is read from /api/meta/enums, never written down here.

test("every sidebar entry opens its view, and each kind's list has its tabs and tools", async ({ page, request }) => {
  const { inquiry_kind_all: kinds } = (await (await request.get("/api/meta/enums")).json()) as {
    inquiry_kind_all: string[];
  };
  await page.goto("/app/#/activity");
  const sidebar = page.getByRole("navigation", { name: "Sidebar" });
  const entries = sidebar.locator("a.nav-item");
  // Graph, Console, Activity, one Browse entry per kind in the server's order,
  // then Your settings and, for the e2e server's admin, Admin.
  const hashes = ["#/graph", "#/console", "#/activity", ...kinds.map((kind) => `#/list/${kind}`), "#/settings", "#/admin"];
  await expect(entries).toHaveCount(hashes.length);

  for (const [n, hash] of hashes.entries()) {
    const entry = entries.nth(n);
    const name = (await entry.textContent())!.trim();
    await entry.click();
    // The console adds its open view's name: "Console · Untitled view".
    const named = name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    await expect(page.getByRole("heading", { level: 1 })).toHaveText(new RegExp(`^${named}( · .+)?$`));
    await expect(entry).toHaveAttribute("aria-current", "page");
    expect(new URL(page.url()).hash).toBe(hash);
    if (!hash.startsWith("#/list/")) continue;
    const tabs = page.getByRole("navigation", { name: "Status" });
    await expect(tabs.getByRole("button")).toHaveText(["Active", "Closed", "All"]);
    for (const tool of [/^Filter$/, /^Group: /, /^Sort: /]) {
      await expect(page.getByRole("button", { name: tool })).toBeVisible();
    }
    await expect(page.getByTitle("The same query from the CLI")).toHaveText(new RegExp(`^trax ${hash.slice("#/list/".length).toLowerCase()}`));
  }
});
