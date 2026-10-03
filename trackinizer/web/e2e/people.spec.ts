import { readFileSync } from "node:fs";
import type { APIRequestContext, Locator, Page, Request } from "@playwright/test";
import { expect, test } from "./fixtures";

// People and agents in owner and subscriber pickers against the e2e server,
// where --no-auth makes every request the admin no-auth@localhost. Each test
// names its people, agents and rows with a fresh suffix, since specs share one
// server; people added by hand live in each browser context's own storage.

const suffix = () => crypto.randomUUID().slice(0, 8);

async function createIssue(request: APIRequestContext, fields: object): Promise<string> {
  const response = await request.post("/api/inquiries/issue", { data: { ...fields, idempotency_key: crypto.randomUUID() } });
  expect(response.ok(), await response.text()).toBe(true);
  return (await response.json()).id;
}

async function stored(request: APIRequestContext, id: string): Promise<{ owner: string | null; subscribers: string[] | null }> {
  return (await request.get(`/api/inquiries/${id}`)).json();
}

async function openDetail(page: Page, id: string): Promise<void> {
  await page.goto(`/app/#/lookup/${id}`);
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
}

const propButton = (page: Page, field: string) =>
  page.getByRole("complementary", { name: "Properties" }).locator(`[data-field="${field}"] button.prop-btn`);

/** Open the picker behind `trigger`, pick `row` ("New person…"), and fill the dialog it opens. */
async function addThrough(page: Page, trigger: Locator, row: string, fields: { [label: string]: string }): Promise<Locator> {
  await trigger.click();
  await page.getByRole("option", { name: row }).click();
  const dialog = page.getByRole("dialog", { name: /^New (owner|subscriber)$/ });
  for (const [label, value] of Object.entries(fields)) await dialog.getByRole("textbox", { name: label }).fill(value);
  return dialog;
}

/** Collect the page's writes from now on. */
function writesOf(page: Page): Request[] {
  const writes: Request[] = [];
  page.on("request", (sent) => {
    if (sent.method() !== "GET" && sent.url().includes("/api/")) writes.push(sent);
  });
  return writes;
}

test("from a detail: a new person owns it by email, a new agent subscribes by handle, and both keep their names after a reload", async ({
  page,
  request,
}) => {
  const run = suffix();
  const email = `g3-${run}@example.com`;
  const handle = `g3-arm-${run}`;
  const id = await createIssue(request, { title: `G3 detail ${run}` });
  await openDetail(page, id);

  const person = await addThrough(page, propButton(page, "owner"), "New person…", { Name: `Grace ${run}`, "Email (optional)": email });
  await person.getByRole("button", { name: /^Add as owner/ }).click();
  await expect(propButton(page, "owner")).toContainText(email);
  expect((await stored(request, id)).owner).toBe(email);

  const agent = await addThrough(page, propButton(page, "subscribers"), "New agent…", { Handle: handle });
  await expect(agent).toContainText("New agent");
  await agent.getByRole("button", { name: /^Add as subscriber/ }).click();
  await expect(propButton(page, "subscribers")).toContainText(handle);
  expect((await stored(request, id)).subscribers).toEqual([handle]);

  await page.reload();
  await propButton(page, "owner").click();
  const grace = page.getByRole("option", { name: new RegExp(`^Grace ${run}`) });
  await expect(grace).toHaveAttribute("aria-selected", "true");
  await expect(grace.locator(".hint")).toHaveText(email);
  await page.keyboard.press("Escape");
  await propButton(page, "subscribers").click();
  await expect(page.getByRole("option", { name: new RegExp(`^${handle}`) }).locator(".hint")).toHaveText("agent");
});

test("from a create form: a new agent as owner and a new person without an email as subscriber, in the one create", async ({ page, request }) => {
  const run = suffix();
  await page.goto("/app/#/new/Issue");
  const form = page.getByRole("dialog", { name: "New issue" });
  await form.getByRole("textbox", { name: "Title" }).fill(`G3 create ${run}`);
  const writes = writesOf(page);

  const agent = await addThrough(page, form.getByRole("button", { name: "Owner: No owner" }), "New agent…", { Handle: `g3-owner-${run}` });
  await agent.getByRole("button", { name: /^Add as owner/ }).click();
  await expect(form.getByRole("button", { name: `Owner: g3-owner-${run}` })).toBeVisible();
  const person = await addThrough(page, form.getByRole("button", { name: "Subscribers: No subscribers" }), "New person…", {
    Name: `Ada ${run}`,
  });
  await person.getByRole("button", { name: /^Add as subscriber/ }).click();
  await expect(form.getByRole("button", { name: `Subscribers: Ada ${run}` })).toBeVisible();
  expect(writes).toEqual([]);

  await form.getByRole("button", { name: /^Create issue/ }).click();
  await expect(page.getByRole("heading", { level: 1 })).toHaveText(`G3 create ${run}`);
  expect(writes).toHaveLength(1);
  const { id } = (await (await writes[0]!.response())!.json()) as { id: string };
  expect(await stored(request, id)).toMatchObject({ owner: `g3-owner-${run}`, subscribers: [`Ada ${run}`] });
});

