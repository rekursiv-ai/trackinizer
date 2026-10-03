import type { APIRequestContext, Page, Request } from "@playwright/test";
import { expect, failedResource, test } from "./fixtures";

// Editing relations against the e2e server: add through the picker, annotate,
// remove, and supersede with a new inquiry. Each test makes its own rows, with
// titles no other spec uses, since specs share one server.

async function createIssue(request: APIRequestContext, title: string): Promise<{ id: string; seq: number }> {
  const response = await request.post("/api/inquiries/issue", { data: { title, idempotency_key: crypto.randomUUID() } });
  expect(response.ok(), await response.text()).toBe(true);
  const { id } = await response.json();
  return { id, seq: (await (await request.get(`/api/inquiries/${id}`)).json()).seq };
}

/** Open the detail of `id` once the live stream is connected, and collect its writes. */
async function openDetail(page: Page, id: string): Promise<Request[]> {
  const writes: Request[] = [];
  page.on("request", (request) => {
    if (request.method() !== "GET" && request.url().includes("/api/")) writes.push(request);
  });
  const subscribed = page.waitForResponse((response) => response.url().includes("/api/web/subscribe"));
  await page.goto(`/app/#/lookup/${id}`);
  await subscribed;
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  return writes;
}

/** Each write as `METHOD path-after-/api/edges-or-/api` and its body. */
const shapes = (writes: Request[], ids: { [name: string]: string }) =>
  writes.map((write) => {
    let path = new URL(write.url()).pathname;
    for (const [name, id] of Object.entries(ids)) path = path.replaceAll(id, name);
    return [`${write.method()} ${path}`, write.postDataJSON()];
  });

test("a relation is added through the picker, annotated one route each, and removed, leaving its inferred provenance", async ({
  page,
  request,
}) => {
  const run = crypto.randomUUID().slice(0, 8);
  const parent = await createIssue(request, `D3 parent ${run}`);
  const child = await createIssue(request, `D3 child ${run}`);
  const writes = await openDetail(page, child.id);

  await page.getByRole("button", { name: "Add parent" }).click();
  const picker = page.getByRole("dialog", { name: "Add relation" });
  await picker.getByRole("option", { name: /^Narrows…/ }).click();
  await picker.getByRole("combobox").fill(`D3 parent ${run}`);
  await picker.getByRole("option", { name: new RegExp(`D3 parent ${run}`) }).click();
  await page.keyboard.press("ControlOrMeta+Enter");
  await expect(picker).toBeHidden();
  const parents = page.getByRole("region", { name: "Parents", exact: true });
  await expect(parents.locator(".rail-link")).toHaveText(`D3 parent ${run} Issue#${parent.seq}`);
  // The first structural edge between the two inferred the younger produced by the older.
  await expect(parents.locator(".edge-name")).toHaveText(["narrows", "produced_by"]);

  const relation = `narrows Issue#${parent.seq}`;
  const narrows = parents.getByRole("link", { name: relation, exact: true });
  await narrows.hover();
  await page.getByRole("button", { name: `Annotate ${relation}` }).click();
  const panel = page.getByRole("group", { name: `Annotations of ${relation}` });
  await panel.getByRole("textbox", { name: "Note" }).fill("blocks the release");
  await panel.getByRole("button", { name: "Save" }).click();
  await expect(page.getByText("Note set to blocks the release", { exact: true })).toBeVisible();
  await panel.getByRole("button", { name: "Label" }).click();
  await page.getByRole("combobox", { name: "Add a label…" }).fill("d3-label");
  await page.keyboard.press("Enter");
  await expect(panel.getByRole("button", { name: "Remove label d3-label" })).toBeVisible();
  await panel.getByRole("group", { name: "Priority" }).getByRole("button").click();
  await page.getByRole("option", { name: "P1 High" }).click();
  await expect(panel.getByRole("group", { name: "Priority" }).getByRole("button")).toHaveText("P1 High (10)");
  const edge = `/api/edges/${child.id}/narrows/${parent.id}`;
  expect(await (await request.get(edge)).json()).toMatchObject({ note: "blocks the release", labels: ["d3-label"], priority: 10 });
  await panel.getByRole("button", { name: "Done" }).click();

  await page.getByRole("button", { name: `Remove ${relation}` }).click();
  const confirm = page.getByRole("form", { name: `Remove Issue#${child.seq} ${relation}` });
  await expect(confirm.getByRole("note")).toContainText(`Issue#${child.seq} stays produced by Issue#${parent.seq}.`);
  await confirm.getByRole("button", { name: "Remove" }).click();
  await expect(narrows).toBeHidden();
  await expect(parents.locator(".edge-name")).toHaveText(["produced_by"]);
  expect((await request.get(edge)).status()).toBe(404);
  expect((await request.get(`/api/edges/${child.id}/produced_by/${parent.id}`)).ok()).toBe(true);

  const [add, ...rest] = writes;
  expect(add!.headers()["idempotency-key"]).toBeTruthy();
  expect(shapes(writes, { C: child.id, P: parent.id })).toEqual([
    ["POST /api/edges/C/narrows/P", {}],
    ["PUT /api/edges/C/narrows/P/note", { value: "blocks the release" }],
    ["PATCH /api/edges/C/narrows/P/labels", { op: "add", value: "d3-label" }],
    ["PUT /api/edges/C/narrows/P/priority", { value: 10 }],
    ["DELETE /api/edges/C/narrows/P", {}],
  ]);
  expect(new Set(rest.map((write) => write.headers()["idempotency-key"])).size).toBe(rest.length);
});

