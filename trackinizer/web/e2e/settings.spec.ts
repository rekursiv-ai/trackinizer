import { readFileSync } from "node:fs";
import type { Page } from "@playwright/test";
import { expect, failedResource, test } from "./fixtures";

// Your settings against the e2e server, where --no-auth makes every request the
// synthetic admin no-auth@localhost. Tokens and aliases made here are this
// spec's own, named with a fresh suffix.

const suffix = () => crypto.randomUUID().slice(0, 8);

async function openSettings(page: Page): Promise<void> {
  await page.goto("/app/#/settings");
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Your settings");
}

type Listed = { name: string; prefix: string; role: string; revoked_at: string | null };

test("create a token, see its secret once, and revoke it", async ({ page, request }) => {
  const label = `e2e-token-${suffix()}`;
  // --no-auth answers every request as its admin, bearer or not, so the token
  // is checked through the list route rather than by signing in with it.
  const listed = async () => ((await (await request.get("/api/me/tokens")).json()).tokens as Listed[]).find((token) => token.name === label);
  await openSettings(page);
  const tokens = page.getByRole("region", { name: "API tokens" });
  await tokens.getByRole("button", { name: "New token" }).click();
  const form = tokens.getByRole("form", { name: "New token" });
  await form.getByRole("textbox", { name: "Label" }).fill(label);
  await form.getByRole("combobox", { name: "Role" }).selectOption("viewer");
  await form.getByRole("button", { name: "Create token" }).click();

  const shown = tokens.getByRole("status").filter({ hasText: "won't be shown again" });
  const secret = (await shown.locator("code").textContent())!;
  expect(secret.length).toBeGreaterThan(20);
  const row = tokens.getByRole("row").filter({ hasText: label });
  await expect(row).toContainText("viewer");
  await expect(row).not.toContainText(secret);

  // The server holds it with the role picked, known by the secret's prefix.
  const made = await listed();
  expect(made).toMatchObject({ role: "viewer", revoked_at: null });
  expect(secret.startsWith(made!.prefix)).toBe(true);

  await shown.getByRole("button", { name: "Done" }).click();
  await expect(tokens.getByText(secret)).toHaveCount(0);
  await page.reload();
  await expect(tokens.getByRole("row").filter({ hasText: label })).toBeVisible();
  expect(await page.content()).not.toContain(secret);

  await row.getByRole("button", { name: `Revoke ${label}` }).click();
  const confirm = page.getByRole("alertdialog", { name: `Revoke “${label}”?` });
  await confirm.getByRole("button", { name: "Revoke" }).click();
  await expect(confirm).toBeHidden();
  await expect(row).toContainText("revoked");
  await expect(row.getByRole("button", { name: `Revoke ${label}` })).toHaveCount(0);
  expect((await listed())?.revoked_at).not.toBeNull();
});

test("a create whose answer is lost is not sent again, and the list shows the token it made", async ({ page, request, allowErrors }) => {
  // The answer the route drops below.
  allowErrors(failedResource("/api/me/tokens", "ERR_CONNECTION_RESET"));
  const label = `e2e-lost-${suffix()}`;
  let posts = 0;
  await page.route("**/api/me/tokens", async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    posts += 1;
    // The server makes the token; its answer never reaches the page.
    await route.fetch();
    await route.abort("connectionreset");
  });
  await openSettings(page);
  const tokens = page.getByRole("region", { name: "API tokens" });
  await tokens.getByRole("button", { name: "New token" }).click();
  const form = tokens.getByRole("form", { name: "New token" });
  await form.getByRole("textbox", { name: "Label" }).fill(label);
  await form.getByRole("button", { name: "Create token" }).click();
  await expect(form.getByRole("alert")).toContainText("may have been made anyway");
  await expect(form.getByRole("button", { name: "Retry" })).toHaveCount(0);
  await expect(tokens.getByRole("row").filter({ hasText: label })).toBeVisible();
  expect(posts).toBe(1);

  const made = ((await (await request.get("/api/me/tokens")).json()).tokens as (Listed & { id: string })[]).find((token) => token.name === label)!;
  expect((await request.post(`/api/me/tokens/${made.id}/revoke`)).ok()).toBe(true);
});

test("export this browser's state, then import it into a fresh browser", async ({ page, browser }) => {
  const alias = `e2e-alias-${suffix()}`;
  await openSettings(page);
  // Stars and views have no screens yet; one of each is stored as they will be.
  await page.evaluate(() => {
    const key = `trackinizer.v2.${location.origin}.no-auth@localhost`;
    const view = { id: "e2e-view", name: "E2E view", request: { kinds: ["Issue"], filters: [] } };
    const state = JSON.parse(localStorage.getItem(key) ?? '{"version":1}');
    localStorage.setItem(key, JSON.stringify({ ...state, version: 1, stars: ["e2e-star"], views: [view] }));
  });
  await page.reload();
  const names = page.getByRole("region", { name: "Names that mean you" });
  await names.getByRole("textbox", { name: "Name" }).fill(alias);
  await names.getByRole("button", { name: "Add" }).click();
  await expect(names.getByRole("list", { name: "Your names" })).toContainText(alias);

  const downloading = page.waitForEvent("download");
  await page.getByRole("region", { name: "This browser" }).getByRole("button", { name: "Download JSON" }).click();
  const exported = await (await downloading).path();
  expect(JSON.parse(readFileSync(exported, "utf8"))).toMatchObject({ version: 1, stars: ["e2e-star"], aliases: [alias] });

  const fresh = await browser.newContext();
  const other = await fresh.newPage();
  await openSettings(other);
  const here = other.getByRole("region", { name: "This browser" });
  await expect(here.getByText("Stars").locator("xpath=..")).toContainText("0");
  const choosing = other.waitForEvent("filechooser");
  await here.getByRole("button", { name: "Import JSON…" }).click();
  await (await choosing).setFiles(exported);
  await expect(other.getByText("Imported 1 star, 1 saved view and 1 name.", { exact: true })).toBeVisible();
  await expect(here.getByText("Stars").locator("xpath=..")).toContainText("1");
  await expect(other.getByRole("list", { name: "Your names" })).toContainText(alias);
  await fresh.close();
});

// Sign out itself is the unit tests' (src/settings/index.test.tsx): this server
// signs nobody in.
test("under --no-auth, where nobody signs in, settings offer no sign out", async ({ page }) => {
  await openSettings(page);
  await expect(page.getByRole("region", { name: "Profile" })).toContainText("no-auth@localhost");
  await expect(page.getByRole("region", { name: "Session" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Sign out" })).toHaveCount(0);
});
