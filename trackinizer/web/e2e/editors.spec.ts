import { execFile } from "node:child_process";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import type { APIRequestContext, Page, Request } from "@playwright/test";
import { expect, failedResource, test } from "./fixtures";

// The detail's editors against the e2e server, with the Python client as a
// second user. Each test makes its own Issue, so rows other spec files create
// never meet these.

const ROOT = fileURLToPath(new URL("../../../..", import.meta.url));

/** Edit field `field` of inquiry `id` to the JSON `value` as the Python client, as `e2e-writer`. */
async function pythonEdit(baseURL: string, id: string, field: string, value: unknown): Promise<void> {
  const code = [
    "import json, sys, uuid",
    "from trackinizer.client.client import Client",
    "client = Client(sys.argv[1], author='e2e-writer')",
    "client.edit(uuid.UUID(sys.argv[2]), sys.argv[3], json.loads(sys.argv[4]), actor='e2e-writer')",
  ].join("\n");
  await promisify(execFile)("uv", ["--quiet", "run", "--frozen", "python", "-c", code, baseURL, id, field, JSON.stringify(value)], {
    cwd: ROOT,
  });
}

async function createIssue(request: APIRequestContext, fields: object): Promise<string> {
  const response = await request.post("/api/inquiries/issue", { data: { ...fields, idempotency_key: crypto.randomUUID() } });
  expect(response.ok(), await response.text()).toBe(true);
  return (await response.json()).id;
}