test("supersede with a new inquiry is one batch, opens the new inquiry, and leaves the old one's status", async ({ page, request }) => {
  const run = crypto.randomUUID().slice(0, 8);
  const old = await createIssue(request, `D3 old ${run}`);
  const writes = await openDetail(page, old.id);

  await page.getByRole("button", { name: `Issue#${old.seq} actions` }).click();
  await page.getByRole("option", { name: /^Supersede with a new inquiry…/ }).click();
  const dialog = page.getByRole("dialog", { name: `Supersede Issue#${old.seq} with a new issue` });
  await expect(dialog.getByRole("textbox", { name: "Title" })).toHaveValue(`D3 old ${run}`);
  await dialog.getByRole("textbox", { name: "Title" }).fill(`D3 new ${run}`);
  await dialog.getByRole("button", { name: /Create and supersede/ }).click();

  await expect(page.getByRole("heading", { level: 1 })).toHaveText(`D3 new ${run}`);
  const created = new URL(page.url()).hash.replace("#/lookup/", "");
  const parents = page.getByRole("region", { name: "Parents", exact: true });
  await expect(parents.locator(".rail-link")).toHaveText(`D3 old ${run} Issue#${old.seq}`);
  await expect(parents.getByRole("link", { name: `supersedes Issue#${old.seq}`, exact: true })).toBeVisible();
  expect(writes.map((write) => `${write.method()} ${new URL(write.url()).pathname}`)).toEqual(["POST /api/inquiries/batch"]);
  const body = writes[0]!.postDataJSON();
  expect(body).toEqual({
    items: [{ kind: "Issue", title: `D3 new ${run}`, idempotency_key: body.items[0].idempotency_key }],
    edges: [{ edge_kind: "supersedes", from_index: 0, to_id: old.id }],
  });
  expect((await request.get(`/api/edges/${created}/supersedes/${old.id}`)).ok()).toBe(true);
  expect((await (await request.get(`/api/inquiries/${old.id}`)).json()).status).toBe("active");
});

test("a relation removed while its answer is lost is read back and done, not removed again (K1)", async ({ page, request, allowErrors }) => {
  const run = crypto.randomUUID().slice(0, 8);
  const parent = await createIssue(request, `K1 parent ${run}`);
  const child = await createIssue(request, `K1 child ${run}`);
  const edge = `/api/edges/${child.id}/narrows/${parent.id}`;
  expect((await request.post(edge, { data: {} })).ok()).toBe(true);
  // The remove's answer, which the route drops once the server has applied it.
  allowErrors(failedResource(edge, "ERR_FAILED"));
  await page.route(`**${edge}`, async (route) => {
    if (route.request().method() !== "DELETE") return route.fallback();
    await route.fetch();
    await route.abort();
  });
  const writes = await openDetail(page, child.id);

  const relation = `narrows Issue#${parent.seq}`;
  const narrows = page.getByRole("region", { name: "Parents", exact: true }).getByRole("link", { name: relation, exact: true });
  await narrows.hover();
  await page.getByRole("button", { name: `Remove ${relation}` }).click();
  await page.getByRole("form", { name: `Remove Issue#${child.seq} ${relation}` }).getByRole("button", { name: "Remove" }).click();

  await expect(page.getByText(`Removed: Issue#${child.seq} ${relation}`, { exact: true })).toBeVisible();
  await expect(narrows).toBeHidden();
  await expect(page.getByRole("alert")).toHaveCount(0);
  expect(shapes(writes, { C: child.id, P: parent.id })).toEqual([["DELETE /api/edges/C/narrows/P", {}]]);
  expect((await request.get(edge)).status()).toBe(404);
});
