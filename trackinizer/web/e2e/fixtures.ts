// The suite's `test` and `expect`. Every spec imports them from here, not from
// @playwright/test, so every test runs under the error guard below.
import { type APIRequestContext, type Page, test as base, expect } from "@playwright/test";

export { expect };

/** The route the page is on, as the app names it (`#/ref/Issue/7`): its path under `/trax/`, with its query. */
export function route(page: Page): string {
  const url = new URL(page.url());
  if (url.hash.startsWith("#/")) return url.hash;
  const path = url.pathname.replace(/^\/trax\/?/, "");
  return path || url.search ? `#/${path}${url.search}` : "";
}

/**
 * Set the suite's user's preferences (theme, stars, saved views, ...), as Settings
 * or the agent does; `null` puts a key back to its default. They live on the server,
 * so a page loaded after this shows them.
 */
export async function setPreferences(
  request: APIRequestContext,
  change: { readonly [key: string]: unknown },
): Promise<void> {
  const patched = await request.patch("/api/me/preferences", { data: change });
  if (!patched.ok()) throw new Error(`Could not set preferences: ${patched.status()} ${await patched.text()}`);
}

/** An Issue's Properties and details stand folded; open them, as a reader does, where a test reads or edits a field in them. */
export async function openProperties(page: Page): Promise<void> {
  const fold = page.locator(".props-fold");
  await expect(fold).toBeAttached();
  if (!(await fold.evaluate((element: HTMLDetailsElement) => element.open))) await fold.locator("summary").click();
  await expect(fold).toHaveJSProperty("open", true);
}

/**
 * `test`, failing a test whose pages logged a `console.error` or threw an error
 * nothing caught, unless the test allowed it.
 *
 * The app logs a failure it handles (a failed request, a refused write) as a
 * warning, so an error means a bug: an uncaught error, an unhandled rejection or
 * a render crash (`src/debug/`). The browser logs each request answered 4xx or
 * 5xx, or never answered, as an error of its own ("Failed to load resource"), so
 * a test that provokes one on purpose allows it with `allowErrors`, with a
 * comment saying why. The guard watches the test's browser context, so a second
 * page it opens counts too; a context the test makes itself does not.
 */
export const test = base.extend<{
  allowErrors: (pattern: RegExp) => void;
  preferencesReset: undefined;
}>({
  // The suite's one user's preferences live on the server and outlast a test, so
  // each test starts on the defaults; a spec sets what it needs with `setPreferences`.
  preferencesReset: [
    async ({ request }, use) => {
      const profile = (await (await request.get("/api/me/profile")).json()) as { preferences: { [key: string]: unknown } };
      await setPreferences(request, Object.fromEntries(Object.keys(profile.preferences).map((key) => [key, null])));
      await use(undefined);
    },
    { auto: true },
  ],
  allowErrors: [
    async ({ context }, use) => {
      const allowed: RegExp[] = [];
      const seen: string[] = [];
      context.on("console", (message) => {
        if (message.type() === "error") seen.push(`console.error: ${message.text()} (${message.location().url})`);
      });
      context.on("weberror", (error) => seen.push(`page error: ${error.error().stack ?? error.error().message}`));
      await use((pattern) => void allowed.push(pattern));
      expect(seen.filter((line) => !allowed.some((pattern) => pattern.test(line))), "errors the page logged or threw").toEqual([]);
    },
    // Automatic: it guards every test, whether or not the test names it.
    { auto: true },
  ],
});

/**
 * The browser's own error for a request to a URL whose path starts with `path`:
 * answered with HTTP `status`, or failed with Chromium's `net::<status>` (a
 * request a test aborted is `ERR_FAILED`), for `allowErrors`.
 */
export function failedResource(path: string, status: number | string): RegExp {
  const why = typeof status === "number" ? `the server responded with a status of ${status} \\(` : `net::${status} `;
  return new RegExp(`^console\\.error: Failed to load resource: ${why}.*\\(https?://[^/]+${path.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}`);
}

/** The tab's live stream, `/api/web/subscribe` (not the probe the page sends before it). */
export const STREAM_URL = /\/api\/web\/subscribe(?:$|\?(?!probe))/;

/** Resolves when the page's live stream has answered. */
export function streamOpened(page: Page, ok = false) {
  return page.waitForResponse((response) => STREAM_URL.test(response.url()) && (!ok || response.status() === 200));
}

/** The globs `page.route` matches the live stream by. */
export const STREAM_ROUTES = ["**/api/web/subscribe"] as const;

/** Refuse the page's live stream connections until `page.unroute`. */
export async function abortStream(page: Page) {
  for (const route of STREAM_ROUTES) await page.route(route, (request) => request.abort());
}

/** Allow the browser's error line for each stream route a test refused. */
export function allowStreamErrors(allowErrors: (pattern: RegExp) => void, status: number | string = "ERR_FAILED") {
  allowErrors(failedResource("/api/web/subscribe", status));
}