async function stored(request: APIRequestContext, id: string): Promise<{ [field: string]: unknown }> {
  return (await request.get(`/api/inquiries/${id}`)).json();
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

const properties = (page: Page) => page.getByRole("complementary", { name: "Properties" });
const propButton = (page: Page, field: string) => properties(page).locator(`[data-field="${field}"] button.prop-btn`);

test("a status change after the Python client's gets the conflict dialog, then lands on Save mine", async ({
  page,
  request,
  baseURL,
  allowErrors,
}) => {
  const id = await createIssue(request, { title: "D2 status conflict" });
  // With the stream on, the other change could show before the click; acting on
  // what the page showed before it did is the case compare-and-set exists for.
  await page.route("**/api/web/subscribe", (route) => route.abort());
  // The stream this turns off, and the compare-and-set 409 the other change causes.
  allowErrors(failedResource("/api/web/subscribe", "ERR_FAILED"));
  allowErrors(failedResource(`/api/inquiries/${id}/status`, 409));
  await page.goto(`/app/#/lookup/${id}`);
  await expect(propButton(page, "status")).toHaveText("Active");
  await pythonEdit(baseURL!, id, "status", "abandoned");

  await propButton(page, "status").click();
  await page.getByRole("option", { name: "Complete" }).click();
  const dialog = page.getByRole("alertdialog", { name: "Status changed" });
  await expect(dialog).toContainText("e2e-writer changed this since you saw it.");
  await expect(dialog.locator("dd")).toHaveText(["Abandoned", "Complete"]);
  await dialog.getByRole("button", { name: "Save mine" }).click();
  await expect(propButton(page, "status")).toHaveText("Complete");
  expect((await stored(request, id)).status).toBe("complete");
});

/** Set Issue `id`'s priority to `value` directly, as the second user `e2e-writer`. */
async function setPriorityAsOther(request: APIRequestContext, id: string, value: number): Promise<{ change_id: string | null }> {
  const response = await request.put(`/api/issue/${id}/priority`, { data: { value, actor: "e2e-writer" } });
  expect(response.ok(), await response.text()).toBe(true);
  return response.json();
}

/**
 * Pick P1 High for Issue `id`'s priority, from P2 Medium, while another user
 * sets it to P3 Low after the write reaches the server and before its answer
 * would reach the page, which never gets it. `before` runs first, inside that
 * window. The server replays no write that changed nothing, so only the read
 * before a resend keeps the page from saving P1 High over P3 Low.
 */
async function pickHighWhileAnswerLost(
  page: Page,
  request: APIRequestContext,
  id: string,
  before: () => Promise<unknown> = async () => {},
): Promise<{ writes: Request[]; answers: { change_id: string | null }[] }> {
  const answers: { change_id: string | null }[] = [];
  await page.route(`**/api/issue/${id}/priority`, async (route) => {
    await before();
    answers.push(await (await route.fetch()).json());
    await setPriorityAsOther(request, id, 30);
    await route.abort();
  });
  const writes = await openDetail(page, id);
  await expect(propButton(page, "priority")).toContainText("P2");
  await propButton(page, "priority").click();
  await page.getByRole("option", { name: "P1 High" }).click();
  return { writes, answers };
}

/** Expect the conflict dialog to name the other user, keep theirs, and see P3 Low stay. */
async function keepTheirs(page: Page, request: APIRequestContext, id: string): Promise<void> {
  const dialog = page.getByRole("alertdialog", { name: "Priority changed" });
  await expect(dialog).toContainText("e2e-writer changed this since you saw it.");
  await dialog.getByRole("button", { name: "Keep theirs" }).click();
  await expect(dialog).toBeHidden();
  await expect(propButton(page, "priority")).toContainText("P3");
  expect((await stored(request, id)).priority).toBe(30);
}

test("a field write whose answer is lost while another user changes the field shows the conflict, and is not resent (K1)", async ({
  page,
  request,
  allowErrors,
}) => {
  const id = await createIssue(request, { title: "K1 lost answer", priority: 20 });
  // The write's answer, which the route drops once the server has applied it.
  allowErrors(failedResource(`/api/issue/${id}/priority`, "ERR_FAILED"));
  const { writes, answers } = await pickHighWhileAnswerLost(page, request, id);
  await keepTheirs(page, request, id);
  expect(answers.map((answer) => answer.change_id === null)).toEqual([false]);
  expect(writes).toHaveLength(1);
});

test("the same for a field write that changed nothing, whose key the server does not replay (K1)", async ({ page, request, allowErrors }) => {
  const id = await createIssue(request, { title: "K1 lost no-op", priority: 20 });
  // The write's answer, which the route drops once the server has applied it.
  allowErrors(failedResource(`/api/issue/${id}/priority`, "ERR_FAILED"));
  // The other user sets P1 High first, so the page's write changes nothing.
  const { writes, answers } = await pickHighWhileAnswerLost(page, request, id, () => setPriorityAsOther(request, id, 10));
  await keepTheirs(page, request, id);
  expect(answers.map((answer) => answer.change_id === null)).toEqual([true]);
  expect(writes).toHaveLength(1);
});

test("a label is added and removed with one single-element PATCH each, never a PUT", async ({ page, request }) => {
  const id = await createIssue(request, { title: "D2 labels", labels: ["d2-keep"] });
  const writes = await openDetail(page, id);
  await propButton(page, "labels").click();
  await page.getByRole("combobox").fill("d2-new");
  await page.keyboard.press("Enter");
  await expect(propButton(page, "labels")).toHaveText("d2-keepd2-new");
  await page.getByRole("option", { name: "d2-keep" }).click();
  await expect(propButton(page, "labels")).toHaveText("d2-new");
  await page.keyboard.press("Escape");

  expect(writes.map((write) => `${write.method()} ${new URL(write.url()).pathname.split("/").at(-1)}`)).toEqual([
    "PATCH labels",
    "PATCH labels",
  ]);
  expect(writes.map((write) => write.postDataJSON())).toEqual([
    { op: "add", value: "d2-new" },
    { op: "sub", value: "d2-keep" },
  ]);
  expect((await stored(request, id)).labels).toEqual(["d2-new"]);
});

test("the exact priority takes a typed number, focused as it opens (COLD-10)", async ({ page, request }) => {
  const id = await createIssue(request, { title: "D2 exact priority", priority: 20 });
  await openDetail(page, id);
  await propButton(page, "priority").click();
  // A digit in the menu's search box is typed, not picked (COLD-15).
  await page.keyboard.type("1");
  await expect(page.getByRole("combobox")).toHaveValue("1");
  await page.keyboard.press("Backspace");
  await page.getByRole("option", { name: /^Exact number…/ }).click();
  const exact = page.getByRole("spinbutton", { name: "Exact priority" });
  await expect(exact).toBeFocused();
  await page.keyboard.type("15");
  await page.keyboard.press("Enter");
  await expect(propButton(page, "priority")).toHaveText("P1 High15");
  await expect(propButton(page, "priority")).toBeFocused();
  expect((await stored(request, id)).priority).toBe(15);
});

test("a description draft is kept across a live update, and names who changed the text under it", async ({
  page,
  request,
  baseURL,
}) => {
  const id = await createIssue(request, { title: "D2 draft", description: "First words." });
  await openDetail(page, id);
  await page.getByText("First words.").click();
  const editor = page.getByRole("textbox", { name: "Description" });
  await editor.press("End");
  await editor.pressSequentially(" My draft.");

  // Another field changes: the live refetch redraws the page and keeps the draft.
  await pythonEdit(baseURL!, id, "title", "D2 draft, retitled");
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("D2 draft, retitled");
  await expect(editor).toHaveValue("First words. My draft.");
  await expect(editor).toBeFocused();

  // The description itself changes: the editor says who, and keeps mine on request.
  await pythonEdit(baseURL!, id, "description", "Their words.");
  const notice = page.locator(".ed-changed");
  await expect(notice).toContainText("e2e-writer changed description while you were editing.");
  await expect(editor).toHaveValue("First words. My draft.");
  await notice.getByRole("button", { name: "Keep mine" }).click();
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(editor).toBeHidden();
  await expect(page.locator('[data-field="description"]')).toHaveText("First words. My draft.");
  expect((await stored(request, id)).description).toBe("First words. My draft.");
});

test("an Issue's last type goes with a DELETE checked at save, so a type the Python client added meanwhile stays (R4-F02)", async ({
  page,
  request,
  baseURL,
  allowErrors,
}) => {
  const id = await createIssue(request, { title: "R2 last type", issue_kind: ["bug"] });
  // Without the stream the page keeps showing one type: the case an unchecked whole-field DELETE got wrong.
  await page.route("**/api/web/subscribe", (route) => route.abort());
  // The stream this turns off.
  allowErrors(failedResource("/api/web/subscribe", "ERR_FAILED"));
  const writes: Request[] = [];
  page.on("request", (sent) => {
    if (sent.method() !== "GET" && sent.url().includes("/api/")) writes.push(sent);
  });
  const sent = () => writes.map((write) => [`${write.method()} ${new URL(write.url()).pathname.split("/").at(-1)}`, write.postDataJSON()]);
  await page.goto(`/app/#/lookup/${id}`);
  await expect(propButton(page, "issue_kind")).toHaveText("bug");
  await pythonEdit(baseURL!, id, "issue_kind", ["bug", "feature"]);

  // The server will not empty an Issue's type by PATCH, so the last one goes by
  // DELETE, which would clear the added type too: the check at save stops it.
  await propButton(page, "issue_kind").click();
  await page.getByRole("option", { name: "bug" }).click();
  const dialog = page.getByRole("alertdialog", { name: "Type changed" });
  await dialog.getByRole("button", { name: "Keep theirs" }).click();
  await expect(dialog).toBeHidden();
  await expect(propButton(page, "issue_kind")).toHaveAccessibleName("bug feature");
  expect(sent()).toEqual([]);
  expect((await stored(request, id)).issue_kind).toEqual(["bug", "feature"]);

  // With both shown, bug goes by PATCH, and the last, feature, by DELETE.
  await propButton(page, "issue_kind").click();
  await page.getByRole("option", { name: "bug" }).click();
  await expect(page.getByRole("option", { name: "bug" })).toHaveAttribute("aria-selected", "false");
  await page.getByRole("option", { name: "feature" }).click();
  await expect.poll(async () => (await stored(request, id)).issue_kind).toBeNull();
  expect(sent()).toEqual([
    ["PATCH issue_kind", { op: "sub", value: "bug" }],
    ["DELETE issue_kind", {}],
  ]);
});
