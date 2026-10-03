import type { APIRequestContext, Locator, Page, Request } from "@playwright/test";
import { expect, test } from "./fixtures";

// The create form against the e2e server: a create with relations is one
// request, and Create more keeps the form open. Each test makes its own rows,
// with titles no other spec uses, since specs share one server.

async function createIssue(request: APIRequestContext, title: string): Promise<{ id: string; seq: number }> {
  const response = await request.post("/api/inquiries/issue", { data: { title, idempotency_key: crypto.randomUUID() } });
  expect(response.ok(), await response.text()).toBe(true);
  const { id } = await response.json();
  return { id, seq: (await (await request.get(`/api/inquiries/${id}`)).json()).seq };
}

/** Collect the page's writes from now on. */
function writesOf(page: Page): Request[] {
  const writes: Request[] = [];
  page.on("request", (request) => {
    if (request.method() !== "GET" && request.url().includes("/api/")) writes.push(request);
  });
  return writes;
}

/** Add `relation` to `target`, found by what `search` types, through the form's picker. */
async function addRelation(page: Page, form: Locator, relation: string, search: string, target: RegExp) {
  await form.getByRole("button", { name: "Add relation" }).click();
  await page.getByRole("option", { name: new RegExp(`^${relation}…`) }).click();
  await page.getByRole("combobox", { name: `${relation}: search inquiries` }).fill(search);
  await page.getByRole("option", { name: target }).click();
  await page.keyboard.press("Escape");
  // Focus goes back to the button that opened the picker, still inside the form.
  await expect(form.getByRole("button", { name: "Add relation" })).toBeFocused();
}

test("a create with relations is one request, opened from C on the list, and opens the new inquiry", async ({ page, request }) => {
  const run = crypto.randomUUID().slice(0, 8);
  const parent = await createIssue(request, `D4 parent ${run}`);
  const blocker = await createIssue(request, `D4 blocker ${run}`);
  await page.goto("/app/#/list/Issue");
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Issues");
  const writes = writesOf(page);

  await page.keyboard.press("c");
  const form = page.getByRole("dialog", { name: "New issue" });
  await expect(page).toHaveURL(/#\/new\/Issue$/);
  const title = form.getByRole("textbox", { name: "Title" });
  await expect(title).toBeFocused();
  await title.fill(`D4 child ${run}`);
  // Enter in a one-line field does not create; only ⌘↵ and the button do.
  await title.press("Enter");
  await form.getByRole("textbox", { name: "Done when" }).fill("Both land together");
  await addRelation(page, form, "Narrows", `D4 parent ${run}`, new RegExp(`D4 parent ${run}`));
  await addRelation(page, form, "Requires", `Issue#${blocker.seq}`, new RegExp(`^Issue#${blocker.seq}`));
  await expect(form.getByRole("list", { name: "Relations" }).getByRole("listitem")).toHaveText([
    `NarrowsIssue#${parent.seq}D4 parent ${run}`,
    `RequiresIssue#${blocker.seq}D4 blocker ${run}`,
  ]);
  expect(writes).toEqual([]);
  await form.getByRole("button", { name: /^Create issue/ }).click();

  await expect(page.getByRole("heading", { level: 1 })).toHaveText(`D4 child ${run}`);
  const created = new URL(page.url()).hash.replace("#/lookup/", "");
  const narrows = page.getByRole("region", { name: "Parents", exact: true }).locator(".rail-peer", { hasText: `D4 parent ${run}` });
  await expect(narrows.locator(".rail-link")).toHaveText(`D4 parent ${run} Issue#${parent.seq}`);
  await expect(narrows.locator(".edge-name").first()).toHaveText("narrows");
  expect(writes.map((write) => `${write.method()} ${new URL(write.url()).pathname}`)).toEqual(["POST /api/inquiries/issue"]);
  const body = writes[0]!.postDataJSON();
  expect(body).toEqual({
    title: `D4 child ${run}`,
    validation: "Both land together",
    status: "active",
    priority: 20,
    issue_kind: ["task"],
    narrows: [[parent.id, null]],
    requires: [blocker.id],
    idempotency_key: body.idempotency_key,
  });
  expect((await request.get(`/api/edges/${created}/narrows/${parent.id}`)).ok()).toBe(true);
  expect((await request.get(`/api/edges/${created}/requires/${blocker.id}`)).ok()).toBe(true);
});

test("Create more keeps the form open; a relation the body cannot hold makes each create one batch", async ({ page, request }) => {
  const run = crypto.randomUUID().slice(0, 8);
  const question = await createIssue(request, `D4 question ${run}`);
  await page.goto("/app/#/new/Artifact");
  const form = page.getByRole("dialog", { name: "New artifact" });
  const writes = writesOf(page);
  await form.getByRole("button", { name: "Create more" }).click();
  await expect(form.getByRole("button", { name: "Create more" })).toHaveAttribute("aria-pressed", "true");

  const titles = [`D4 answer one ${run}`, `D4 answer two ${run}`];
  for (const title of titles) {
    await form.getByRole("textbox", { name: "Title" }).fill(title);
    await addRelation(page, form, "Produced by", `Issue#${question.seq}`, new RegExp(`^Issue#${question.seq}`));
    await page.keyboard.press("ControlOrMeta+Enter");
    await expect(form.getByRole("textbox", { name: "Title" })).toHaveValue("");
    await expect(form.getByRole("textbox", { name: "Title" })).toBeFocused();
    await expect(form.getByRole("list", { name: "Relations" })).toBeHidden();
  }
  await expect(page).toHaveURL(/#\/new\/Artifact$/);

  expect(writes.map((write) => `${write.method()} ${new URL(write.url()).pathname}`)).toEqual([
    "POST /api/inquiries/batch",
    "POST /api/inquiries/batch",
  ]);
  const bodies = writes.map((write) => write.postDataJSON());
  expect(bodies.map(({ items, edges }) => [items.map(({ idempotency_key, ...item }: { idempotency_key: string }) => item), edges])).toEqual(
    titles.map((title) => [[{ kind: "Artifact", title, status: "active" }], [{ edge_kind: "produced_by", from_index: 0, to_id: question.id }]]),
  );
  expect(bodies[0].items[0].idempotency_key).not.toBe(bodies[1].items[0].idempotency_key);
  const produces = await (await request.get(`/api/web/get/${question.id}`)).json();
  expect(produces.backlinks.produced_by.map((peer: { title: string }) => peer.title).sort()).toEqual([...titles].sort());

  await form.getByRole("button", { name: "Cancel" }).click();
  await expect(form).toBeHidden();
});

test("a form opened with C and closed puts focus back on the list's current row, not the page body", async ({ page, request }) => {
  await createIssue(request, `D4 focus ${crypto.randomUUID().slice(0, 8)}`);
  await page.goto("/app/#/list/Issue");
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Issues");
  const current = page.locator('a.row[aria-current="true"]');
  await expect(current).toHaveCount(1);
  for (const close of ["Escape", "Cancel"]) {
    await page.keyboard.press("c");
    const form = page.getByRole("dialog", { name: "New issue" });
    await expect(form.getByRole("textbox", { name: "Title" })).toBeFocused();
    if (close === "Escape") await page.keyboard.press("Escape");
    else await form.getByRole("button", { name: "Cancel" }).click();
    await expect(form).toBeHidden();
    await expect(current, `closed by ${close}`).toBeFocused();
  }
});
