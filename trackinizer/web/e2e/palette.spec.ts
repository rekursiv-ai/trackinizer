import type { Page, Route } from "@playwright/test";
import { expect, failedResource, test } from "./fixtures";

// The server is ephemeral and shared with the other spec files, so this file seeds
// its own rows, with words no other spec uses: a hub with three children, a Belief
// that no view here loads, and two titles for a regex. Fixed idempotency keys make
// the seed a replay if a retry runs it again.
const ITEMS = [
  { kind: "Issue", title: "C4 hub" },
  ...[1, 2, 3].map((n) => ({ kind: "Issue", title: `C4 local child ${n}` })),
  { kind: "Belief", title: "C4 server-only belief zebra" },
  { kind: "Issue", title: "C4rx 4242 regex target" },
  { kind: "Issue", title: "C4rx abc no digits" },
];
const BUDGET = "query exceeded the time budget; narrow the filters or add more specific terms";
let ids: string[] = [];

test.beforeAll(async ({ request }) => {
  const items = ITEMS.map((item, n) => ({
    ...item,
    idempotency_key: `c4e2e000-0000-4000-8000-${String(n).padStart(12, "0")}`,
  }));
  const edges = [1, 2, 3].map((n) => ({ from_index: n, to_index: 0, edge_kind: "narrows" }));
  const response = await request.post("/api/inquiries/batch", { data: { items, edges } });
  expect(response.ok(), await response.text()).toBe(true);
  ids = (await response.json()).ids;
});

const heading = (page: Page) => page.getByRole("heading", { level: 1 });
const palette = (page: Page) => page.getByRole("dialog", { name: "Command menu" });
const input = (page: Page) => palette(page).getByRole("combobox", { name: "Command" });
const section = (page: Page, name: RegExp | string) => palette(page).getByRole("group", { name });
const activeOption = (page: Page) => palette(page).locator('[role="option"][aria-selected="true"]');

test("focus lands in the input, keys typed at once are kept, one Escape closes, and focus returns", async ({
  page,
}) => {
  const sidebar = page.getByRole("navigation", { name: "Sidebar" });
  // Five fresh page loads, as the library survey ran: a palette that mounts
  // slowly loses the first keys only now and then.
  for (let run = 0; run < 5; run++) {
    await page.goto(`/app/?run=${run}#/activity`);
    await expect(heading(page)).toHaveText("Activity");
    const opener = sidebar.getByRole("link", { name: "Issues" });
    await opener.focus();
    await page.keyboard.press(run % 2 ? "ControlOrMeta+p" : "ControlOrMeta+k");
    await page.keyboard.type("typed at once");
    await expect(input(page)).toBeFocused();
    await expect(input(page)).toHaveValue("typed at once");
    await page.keyboard.press("Escape");
    await expect(palette(page)).toBeHidden();
    await expect(opener).toBeFocused();
  }

  const button = sidebar.getByRole("button", { name: "Search" });
  await button.click();
  await expect(input(page)).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(palette(page)).toBeHidden();
  await expect(button).toBeFocused();
});

test("loaded rows show while typing, the server's after a pause with their time; arrows and Enter open one", async ({
  page,
}) => {
  // Every search answers a second late, so the loaded rows are seen before any answer.
  await page.route("**/api/web/search?*", async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 1000));
    await route.continue();
  });
  await page.goto(`/app/#/lookup/${ids[0]}`);
  await expect(heading(page)).toHaveText("C4 hub");
  await page.keyboard.press("ControlOrMeta+k");
  await page.keyboard.type("C4 local child");
  await expect(section(page, "Inquiries").getByRole("option")).toHaveCount(3);
  await expect(palette(page).getByRole("status")).toHaveText("Searching the server…");
  // The server finds the same three, already listed, and says when it looked.
  await expect(palette(page).getByRole("status")).toHaveText(
    /^The server's results, as of \d{1,2}:\d\d:\d\d(\s[AP]M)?, are all listed above\.$/,
  );

  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("ArrowUp");
  await expect(activeOption(page)).toContainText("C4 local child");
  const title = (await activeOption(page).locator(".lbl").textContent())!;
  await page.keyboard.press("Enter");
  await expect(palette(page)).toBeHidden();
  await expect(heading(page)).toHaveText(title);
});

test("a regex search keeps its backslashes, and a server result opens", async ({ page }) => {
  await page.goto("/app/#/activity");
  await expect(heading(page)).toHaveText("Activity");
  await page.keyboard.press("ControlOrMeta+k");
  await page.keyboard.type("title:^C4rx\\s\\d+");
  const server = section(page, /^From the server · as of /);
  await expect(server.getByRole("option")).toHaveText([/C4rx 4242 regex target/]);
  await page.keyboard.press("Enter");
  await expect(heading(page)).toHaveText("C4rx 4242 regex target");
});

test("a search link opens the search page on its query and searches at once", async ({ page }) => {
  await page.goto("/app/#/search/zebra");
  await expect(page.getByRole("searchbox", { name: "Search query" })).toHaveValue("zebra");
  await expect(page.getByRole("table").getByRole("row").filter({ hasText: "C4 server-only belief zebra" })).toHaveCount(1);
});

test("typing on cancels the searches still in flight", async ({ page }) => {
  const held: Route[] = [];
  await page.route("**/api/web/search?*", (route) => {
    if (new URL(route.request().url()).searchParams.get("q") === "held") held.push(route);
    else void route.continue();
  });
  const aborted: string[] = [];
  page.on("requestfailed", (request) => {
    if (request.url().includes("/api/web/search")) aborted.push(request.failure()?.errorText ?? "");
  });
  await page.goto("/app/#/activity");
  await expect(heading(page)).toHaveText("Activity");
  await page.keyboard.press("ControlOrMeta+k");
  await page.keyboard.type("held");
  await expect.poll(() => held.length).toBe(9);
  await page.keyboard.type("x");
  await expect.poll(() => aborted).toEqual(Array(9).fill("net::ERR_ABORTED"));
});

test("a search over the server's time budget shows the server's message, and Retry runs it again", async ({
  page,
  allowErrors,
}) => {
  // The Issue search the route answers 400 below.
  allowErrors(failedResource("/api/web/search", 400));
  let budget = true;
  await page.route("**/api/web/search?*", (route) => {
    const kind = new URL(route.request().url()).searchParams.get("kind");
    if (budget && kind === "Issue") void route.fulfill({ status: 400, json: { detail: BUDGET } });
    else void route.continue();
  });
  await page.goto("/app/#/activity");
  await expect(heading(page)).toHaveText("Activity");
  await page.keyboard.press("ControlOrMeta+k");
  await page.keyboard.type("C4 hub");
  const server = section(page, /^From the server/);
  await expect(server.getByRole("option")).toHaveText([`Issues: ${BUDGET}Retry`]);

  budget = false;
  await page.keyboard.press("Enter");
  await expect(server.getByRole("option")).toHaveText([/C4 hub/]);
});

test("picking a command runs it", async ({ page }) => {
  await page.goto("/app/#/list/Paper");
  await expect(heading(page)).toHaveText("Papers");
  await page.keyboard.press("ControlOrMeta+k");
  await page.keyboard.type("activity");
  await expect(activeOption(page)).toHaveText(/Go to Activity/);
  await page.keyboard.press("Enter");
  await expect(heading(page)).toHaveText("Activity");
  expect(new URL(page.url()).hash).toBe("#/activity");
});
