import type { APIRequestContext, Page } from "@playwright/test";
import { expect, failedResource, test } from "./fixtures";
import { exposeEventSources, resetCanvas } from "./canvasState";

// A station is a named workstream with a chat, a page and visuals of its own.
// Two stations are made and renamed; each keeps its page and its visuals as the
// tab cycles with `g ]`, finds one by search with `g s`, and titles the tab by
// its name.

test.use({ canvas: true });

test.beforeEach(async ({ request }) => {
  await resetCanvas(request);
});

async function open(page: Page): Promise<void> {
  await page.addInitScript(exposeEventSources);
  await page.setViewportSize({ width: 1280, height: 800 });
  await page.goto("/app/#/list/Issue");
  await expect(page.locator(".station-name")).toBeVisible();
}

/** Rename the station on show in place: a click on its tab, the new name, Enter. */
async function rename(page: Page, name: string): Promise<void> {
  await page.locator(".station-name").click();
  const field = page.getByRole("navigation", { name: "Station tabs" }).getByRole("textbox", { name: "Station name" });
  await field.fill(name);
  await field.press("Enter");
  await expect(field).toBeHidden();
  await expect(page.locator(".station-name")).toHaveText(name);
}

/** Make a fresh station with + and wait for its tab. */
async function newStation(page: Page): Promise<void> {
  await page.getByRole("button", { name: "New station", exact: true }).click();
  await expect(page.locator(".station-name")).toHaveText(/^unnamed-workstream#\d+$/);
}

/** The page the server holds for the station named `name`, once it is `route`. */
async function pageOf(request: APIRequestContext, name: string, route: string): Promise<void> {
  await expect.poll(async () => {
    const stations = (await (await request.get("/api/workspaces")).json()) as { name: string; page: string | null }[];
    return stations.find((each) => each.name === name)?.page;
  }, { timeout: 5_000 }).toBe(route);
}

async function showTimeline(request: APIRequestContext, name: string): Promise<void> {
  const stations = (await (await request.get("/api/workspaces")).json()) as { id: string; name: string }[];
  const station = stations.find((each) => each.name === name)!;
  const issue = await (await request.post("/api/inquiries/issue", {
    data: { title: `Station ${crypto.randomUUID().slice(0, 8)}`, idempotency_key: crypto.randomUUID() },
  })).json();
  const state = await (await request.get(`/api/workspaces/${station.id}`)).json();
  const shown = await request.post(`/api/workspaces/${station.id}/operations`, {
    headers: { "Idempotency-Key": crypto.randomUUID() },
    data: { revision: state.revision, operation: { kind: "show", visual_type: "trax.timeline", placement: "main", record_id: issue.id } },
  });
  expect(shown.ok(), await shown.text()).toBe(true);
}

test("two stations keep their own pages and visuals, and the tab cycles, searches and is titled by name", async ({ page, request }) => {
  const timeline = page.locator('.visual-tile[data-visual-type="trax.timeline"]');
  await open(page);

  // The first station: renamed, on the activity page, with a timeline.
  await rename(page, "alpha");
  await page.evaluate(() => { window.location.hash = "#/activity"; });
  await pageOf(request, "alpha", "#/activity");
  await showTimeline(request, "alpha");
  await expect(timeline).toBeVisible();
  await expect(page).toHaveTitle("alpha · Trackinizer");

  // The second: new, on the graph with no visual of its own, then renamed and moved to the Issue list.
  await newStation(page);
  await expect(page).toHaveURL(/#\/graph$/);
  await expect(timeline).toHaveCount(0);
  await rename(page, "beta");
  await page.evaluate(() => { window.location.hash = "#/list/Issue"; });
  await pageOf(request, "beta", "#/list/Issue");
  await expect(page).toHaveTitle("beta · Trackinizer");

  // Each station is a tab, in the order they were made; a click on one opens it, and opening moves no tab.
  const tabs = page.getByRole("navigation", { name: "Station tabs" });
  const order = await tabs.getByRole("button").allTextContents();
  expect(order.indexOf("alpha")).toBeLessThan(order.indexOf("beta"));
  await tabs.getByRole("button", { name: "alpha", exact: true }).click();
  await expect(page.locator(".station-name")).toHaveText("alpha");
  await expect(page).toHaveURL(/#\/activity$/);
  expect(await tabs.getByRole("button").allTextContents()).toEqual(order);
  await tabs.getByRole("button", { name: "beta", exact: true }).click();
  await expect(page.locator(".station-name")).toHaveText("beta");
  await expect(page).toHaveURL(/#\/list\/Issue$/);

  // g ] goes to the next renamed station, wrapping, and each shows what it kept.
  // A click on the workbench's corner takes the keyboard out of the tabs without moving the page.
  await page.locator(".visual-canvas").click({ position: { x: 4, y: 4 } });
  await page.keyboard.press("g");
  await page.keyboard.press("]");
  await expect(page.locator(".station-name")).toHaveText("alpha");
  await expect(page).toHaveURL(/#\/activity$/);
  await expect(timeline).toBeVisible();
  await expect(page).toHaveTitle("alpha · Trackinizer");
  await page.keyboard.press("g");
  await page.keyboard.press("]");
  await expect(page.locator(".station-name")).toHaveText("beta");
  await expect(page).toHaveURL(/#\/list\/Issue$/);
  await expect(timeline).toHaveCount(0);

  // g s finds a station by part of its name; Enter opens it.
  await page.keyboard.press("g");
  await page.keyboard.press("s");
  const search = page.getByRole("searchbox", { name: "Search stations" });
  await expect(search).toBeFocused();
  await search.fill("ALP");
  await expect(page.getByRole("dialog", { name: "Stations" }).getByRole("listitem")).toHaveCount(1);
  await search.press("Enter");
  await expect(page.locator(".station-name")).toHaveText("alpha");
  await expect(timeline).toBeVisible();

  // A reload keeps the tab's station, on its page; a new tab has none and opens the one changed last.
  await page.reload();
  await expect(page.locator(".station-name")).toHaveText("alpha");
  await expect(timeline).toBeVisible();
  // Writing beta's page makes it the station changed last.
  const beta = ((await (await request.get("/api/workspaces")).json()) as { id: string; name: string }[]).find((each) => each.name === "beta")!;
  expect((await request.put(`/api/workspaces/${beta.id}/page`, { data: { route: "#/list/Issue" } })).ok()).toBe(true);
  const another = await page.context().newPage();
  await another.goto("/app/");
  await expect(another.locator(".station-name")).toHaveText("beta");
  await another.close();
});

test("a name the user already has is refused, and the tabs say so", async ({ page, allowErrors }) => {
  allowErrors(failedResource("/api/workspaces/", 409));
  await open(page);
  await rename(page, "taken");
  await newStation(page);
  await page.locator(".station-name").click();
  const field = page.getByRole("textbox", { name: "Station name" });
  await field.fill("taken");
  await field.press("Enter");
  await expect(page.locator(".station-tabs").getByRole("alert")).toHaveText("That name is taken");
  await expect(field).toBeVisible();
});

/** Two renamed stations, the second named `second`, with the tab on its Issue list. Names are the user's own, so each case picks its own. */
async function twoStations(page: Page, first: string, second: string): Promise<void> {
  await open(page);
  await rename(page, first);
  await newStation(page);
  await rename(page, second);
  await page.evaluate(() => { window.location.hash = "#/list/Issue"; });
  await expect(page).toHaveURL(/#\/list\/Issue$/);
}

test("after a click on a list row, which opens its page, g ] still switches station, and so do g a, g c and g g", async ({ page }) => {
  const tag = crypto.randomUUID().slice(0, 8);
  await twoStations(page, `row-a-${tag}`, `row-b-${tag}`);
  await page.locator("a.row").first().click();
  await expect(page).toHaveURL(/#\/(lookup|ref)\//);
  await page.keyboard.press("g");
  await page.keyboard.press("]");
  // The stations earlier cases renamed are in the cycle too, so the next one is not the second's.
  await expect(page.locator(".station-name")).not.toHaveText(`row-b-${tag}`);
  for (const [chord, route] of [["a", /#\/activity$/], ["c", /#\/console$/], ["g", /#\/graph$/]] as const) {
    await page.goto("/app/#/list/Issue");
    await page.locator("a.row").first().click();
    await expect(page).toHaveURL(/#\/(lookup|ref)\//);
    await page.keyboard.press("g");
    await page.keyboard.press(chord);
    await expect(page, `g ${chord}`).toHaveURL(route);
  }
});
