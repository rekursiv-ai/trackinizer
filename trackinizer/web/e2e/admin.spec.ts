import type { Page, Request } from "@playwright/test";
import { expect, failedResource, test } from "./fixtures";

// Admin against the e2e server. --no-auth makes every request the synthetic
// admin no-auth@localhost; playwright.config.ts seeds a second user,
// e2e-admin@example.com, whose role this spec changes. Viewers come from
// interception of the profile.

const OTHER = "e2e-admin@example.com";

async function openAdmin(page: Page): Promise<void> {
  await page.goto("/app/#/admin");
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Admin");
}

test("an admin changes a user's role and edits the allowlist", async ({ page, request, allowErrors }) => {
  // Adding an entry the allowlist has answers 409, on purpose, below.
  allowErrors(failedResource("/api/admin/allowlist", 409));
  const users = async () => (await (await request.get("/api/admin/users")).json()).users as { email: string; role: string }[];
  const before = (await users()).find((user) => user.email === OTHER)!.role;
  // A role other than the current one, so a retry of this spec changes it too.
  const role = before === "writer" ? "viewer" : "writer";
  await openAdmin(page);
  const table = page.getByRole("region", { name: "Users" });
  await expect(table.getByRole("row").filter({ hasText: "no-auth@localhost" })).toContainText("you");
  await table.getByRole("combobox", { name: `Role of ${OTHER}` }).selectOption(role);
  await expect(page.getByText(`e2e-admin: role set to ${role}`, { exact: true })).toBeVisible();
  expect((await users()).find((user) => user.email === OTHER)!.role).toBe(role);

  const entry = `e2e-${crypto.randomUUID().slice(0, 8)}@example.com`;
  const allowlist = page.getByRole("region", { name: "Allowlist" });
  const form = allowlist.getByRole("form", { name: "Add to the allowlist" });
  await form.getByRole("textbox", { name: "Email or pattern" }).fill(`  ${entry.toUpperCase()} `);
  await form.getByRole("combobox", { name: "Role for new entry" }).selectOption("viewer");
  await form.getByRole("button", { name: "Add" }).click();
  const row = allowlist.getByRole("row").filter({ hasText: entry });
  await expect(row).toBeVisible();
  await expect(form.getByRole("textbox", { name: "Email or pattern" })).toHaveValue("");

  await form.getByRole("textbox", { name: "Email or pattern" }).fill(entry);
  await form.getByRole("button", { name: "Add" }).click();
  // The server's words for a duplicate, which the unit tests' fake repeats.
  await expect(form.getByRole("alert")).toHaveText("unique constraint violated");

  await row.getByRole("combobox", { name: `Role for ${entry}` }).selectOption("admin");
  await expect(page.getByText(`${entry}: role set to admin`, { exact: true })).toBeVisible();
  const entries = async () => (await (await request.get("/api/admin/allowlist")).json()).entries as { email_or_pattern: string; role: string }[];
  expect((await entries()).find((kept) => kept.email_or_pattern === entry)?.role).toBe("admin");

  await row.getByRole("button", { name: `Remove ${entry}` }).click();
  await page.getByRole("alertdialog", { name: `Remove ${entry}?` }).getByRole("button", { name: "Remove" }).click();
  await expect(row).toHaveCount(0);
  expect((await entries()).some((kept) => kept.email_or_pattern === entry)).toBe(false);
});

test("a viewer sees no Admin entry or command, a link to Admin is refused, and nothing is asked of the admin routes", async ({
  page,
}) => {
  await page.route("**/api/me/profile", async (route) => {
    const response = await route.fetch();
    await route.fulfill({ response, json: { ...(await response.json()), role: "viewer" } });
  });
  const admin: Request[] = [];
  page.on("request", (sent) => {
    if (new URL(sent.url()).pathname.startsWith("/api/admin/")) admin.push(sent);
  });
  await page.goto("/app/#/list/Issue");
  const sidebar = page.getByRole("navigation", { name: "Sidebar" });
  await expect(sidebar.getByText("viewer")).toBeVisible();
  await expect(sidebar.getByRole("link", { name: "Your settings" })).toBeVisible();
  await expect(sidebar.getByRole("link", { name: "Admin" })).toHaveCount(0);

  await page.keyboard.press("ControlOrMeta+k");
  const palette = page.getByRole("dialog", { name: "Command menu" });
  await palette.getByRole("combobox", { name: "Command" }).fill("Go to");
  await expect(palette.getByRole("option", { name: "Go to Your settings" })).toBeVisible();
  await expect(palette.getByRole("option", { name: /Admin/ })).toHaveCount(0);
  await page.keyboard.press("Escape");

  await page.goto("/app/#/admin");
  await expect(page.getByRole("heading", { name: "Admins only" })).toBeVisible();
  expect(admin).toEqual([]);
  // The view refetches the profile as it opens; one still in flight must not
  // outlive the test.
  await page.unrouteAll({ behavior: "ignoreErrors" });
});
