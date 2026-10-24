import type { Page } from "@playwright/test";
import { expect, failedResource, revealBar, setPreferences, test } from "./fixtures";

// Your settings against the e2e server, where --no-auth makes every request the
// synthetic admin no-auth@localhost. Tokens and aliases made here are this
// spec's own, named with a fresh suffix.

const suffix = () => crypto.randomUUID().slice(0, 8);

/** Settings, the sheet over the page. */
const sheet = (page: Page) => page.getByRole("complementary", { name: "Your settings" });

async function openSettings(page: Page): Promise<void> {
  await page.goto("/trax/settings");
  await expect(sheet(page).getByRole("heading", { level: 1 })).toHaveText("Your settings");
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

test("the user's own data follows them to a fresh browser: names, stars, saved views and an unsent draft", async ({ page, browser, request }) => {
  const alias = `e2e-alias-${suffix()}`;
  const made = await request.post("/api/inquiries/issue", { data: { title: "Data follows me", description: "First words.", idempotency_key: crypto.randomUUID() } });
  expect(made.ok(), await made.text()).toBe(true);
  const { id } = (await made.json()) as { id: string };
  // Stars and views have no screens yet; one of each is set as the agent would.
  const view = { id: "e2e-view", name: "E2E view", request: { kinds: ["Issue"], filters: [] } };
  await setPreferences(request, { stars: [id], views: [view] });

  await openSettings(page);
  const names = page.getByRole("region", { name: "Names that mean you" });
  await names.getByRole("textbox", { name: "Name" }).fill(alias);
  await names.getByRole("button", { name: "Add" }).click();
  await expect(names.getByRole("list", { name: "Your names" })).toContainText(alias);

  const kept = page.waitForResponse((response) => response.request().method() === "PATCH" && response.url().endsWith("/api/me/preferences") && (response.request().postData() ?? "").includes("drafts"));
  await page.goto(`/trax/lookup/${id}`);
  await page.getByText("First words.").click();
  await page.getByRole("textbox", { name: "Description" }).pressSequentially(" My draft.");
  expect((await kept).ok()).toBe(true);

  const fresh = await browser.newContext();
  const other = await fresh.newPage();
  const preferences = ((await (await fresh.request.get("/api/me/profile")).json()) as { preferences: { stars: string[]; views: unknown[] } }).preferences;
  expect([preferences.stars, preferences.views]).toEqual([[id], [view]]);
  await other.goto(`/trax/lookup/${id}`);
  await expect(other.getByRole("textbox", { name: "Description" })).toHaveValue("First words. My draft.");
  await openSettings(other);
  await expect(other.getByRole("list", { name: "Your names" })).toContainText(alias);
  await fresh.close();
});

test("Settings opens as half a page over the page, from its left edge, and Close takes the tab back", async ({ page }) => {
  await page.goto("/trax/list/Issue");
  const list = page.getByRole("heading", { level: 1, name: "Issues" });
  await expect(list).toBeVisible();
  await (await revealBar(page)).getByRole("button", { name: /^Account: / }).click();
  await page.getByRole("dialog", { name: "Account" }).getByRole("link", { name: "Your settings" }).click();
  await expect(sheet(page).getByRole("heading", { level: 1 })).toHaveText("Your settings");
  // The list stays under it, mounted, its other half in view.
  await expect(list).toBeAttached();
  const main = (await page.locator("main.main").boundingBox())!;
  const half = (await sheet(page).boundingBox())!;
  // Half the main area inside its 1 px border, and no narrower than 560 px.
  const inner = main.width - 2;
  expect(Math.round(half.width)).toBe(Math.round(Math.min(inner, Math.max(inner / 2, 560))));
  // Against the main area's left edge.
  expect(Math.round(half.x)).toBe(Math.round(main.x + 1));
  // The sections take the sheet's width, so Close stands at their right edge.
  const close = (await page.getByRole("link", { name: "Close settings" }).boundingBox())!;
  const profile = (await page.getByRole("region", { name: "Profile" }).boundingBox())!;
  expect(Math.round(close.x + close.width)).toBe(Math.round(profile.x + profile.width));
  await page.getByRole("link", { name: "Close settings" }).click();
  await expect(page).toHaveURL(/\/trax\/list\/Issue$/);
  await expect(sheet(page)).toHaveCount(0);
});

/** The suite's user's preferences, as the server holds them. */
async function storedPreferences(page: Page): Promise<{ [key: string]: unknown }> {
  return ((await (await page.request.get("/api/me/profile")).json()) as { preferences: { [key: string]: unknown } }).preferences;
}

test("a theme and terminal font size chosen in Settings follow the user to a fresh browser, which keeps neither", async ({ page, browser }) => {
  await openSettings(page);
  const appearance = page.getByRole("region", { name: "Appearance" });
  await appearance.getByRole("combobox", { name: "Theme" }).selectOption("light");
  await appearance.getByRole("combobox", { name: "Terminal font size" }).selectOption("16px");
  await expect.poll(() => storedPreferences(page)).toMatchObject({ theme: "light", terminal_font_size: "16px" });

  const fresh = await browser.newContext();
  const other = await fresh.newPage();
  await other.goto("/trax/graph");
  await expect(other.locator("html")).toHaveAttribute("data-theme", "light");
  expect(await other.evaluate(() => Object.keys(localStorage))).not.toContain("trackinizer.theme");
  await fresh.close();
});

test("what a browser kept before preferences moved to the server is sent once, then forgotten", async ({ page }) => {
  await page.goto("/trax/graph");
  await page.evaluate(() => {
    localStorage.setItem("trackinizer.theme", "light");
    localStorage.setItem("trackinizer.chat.side", "right");
  });
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await expect.poll(() => storedPreferences(page)).toMatchObject({ theme: "light", chat_side: "right" });
  await expect.poll(() => page.evaluate(() => [localStorage.getItem("trackinizer.theme"), localStorage.getItem("trackinizer.chat.side")]))
    .toEqual([null, null]);
});

test("the preferences document saves an edited setting, and the page takes it at once", async ({ page }) => {
  await openSettings(page);
  const doc = page.getByRole("textbox", { name: "Preferences document" });
  await expect(doc).toHaveValue(/"theme": "dark"/);
  await expect(doc).toHaveValue(/\/\/ The app's colours/);
  await doc.fill((await doc.inputValue()).replace('"theme": "dark"', '"theme": "light"'));
  await page.getByRole("region", { name: "Preferences document" }).getByRole("button", { name: "Save" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await expect.poll(() => storedPreferences(page)).toMatchObject({ theme: "light" });
});

test("a browser's old stars, names and drafts are sent once, merged into the user's, and forgotten but for the tiles", async ({ page, request }) => {
  await setPreferences(request, { stars: ["kept-on-server"] });
  await page.goto("/trax/graph");
  await page.evaluate(() => {
    const state = { version: 2, stars: ["old-star"], aliases: ["old-alias"], ui: { tiles: { chat: { collapsed: true, place: null } } } };
    localStorage.setItem(`trackinizer.v2.${location.origin}.no-auth@localhost`, JSON.stringify(state));
    localStorage.setItem("trackinizer.v2.drafts.no-auth@localhost", JSON.stringify({ "i1:title": { text: "Old words", base: "Title" } }));
  });
  await page.reload();
  await expect.poll(() => storedPreferences(page)).toMatchObject({
    stars: ["kept-on-server", "old-star"],
    aliases: ["old-alias"],
    drafts: { "i1:title": { text: "Old words", base: "Title" } },
  });
  await expect.poll(() => page.evaluate(() => Object.keys(localStorage).filter((key) => key.includes("no-auth@localhost")))).toHaveLength(1);
  expect(await page.evaluate(() => JSON.parse(localStorage.getItem(`trackinizer.v2.${location.origin}.no-auth@localhost`)!))).toMatchObject({
    stars: [],
    aliases: [],
    ui: { tiles: { chat: { collapsed: true, place: null } } },
  });
});

// Sign out itself is the unit tests' (src/settings/index.test.tsx): this server
// signs nobody in.
test("under --no-auth, where nobody signs in, settings offer no sign out", async ({ page }) => {
  await openSettings(page);
  await expect(page.getByRole("region", { name: "Profile" })).toContainText("no-auth@localhost");
  await expect(page.getByRole("region", { name: "Session" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Sign out" })).toHaveCount(0);
});
