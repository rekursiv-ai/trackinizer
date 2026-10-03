// The long-task probe the list and detail budgets read: every main-thread task
// over 50 ms the page has had, and a positive control that proves the probe sees
// one, so its silence around a render means there was none.
import { expect, type Page } from "@playwright/test";

/** One long task: when it started and how long it ran, in ms. */
export type LongTask = { readonly start: number; readonly ms: number };

/** Watch the page's long tasks from before its own scripts run. */
export async function watchLongTasks(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const tasks: { start: number; ms: number }[] = [];
    const keep = (entries: PerformanceEntryList) =>
      tasks.push(...entries.map((task) => ({ start: task.startTime, ms: task.duration })));
    const observer = new PerformanceObserver((list) => keep(list.getEntries()));
    observer.observe({ type: "longtask", buffered: true });
    // An entry reaches the callback a task after it is recorded; `takeRecords`
    // hands over what is still queued, so a read never misses the latest.
    Object.assign(window, { longTasks: () => (keep(observer.takeRecords()), tasks) });
  });
}

/** Every long task the page has had so far. */
export function longTasks(page: Page): Promise<LongTask[]> {
  return page.evaluate(() => (window as unknown as { longTasks: () => { start: number; ms: number }[] }).longTasks());
}

/**
 * The positive control (DRV-05): an 80 ms task of the page's own, run from a
 * timer, must be seen. One that `page.evaluate` runs itself would not do: it is
 * DevTools' task, which the Long Tasks API does not count, so a probe that
 * "saw" nothing there proved nothing.
 */
export async function expectControlSeen(page: Page): Promise<void> {
  const before = (await longTasks(page)).length;
  await page.evaluate(
    () =>
      new Promise<void>((done) =>
        setTimeout(() => {
          const end = performance.now() + 80;
          while (performance.now() < end);
          requestAnimationFrame(() => setTimeout(done, 0));
        }, 0),
      ),
  );
  const seen = (await longTasks(page)).slice(before);
  expect(seen.filter((task) => task.ms >= 80), "the probe sees an 80 ms task of the page's own").toHaveLength(1);
}
