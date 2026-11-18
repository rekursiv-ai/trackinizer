import { expect, test } from "../fixtures";
import { PERF_CHILDREN, readHubs } from "../hubs";
import { expectControlSeen, longTasks, watchLongTasks } from "../longTasks";

// The same check as performance.spec.ts, with the agent canvas on, the default for a
// user: the page loads after the canvas chunk, beside Chat.
//
// The plan's budget "rendering a 50-row list or a detail page causes no
// main-thread task over 50 ms" for the detail: lists.spec.ts checks the list.
// The hub, made by hubs.setup.ts, has 60 children, and each also counts as
// produced by it, the provenance the first structural edge infers; with one
// Artifact it produced, the rail lists 61 children, the first ten at once.
test.use({ canvas: true });

let hub = "";

test.beforeAll(({}, info) => {
  hub = readHubs(info).perf;
});

test("with the canvas on, a hub's detail renders with no main-thread task over 50 ms after its response", async ({ page }) => {
  await watchLongTasks(page);
  // As in lists.spec.ts: other spec files load the same server, so load up to
  // three times, log every sample, and hold each to the budget.
  const samples = [];
  for (let attempt = 0; attempt < 3; attempt++) {
    await page.goto(`/app/?load=${attempt}#/lookup/${hub}`);
    const children = page.getByRole("region", { name: "Children", exact: true });
    await expect(children.locator(".rail-peer")).toHaveCount(10);
    await expect(children.getByRole("button", { name: `Show all ${PERF_CHILDREN + 1}` })).toBeVisible();
    await expect(page.getByRole("region", { name: "Chat" })).toBeVisible();
    const timing = await page.evaluate(() => {
      const detail = performance
        .getEntriesByType("resource")
        .find((entry): entry is PerformanceResourceTiming => entry.name.includes("/api/web/get/"))!;
      return { shownMs: performance.now(), detailEnd: detail.responseEnd };
    });
    const tasks = await longTasks(page);
    console.log(
      `hub detail (canvas on): shown at ${timing.shownMs.toFixed(0)} ms, response at ${timing.detailEnd.toFixed(0)} ms, ` +
        `long tasks ${JSON.stringify(tasks)}`,
    );
    samples.push({ ...timing, tasks });
  }
  // Evaluating the bundle at boot comes before the detail's response and is not
  // the detail's to answer for.
  for (const { tasks, detailEnd } of samples) {
    expect(tasks.filter((task) => task.start + task.ms > detailEnd)).toEqual([]);
  }
  await expectControlSeen(page);
});
