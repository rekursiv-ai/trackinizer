import type { APIRequestContext, Page, Request } from "@playwright/test";
import { expect, failedResource, test, streamOpened } from "./fixtures";

// Purge from the detail's ⋯ menu against the e2e server. The row is this
// test's own, so no other spec file's rows are touched.

async function createIssue(request: APIRequestContext, fields: object): Promise<string> {
  const response = await request.post("/api/inquiries/issue", { data: { ...fields, idempotency_key: crypto.randomUUID() } });
  expect(response.ok(), await response.text()).toBe(true);
  return (await response.json()).id;
}

/** Open `hash` once the live stream is connected. */
async function openLive(page: Page, hash: string): Promise<void> {
  const subscribed = streamOpened(page);
  await page.goto(`/app/${hash}`);
  await subscribed;
}

test("an owned row: Purge clears its owner by compare-and-set, then purges with the reason; it leaves the list, and an open detail says so", async ({
  page,
  context,
  request,
  allowErrors,
}) => {
  const label = `d5-purge-${crypto.randomUUID().slice(0, 8)}`;
  const id = await createIssue(request, { title: "D5 purge me", owner: "e2e-owner", labels: [label] });
  // The other tab's detail reads the purged row again, and the server answers 404.
  allowErrors(failedResource(`/api/web/get/${id}`, 404));
  // Another tab has the detail open: the stream tells it the row is gone.
  const other = await context.newPage();
  await openLive(other, `#/lookup/${id}`);
  await expect(other.getByRole("heading", { level: 1 })).toHaveText("D5 purge me");

  // The list is loaded first, so going back to it tests that its cached page refetches.
  await openLive(page, "#/list/Issue");
  await page.getByRole("button", { name: /^Filter$/ }).click();
  await page.getByRole("option", { name: "Label", exact: true }).click();
  await page.getByRole("combobox").fill(label);
  await page.keyboard.press("Enter");
  await page.keyboard.press("Escape");
  await expect(page.locator("a.row .row-title")).toHaveText(["D5 purge me"]);
  await page.locator("a.row").click();
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("D5 purge me");

  const writes: Request[] = [];
  page.on("request", (sent) => {
    if (sent.method() !== "GET" && sent.url().includes("/api/")) writes.push(sent);
  });
  await page.getByRole("button", { name: /^Issue#\d+ actions$/ }).click();
  await page.getByRole("option", { name: "Purge…" }).click();
  const dialog = page.getByRole("dialog", { name: /^Purge Issue#\d+$/ });
  await expect(dialog).toContainText("is owned by e2e-owner.");
  await dialog.getByRole("textbox", { name: "Reason" }).fill("D5 e2e: a test row");
  await dialog.getByRole("button", { name: /^Clear owner and purge/ }).click();

  await expect(page.getByRole("heading", { name: "Deleted or purged" })).toBeVisible();
  await expect(page.getByText(/^Purged Issue#\d+$/)).toBeVisible();
  expect(writes.map((write) => `${write.method()} ${new URL(write.url()).pathname.replace(id, "{id}")}`)).toEqual([
    "PUT /api/inquiries/{id}/owner",
    "DELETE /api/inquiries/{id}",
  ]);
  expect(writes.map((write) => write.postDataJSON())).toEqual([
    { value: null, mode: "cas", expected: "e2e-owner", reason: "D5 e2e: a test row" },
    { reason: "D5 e2e: a test row" },
  ]);
  expect((await request.get(`/api/inquiries/${id}`)).status()).toBe(404);
  const purged = await (await request.get(`/api/change_log?subject_id=${id}&kind=purged`)).json();
  expect(purged.map((change: { reason: string }) => change.reason)).toEqual(["D5 e2e: a test row"]);

  await expect(other.getByRole("heading", { name: "Deleted or purged" })).toBeVisible();
  await page.locator("a.crumb-btn").click();
  await expect(page.getByTitle("The same query from the CLI")).toContainText(`labels is ${label}`);
  await expect(page.getByRole("heading", { name: "Nothing here" })).toBeVisible();
});

test("a purge whose answer is lost after it landed is read back and reported purged, not a 404 (K1)", async ({ page, request, allowErrors }) => {
  const id = await createIssue(request, { title: "K1 purge, answer lost" });
  // The purge's answer, which the route drops once the server has applied it,
  // and the reads of the purged row that follow, which the server answers 404.
  allowErrors(failedResource(`/api/inquiries/${id}`, "ERR_FAILED"));
  allowErrors(failedResource(`/api/inquiries/${id}`, 404));
  allowErrors(failedResource(`/api/web/get/${id}`, 404));
  await page.route(`**/api/inquiries/${id}`, async (route) => {
    if (route.request().method() !== "DELETE") return route.fallback();
    await route.fetch();
    await route.abort();
  });
  await openLive(page, `#/lookup/${id}`);
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("K1 purge, answer lost");
  const purges: Request[] = [];
  page.on("request", (sent) => {
    if (sent.method() === "DELETE") purges.push(sent);
  });

  await page.getByRole("button", { name: /^Issue#\d+ actions$/ }).click();
  await page.getByRole("option", { name: "Purge…" }).click();
  const dialog = page.getByRole("dialog", { name: /^Purge Issue#\d+$/ });
  await dialog.getByRole("textbox", { name: "Reason" }).fill("K1 e2e: a test row");
  await dialog.getByRole("button", { name: /^Purge permanently/ }).click();

  await expect(page.getByText(/^Purged Issue#\d+$/)).toBeVisible();
  await expect(page.getByRole("heading", { name: "Deleted or purged" })).toBeVisible();
  await expect(page.getByText("not found")).toHaveCount(0);
  expect(purges).toHaveLength(1);
  expect((await request.get(`/api/inquiries/${id}`)).status()).toBe(404);
});
