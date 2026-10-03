import type { Page } from "@playwright/test";
import { expect, failedResource, test } from "./fixtures";

// The search results page against the e2e server, as the old UI's `#/search`
// was: one search across every kind, a Ref, Status and Title table with its
// count, no matches, the server's message, and an empty query. Titles carry
// this run's tag, digits only, since specs share one server and search it.

const TAG = `srch${String(crypto.getRandomValues(new Uint32Array(1))[0])}`;
const BUDGET = "query exceeded the time budget; narrow the filters or add more specific terms";

test.beforeAll(async ({ request }) => {
  const items = [
    { kind: "Issue", title: `Search ${TAG} issue` },
    { kind: "Belief", title: `Search ${TAG} belief`, judgement: "proven" },
    { kind: "Paper", title: `Search ${TAG} paper` },
  ].map((item) => ({ ...item, idempotency_key: crypto.randomUUID() }));
  const response = await request.post("/api/inquiries/batch", { data: { items } });
  expect(response.ok(), await response.text()).toBe(true);
});

const heading = (page: Page) => page.getByRole("heading", { level: 1 });
const box = (page: Page) => page.getByRole("searchbox", { name: "Search query" });

/** The results table's rows, each as its cells' text. */
async function rows(page: Page): Promise<string[][]> {
  return page
    .getByRole("table")
    .getByRole("row")
    .evaluateAll((all) => all.map((row) => [...row.querySelectorAll("th, td")].map((cell) => cell.textContent ?? "")));
}

test("a search link shows every kind's matches from one request, newest first, with their count", async ({ page }) => {
  const searches: URL[] = [];
  page.on("request", (request) => {
    if (request.url().includes("/api/web/search")) searches.push(new URL(request.url()));
  });
  await page.goto(`/app/#/search/${encodeURIComponent(`Search ${TAG}`)}`);
  await expect(heading(page)).toHaveText(`Search: Search ${TAG} (3)`);
  expect(await rows(page)).toEqual([
    ["Ref", "Status", "Title"],
    [expect.stringMatching(/^Paper#\d+$/), "Active", `Search ${TAG} paper`],
    [expect.stringMatching(/^Belief#\d+$/), "Active", `Search ${TAG} belief`],
    [expect.stringMatching(/^Issue#\d+$/), "Active", `Search ${TAG} issue`],
  ]);
  expect(searches.map((url) => [url.searchParams.get("q"), url.searchParams.get("kind"), url.searchParams.get("limit")])).toEqual([
    [`Search ${TAG}`, null, "50"],
  ]);
  await page.getByRole("table").getByRole("link", { name: /^Belief#/ }).click();
  await expect(heading(page)).toHaveText(`Search ${TAG} belief`);
});

test("an empty link asks for a query; one entered with no match says so", async ({ page }) => {
  await page.goto("/app/#/search");
  await expect(heading(page)).toHaveText("Search");
  await expect(page.getByText("Enter a query above")).toBeVisible();
  await expect(box(page)).toBeFocused();
  await box(page).fill(`${TAG} nothing at all`);
  await box(page).press("Enter");
  await expect(heading(page)).toHaveText(`Search: ${TAG} nothing at all (0)`);
  await expect(page.getByText("No matches")).toBeVisible();
  expect(new URL(page.url()).hash).toBe(`#/search/${encodeURIComponent(`${TAG} nothing at all`)}`);
});

test("a search over the server's time budget shows its message, and Retry searches again", async ({ page, allowErrors }) => {
  // The browser logs the refused search, which this test provokes.
  allowErrors(failedResource("/api/web/search", 400));
  let refuse = true;
  await page.route("**/api/web/search?*", (route) =>
    refuse ? route.fulfill({ status: 400, json: { detail: BUDGET } }) : route.continue(),
  );
  await page.goto(`/app/#/search/${encodeURIComponent(`Search ${TAG}`)}`);
  await expect(page.getByRole("alert")).toContainText(BUDGET);
  refuse = false;
  await page.getByRole("alert").getByRole("button", { name: "Retry" }).click();
  await expect(heading(page)).toHaveText(`Search: Search ${TAG} (3)`);
});
