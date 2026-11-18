import { execFile } from "node:child_process";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import type { APIRequestContext, Page, Request } from "@playwright/test";
import { expect, failedResource, test, abortStream, allowStreamErrors } from "./fixtures";

// Multi-select edits from a list against the e2e server, with the Python client
// as a second user. Each test labels its rows with a label of its own and
// filters the list to it, so rows other spec files create never meet these.

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

/** Three active Issues labelled with a fresh label; their ids, oldest first, and the label. */
async function createIssues(request: APIRequestContext, name: string): Promise<{ ids: string[]; label: string }> {
  const label = `d5-${name}-${crypto.randomUUID().slice(0, 8)}`;
  const ids = [];
  for (const n of [1, 2, 3]) {
    const response = await request.post("/api/inquiries/issue", {
      data: { title: `D5 ${name} ${n}`, labels: [label], idempotency_key: crypto.randomUUID() },
    });
    expect(response.ok(), await response.text()).toBe(true);
    ids.push((await response.json()).id as string);
  }
  return { ids, label };
}

/** Open the Issue list filtered to `label`, select all three rows by a click and a shift-click, and collect writes. */
async function selectAll(page: Page, label: string): Promise<Request[]> {
  await page.goto("/app/#/list/Issue");
  await page.getByRole("button", { name: /^Filter$/ }).click();
  await page.getByRole("option", { name: "Label", exact: true }).click();
  await page.getByRole("combobox").fill(label);
  await page.keyboard.press("Enter");
  await page.keyboard.press("Escape");
  await expect(page.locator("a.row .row-title")).toHaveCount(3);
  const checks = page.getByRole("button", { name: /^Select Issue#\d+$/ });
  await checks.first().click();
  await checks.last().click({ modifiers: ["Shift"] });
  await expect(page.getByRole("group", { name: "Bulk actions" })).toContainText("3 selected");
  const writes: Request[] = [];
  page.on("request", (sent) => {
    if (sent.method() !== "GET" && sent.url().includes("/api/")) writes.push(sent);
  });
  return writes;
}

async function setStatus(page: Page, status: string): Promise<void> {
  const bar = page.getByRole("group", { name: "Bulk actions" });
  await bar.getByRole("button", { name: "Status" }).click();
  await page.getByRole("option", { name: status, exact: true }).click();
}

async function statuses(request: APIRequestContext, ids: readonly string[]): Promise<string[]> {
  return Promise.all(ids.map(async (id) => (await (await request.get(`/api/inquiries/${id}`)).json()).status as string));
}

/** A status write's row and body, in the order sent. */
const described = (writes: readonly Request[]) =>
  writes.map((write) => ({ id: new URL(write.url()).pathname.split("/")[3], body: write.postDataJSON() }));

test("a bulk status change with one row changed by someone else: the report names it, and Retry sends only it", async ({
  page,
  request,
  baseURL,
  allowErrors,
}) => {
  const { ids, label } = await createIssues(request, "conflict");
  // With the stream on, the other change could show before the pick; acting on
  // what the page showed before it did is the case compare-and-set exists for.
  await abortStream(page);
  // The stream this turns off, and the compare-and-set 409 the other change causes.
  allowStreamErrors(allowErrors);
  allowErrors(failedResource("/api/inquiries/", 409));
  const writes = await selectAll(page, label);
  await pythonEdit(baseURL!, ids[1]!, "status", "abandoned");

  await setStatus(page, "Complete");
  const report = page.getByRole("region", { name: "Status set to Complete: results" });
  await expect(report.getByRole("alert")).toHaveText("Status set to Complete: 1 of 3 not saved.");
  await expect(report.locator("li").first()).toContainText("D5 conflict 2");
  await expect(report.locator("li").first()).toContainText("Changed to Abandoned since the list loaded it.");
  await expect(report.locator("li")).toHaveText([/D5 conflict 2/, /Saved$/, /Saved$/]);
  expect(described(writes).toSorted((a, b) => a.id!.localeCompare(b.id!))).toEqual(
    ids.map((id) => ({ id, body: { value: "complete", mode: "cas", expected: "active" } })).toSorted((a, b) => a.id.localeCompare(b.id)),
  );
  expect(new Set(writes.map((write) => write.headers()["idempotency-key"])).size).toBe(3);

  await report.getByRole("button", { name: "Retry 1 failed" }).click();
  await expect(page.getByText("Status set to Complete on 3 inquiries.", { exact: true })).toBeVisible();
  await expect(report).toBeHidden();
  expect(described(writes.slice(3))).toEqual([{ id: ids[1], body: { value: "complete", mode: "cas", expected: "abandoned" } }]);
  expect(await statuses(request, ids)).toEqual(["complete", "complete", "complete"]);
});

test("a row whose answer is lost after it landed is read back and not sent again; one that never arrived goes again with its key", async ({
  page,
  request,
  allowErrors,
}) => {
  const { ids, label } = await createIssues(request, "lost");
  const writes = await selectAll(page, label);
  // Row 1's write reaches the server and lands, but its answer is lost: the read
  // before a resend finds it landed.
  await page.route(`**/api/inquiries/${ids[0]}/status`, async (route) => {
    await route.fetch();
    await route.fulfill({ status: 503, json: { detail: "answer lost" } });
  });
  // Row 2's first write never reaches the server: the read finds it unsent.
  let dropped = false;
  await page.route(`**/api/inquiries/${ids[1]}/status`, async (route) => {
    if (dropped) return route.fallback();
    dropped = true;
    await route.abort();
  });
  // The answer the first route loses, and the write the second drops.
  allowErrors(failedResource(`/api/inquiries/${ids[0]}/status`, 503));
  allowErrors(failedResource(`/api/inquiries/${ids[1]}/status`, "ERR_FAILED"));

  await setStatus(page, "Complete");
  await expect(page.getByText("Status set to Complete on 3 inquiries.", { exact: true })).toBeVisible();

  const to = (id: string) => writes.filter((write) => write.url().includes(id));
  expect(to(ids[0]!)).toHaveLength(1);
  expect(to(ids[1]!)).toHaveLength(2);
  expect(new Set(to(ids[1]!).map((write) => write.headers()["idempotency-key"])).size).toBe(1);
  expect(writes).toHaveLength(4);
  expect(await statuses(request, ids)).toEqual(["complete", "complete", "complete"]);
  for (const id of ids.slice(0, 2)) {
    const changes = await (await request.get(`/api/change_log?subject_id=${id}`)).json();
    expect(changes.filter((change: { kind: string }) => change.kind === "status")).toHaveLength(1);
  }
});

test("a bulk priority checks each row first: one changed since the list loaded is a conflict and is not sent, and rows already so are the server's to say (CR-W01, R4-F04)", async ({
  page,
  request,
  baseURL,
  allowErrors,
}) => {
  const label = `r2-priority-${crypto.randomUUID().slice(0, 8)}`;
  const ids = [];
  for (const n of [1, 2, 3]) {
    const response = await request.post("/api/inquiries/issue", {
      data: { title: `R2 priority ${n}`, labels: [label], priority: 10, idempotency_key: crypto.randomUUID() },
    });
    expect(response.ok(), await response.text()).toBe(true);
    ids.push((await response.json()).id as string);
  }
  // Without the stream the list keeps showing P1 High on every row.
  await abortStream(page);
  // The stream this turns off.
  allowStreamErrors(allowErrors);
  const writes = await selectAll(page, label);
  await pythonEdit(baseURL!, ids[1]!, "priority", 30);

  await page.getByRole("group", { name: "Bulk actions" }).getByRole("button", { name: "Priority" }).click();
  await page.getByRole("option", { name: "P1 High" }).click();
  const report = page.getByRole("region", { name: "Priority set to P1 High (10): results" });
  await expect(report.getByRole("alert")).toHaveText("Priority set to P1 High (10): 1 of 3 not saved.");
  await expect(report.locator("li")).toHaveText([
    /R2 priority 2.*Changed to P3 Low \(30\) since the list loaded it\./,
    /Already so$/,
    /Already so$/,
  ]);
  const puts = () => writes.filter((write) => write.method() === "PUT").map((write) => new URL(write.url()).pathname.split("/")[3]);
  expect(puts().toSorted()).toEqual([ids[0], ids[2]].toSorted());

  await report.getByRole("button", { name: "Retry 1 failed" }).click();
  await expect(page.getByText("Priority set to P1 High (10) on 1 inquiry; 2 were already so.", { exact: true })).toBeVisible();
  expect(puts().at(-1)).toBe(ids[1]);
  const priorities = await Promise.all(ids.map(async (id) => (await (await request.get(`/api/inquiries/${id}`)).json()).priority));
  expect(priorities).toEqual([10, 10, 10]);
});

test("a toast stands clear of the bulk bar on a 1280 px screen", async ({ page, request }) => {
  await page.setViewportSize({ width: 1280, height: 800 });
  const { label } = await createIssues(request, "toast");
  await selectAll(page, label);
  const bar = page.getByRole("group", { name: "Bulk actions" });
  await bar.getByRole("button", { name: "Priority" }).click();
  await page.getByRole("option", { name: "P1 High" }).click();
  const toast = page.locator(".toast").first();
  await expect(toast).toBeVisible();
  await expect(bar).toBeVisible();
  const [a, b] = [(await toast.boundingBox())!, (await bar.boundingBox())!];
  const overlaps = a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height;
  expect(overlaps, `toast ${JSON.stringify(a)} over bar ${JSON.stringify(b)}`).toBe(false);
});
