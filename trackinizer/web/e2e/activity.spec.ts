import type { APIRequestContext, Locator, Page } from "@playwright/test";
import { expect, test } from "./fixtures";

// The feed is every change on the server, which other spec files write to in
// parallel: one run put over 1,000 of theirs ahead of a line this file wrote in
// its setup. So each check writes its change with its tab already open and the
// stream connected, and finds its line where live changes join, at the top, by
// a title made unique here. It never pages through, or counts, others' changes.
const TAG = crypto.randomUUID().slice(0, 8);

/** Open Activity and wait for the live stream, so changes written next join the feed. */
async function openActivity(page: Page) {
  const subscribed = page.waitForResponse((response) => response.url().includes("/api/web/subscribe"));
  await page.goto("/app/#/activity");
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Activity");
  await subscribed;
}

/** Open a tab of the feed. */
async function openTab(page: Page, name: string) {
  await page.getByRole("navigation", { name: "Change kind" }).getByRole("button", { name, exact: true }).click();
  await expect(page.getByRole("button", { name, exact: true })).toHaveAttribute("aria-current", "page");
}

/** The feed's one line containing `text`. A line's title shows once its inquiry is looked up. */
async function line(page: Page, text: string): Promise<Locator> {
  const found = page.locator(".feed-row").filter({ hasText: text });
  await expect(found).toHaveCount(1);
  return found;
}

/** Create inquiries titled `titles` (kind first, then title), one request; returns their ids in order. */
async function create(request: APIRequestContext, ...titles: [string, string][]): Promise<string[]> {
  const items = titles.map(([kind, title]) => ({ kind, title, idempotency_key: crypto.randomUUID() }));
  const created = await request.post("/api/inquiries/batch", { data: { items, edges: [] } });
  expect(created.ok(), await created.text()).toBe(true);
  return (await created.json()).ids;
}

async function write(response: Promise<{ ok(): boolean; text(): Promise<string> }>) {
  const answer = await response;
  expect(answer.ok(), await answer.text()).toBe(true);
}

test("each tab shows its kind of change as it happens, with its inquiry and reason", async ({ page, request }) => {
  const [child, parent, belief] = [`Activity child ${TAG}`, `Activity parent ${TAG}`, `Activity belief ${TAG}`];
  await openActivity(page);

  await openTab(page, "Created");
  const [childId, parentId, beliefId] = await create(request, ["Issue", child], ["Issue", parent], ["Belief", belief]);
  await expect(await line(page, belief)).toContainText("created the belief on Belief#");

  await openTab(page, "Status");
  await write(request.put(`/api/inquiries/${childId}/status`, { data: { value: "complete", reason: `Done in ${TAG}.` } }));
  const status = await line(page, child);
  await expect(status).toContainText("changed status from active to complete on Issue#");
  await expect(status.locator("blockquote")).toHaveText(`Done in ${TAG}.`);

  await openTab(page, "Judgements");
  await write(request.put(`/api/belief/${beliefId}/judgement`, { data: { value: "proven", reason: "Three reruns agree." } }));
  await expect(await line(page, belief)).toContainText("marked as Proven on Belief#");

  await openTab(page, "Edits");
  await write(request.put(`/api/inquiries/${parentId}/description`, { data: { value: "Now with a description." } }));
  await expect(await line(page, parent)).toContainText("added a description on Issue#");

  // The edge is written on both ends; the feed shows it once, as the child states it.
  await openTab(page, "Relations");
  await write(request.post(`/api/edges/${childId}/requires/${parentId}`, { data: {} }));
  await expect(await line(page, child)).toContainText(/added relation Requires Issue#\d+ on Issue#\d+/);
  await expect(
    page.locator(".feed-row").filter({ hasText: parent }).filter({ hasText: /added (relation Requires|a requires relation)/ }),
  ).toHaveCount(0);
});

test("title, priority, label and owner edits, a removed relation and a purge each show in their tab as they happen (PA2)", async ({
  page,
  request,
}) => {
  const [child, parent, doomed] = [`Activity edited ${TAG}`, `Activity edited parent ${TAG}`, `Activity purged ${TAG}`];
  const [childId, parentId, doomedId] = await create(request, ["Issue", child], ["Issue", parent], ["Issue", doomed]);
  await write(request.post(`/api/edges/${childId}/requires/${parentId}`, { data: {} }));
  await openActivity(page);
  const lineWith = async (phrase: string | RegExp, title: string) =>
    expect(page.locator(".feed-row").filter({ hasText: title }).filter({ hasText: phrase })).toHaveCount(1);

  await openTab(page, "Edits");
  await write(request.put(`/api/inquiries/${childId}/title`, { data: { value: `${child} renamed` } }));
  await lineWith("changed the title on Issue#", `${child} renamed`);
  await write(request.put(`/api/issue/${childId}/priority`, { data: { value: 10 } }));
  await lineWith("set priority to 10 on Issue#", child);
  await write(request.patch(`/api/inquiries/${childId}/labels`, { data: { op: "add", value: `pa2-${TAG}` } }));
  await lineWith(`added label pa2-${TAG} on Issue#`, child);
  await write(request.put(`/api/inquiries/${childId}/owner`, { data: { value: "bo@example.com" } }));
  await lineWith("set the owner to bo@example.com on Issue#", child);

  await openTab(page, "Relations");
  await write(request.delete(`/api/edges/${childId}/requires/${parentId}`, { data: {} }));
  await lineWith(/removed relation Requires Issue#\d+ on Issue#/, child);

  await openTab(page, "Created");
  await write(request.delete(`/api/inquiries/${doomedId}`, { data: { reason: `Purged in ${TAG}.` } }));
  const purge = page.locator(".feed-row").filter({ hasText: `Purged in ${TAG}.` });
  await expect(purge).toContainText("purged the issue on Issue");
});

test("All merges the tabs, and a line opens its inquiry; Back returns to the tab", async ({ page, request }) => {
  const title = `Activity all ${TAG}`;
  await openActivity(page);
  await openTab(page, "All");
  const [id] = await create(request, ["Issue", title]);
  await write(request.put(`/api/inquiries/${id}/status`, { data: { value: "complete", reason: `All done in ${TAG}.` } }));
  const status = await line(page, `All done in ${TAG}.`);
  await status.getByRole("link", { name: /^Issue#\d+$/ }).click();
  await expect(page.getByRole("heading", { level: 1 })).toHaveText(title);
  await page.goBack();
  await expect(page.getByRole("button", { name: "All", exact: true })).toHaveAttribute("aria-current", "page");
  await line(page, `All done in ${TAG}.`);
});