test("from the bulk bar: a new person owns every selected row, by email", async ({ page, request }) => {
  const run = suffix();
  const label = `g3-bulk-${run}`;
  const ids = [await createIssue(request, { title: `G3 bulk ${run} 1`, labels: [label] }), await createIssue(request, { title: `G3 bulk ${run} 2`, labels: [label] })];
  await page.goto("/app/#/list/Issue");
  await page.getByRole("button", { name: /^Filter$/ }).click();
  await page.getByRole("option", { name: "Label", exact: true }).click();
  await page.getByRole("combobox").fill(label);
  await page.keyboard.press("Enter");
  await page.keyboard.press("Escape");
  await expect(page.locator("a.row .row-title")).toHaveCount(2);
  const checks = page.getByRole("button", { name: /^Select Issue#\d+$/ });
  await checks.first().click();
  await checks.last().click({ modifiers: ["Shift"] });
  const bar = page.getByRole("group", { name: "Bulk actions" });
  await expect(bar).toContainText("2 selected");

  const email = `g3-bulk-${run}@example.com`;
  const dialog = await addThrough(page, bar.getByRole("button", { name: "Owner" }), "New person…", { Name: `Bulk ${run}`, "Email (optional)": email });
  await dialog.getByRole("button", { name: /^Add as owner/ }).click();
  await expect(page.locator(".toast-text", { hasText: `Owner set to ${email}` })).toBeVisible();
  for (const id of ids) expect((await stored(request, id)).owner).toBe(email);
});

test("an admin invites a new person to sign in as a writer; a writer is offered no invite", async ({ page, request }) => {
  const run = suffix();
  const email = `g3-invite-${run}@example.com`;
  const id = await createIssue(request, { title: `G3 invite ${run}` });
  await openDetail(page, id);
  const dialog = await addThrough(page, propButton(page, "owner"), "New person…", { Name: `Invitee ${run}`, "Email (optional)": email });
  await expect(dialog).toContainText("No account yet");
  await dialog.getByRole("checkbox", { name: /^Invite to sign in/ }).check();
  await dialog.getByRole("button", { name: /^Add as owner/ }).click();
  await expect(page.locator(".toast-text", { hasText: `Invited ${email} to sign in as a writer` })).toBeVisible();
  await expect(propButton(page, "owner")).toContainText(email);
  const entries = (await (await request.get("/api/admin/allowlist")).json()).entries as { email_or_pattern: string; role: string }[];
  expect(entries.find((entry) => entry.email_or_pattern === email)?.role).toBe("writer");
  // Leave the shared server's allowlist as it was.
  expect((await request.delete(`/api/admin/allowlist/${encodeURIComponent(email)}`)).ok()).toBe(true);

  await page.route("**/api/me/profile", async (route) => {
    const response = await route.fetch();
    await route.fulfill({ response, json: { ...(await response.json()), role: "writer" } });
  });
  const admin: Request[] = [];
  page.on("request", (sent) => {
    if (new URL(sent.url()).pathname.startsWith("/api/admin/")) admin.push(sent);
  });
  await page.reload();
  const writer = await addThrough(page, propButton(page, "owner"), "New person…", { "Email (optional)": `g3-other-${run}@example.com` });
  await expect(writer.getByRole("button", { name: /^Add as owner/ })).toBeVisible();
  await expect(writer.getByRole("checkbox")).toHaveCount(0);
  expect(admin).toEqual([]);
  await page.unrouteAll({ behavior: "ignoreErrors" });
});

test("people added by hand travel in the export; an import adds them, and one named here keeps this browser's name", async ({
  page,
  browser,
  request,
}) => {
  const run = suffix();
  const email = `g3-export-${run}@example.com`;
  const id = await createIssue(request, { title: `G3 export ${run}` });
  await openDetail(page, id);
  const dialog = await addThrough(page, propButton(page, "owner"), "New person…", { Name: `Exported ${run}`, "Email (optional)": email });
  await dialog.getByRole("button", { name: /^Add as owner/ }).click();
  await expect(propButton(page, "owner")).toContainText(email);
  const agent = await addThrough(page, propButton(page, "subscribers"), "New agent…", { Handle: `g3-exported-${run}` });
  await agent.getByRole("button", { name: /^Add as subscriber/ }).click();
  await expect(propButton(page, "subscribers")).toContainText(`g3-exported-${run}`);
  await page.goto("/app/#/settings");
  const downloading = page.waitForEvent("download");
  await page.getByRole("region", { name: "This browser" }).getByRole("button", { name: "Download JSON" }).click();
  const exported = await (await downloading).path();
  expect(JSON.parse(readFileSync(exported, "utf8")).people).toEqual({
    [email]: { name: `Exported ${run}`, type: "person" },
    [`g3-exported-${run}`]: { name: `g3-exported-${run}`, type: "agent" },
  });

  const fresh = await browser.newContext();
  const other = await fresh.newPage();
  await openDetail(other, id);
  const named = await addThrough(other, propButton(other, "owner"), "New person…", { Name: `Here ${run}`, "Email (optional)": email });
  await named.getByRole("button", { name: /^Add as owner/ }).click();
  await other.goto("/app/#/settings");
  const here = other.getByRole("region", { name: "This browser" });
  const choosing = other.waitForEvent("filechooser");
  await here.getByRole("button", { name: "Import JSON…" }).click();
  await (await choosing).setFiles(exported);
  await expect(other.getByText("Imported 1 person.", { exact: true })).toBeVisible();

  await openDetail(other, id);
  await propButton(other, "owner").click();
  await expect(other.getByRole("option", { name: new RegExp(`^Here ${run}`) })).toBeVisible();
  await expect(other.getByRole("option", { name: new RegExp(`^Exported ${run}`) })).toHaveCount(0);
  await other.keyboard.press("Escape");
  await propButton(other, "subscribers").click();
  await expect(other.getByRole("option", { name: new RegExp(`^g3-exported-${run}`) }).locator(".hint")).toHaveText("agent");
  await fresh.close();
});
