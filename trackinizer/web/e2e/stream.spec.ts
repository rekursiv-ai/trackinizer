import { execFile } from "node:child_process";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import type { APIRequestContext, Page } from "@playwright/test";
import { expect, test, streamOpened } from "./fixtures";

// The default path: the canvas is on, so the tab's stream is the canvas's.
test.use({ canvas: true });

// Live updates against the e2e server: a second client, the Python one, writes
// while the page watches. Each test filters a list to a label of its own, so
// rows other spec files create never match it.

const ROOT = fileURLToPath(new URL("../../../..", import.meta.url));

/**
 * Run `code` with a Python `Client` on the server at `baseURL` as `client`, and
 * the extra `args` from `sys.argv[2]` on. Resolves with what it printed.
 */
async function python(baseURL: string, code: string, ...args: string[]): Promise<string> {
  const prelude = [
    "import sys, time, uuid",
    "from trackinizer.client.client import Client",
    "client = Client(sys.argv[1], author='e2e-writer')",
  ].join("\n");
  const { stdout } = await promisify(execFile)(
    "uv",
    ["--quiet", "run", "--frozen", "python", "-c", `${prelude}\n${code}`, baseURL, ...args],
    { cwd: ROOT },
  );
  return stdout.trim();
}

/** A label no other test uses. */
const freshLabel = () => `e1-live-${crypto.randomUUID().slice(0, 8)}`;

async function createIssue(request: APIRequestContext, title: string, label: string): Promise<string> {
  const response = await request.post("/api/inquiries/issue", {
    data: { title, labels: [label], idempotency_key: crypto.randomUUID() },
  });
  expect(response.ok(), await response.text()).toBe(true);
  return (await response.json()).id;
}

/** Open the Issue list filtered to `label`, once the live stream is connected. */
async function openList(page: Page, label: string) {
  const subscribed = streamOpened(page);
  await page.goto("/app/#/list/Issue");
  await subscribed;
  await page.getByRole("button", { name: /^Filter$/ }).click();
  await page.getByRole("option", { name: "Label", exact: true }).click();
  await page.getByRole("combobox", { name: /^Label/ }).fill(label);
  await page.keyboard.press("Enter");
  await page.keyboard.press("Escape");
  await expect(page.getByTitle("The same query from the CLI")).toContainText(`labels is ${label}`);
}

test("a change from another client shows in an open list and an open detail within 2 s", async ({
  page,
  request,
  baseURL,
}) => {
  const label = freshLabel();
  const id = await createIssue(request, "Before the change", label);
  await openList(page, label);
  await expect(page.locator("a.row .row-title")).toHaveText(["Before the change"]);
  // The peek shows the focused row's detail beside the list.
  await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
  await page.keyboard.press("Space");
  await expect(page.locator(".peek .d-title")).toHaveText("Before the change");

  const title = `Changed by the Python client ${label}`;
  // Watched from inside the page, every frame, so the time is when it showed.
  const shown = page.waitForFunction(
    (title) =>
      document.querySelector("a.row .row-title")?.textContent === title &&
      document.querySelector(".peek .d-title")?.textContent === title &&
      Date.now(),
    title,
    { polling: "raf", timeout: 20_000 },
  );
  const written = Number(
    await python(
      baseURL!,
      "client.edit(uuid.UUID(sys.argv[2]), 'title', sys.argv[3], actor='e2e-writer')\nprint(int(time.time() * 1000))",
      id,
      title,
    ),
  );
  const seen = Number(await (await shown).jsonValue());
  console.log(`list and detail showed the change ${seen - written} ms after the write returned`);
  expect(seen - written).toBeLessThan(2_000);
});

test("a new matching row enters an empty filtered list", async ({ page, baseURL }) => {
  const label = freshLabel();
  await openList(page, label);
  await expect(page.getByText("Nothing here")).toBeVisible();

  const title = `Created by the Python client ${label}`;
  // Shown, or counted in the "N new" pill: the plan's freshness target for new rows.
  const noticed = page.waitForFunction(
    (title) =>
      ([...document.querySelectorAll("a.row .row-title")].some((row) => row.textContent === title) ||
        document.querySelector(".live-pill") !== null) &&
      Date.now(),
    title,
    { polling: "raf", timeout: 20_000 },
  );
  const written = Number(
    await python(
      baseURL!,
      "client.submit_batch([('Issue', {'title': sys.argv[2], 'labels': [sys.argv[3]]})], actor='e2e-writer')\n" +
        "print(int(time.time() * 1000))",
      title,
      label,
    ),
  );
  const seen = Number(await (await noticed).jsonValue());
  console.log(`the new row was noticed ${seen - written} ms after the write returned`);
  expect(seen - written).toBeLessThan(3_000);
  // The list is at its top and the user idle, so the row joins it by itself.
  await expect(page.locator("a.row .row-title")).toHaveText([title]);
});
