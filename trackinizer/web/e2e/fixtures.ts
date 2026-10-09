// The suite's `test` and `expect`. Every spec imports them from here, not from
// @playwright/test, so every test runs under the error guard below.
import { type APIRequestContext, type Page, test as base, expect } from "@playwright/test";

export { expect };

/** Agree to the rules as they stand, as the page does: naming the version the profile shows. */
export async function agreeToRules(request: APIRequestContext): Promise<void> {
  const profile = (await (await request.get("/api/me/profile")).json()) as { rules_version: string };
  const put = await request.put("/api/me/acknowledge", { data: { rules_version: profile.rules_version } });
  if (!put.ok()) throw new Error(`Could not agree to the rules: ${put.status()} ${await put.text()}`);
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
  canvas: boolean;
  canvasPreference: undefined;
  welcome: boolean;
  welcomeAgreement: undefined;
}>({
  // Whether the suite's one user has the agent canvas on for the test. It is on
  // by default for every user, and a spec of the default path (stream.spec,
  // session-chat.spec, the canvas specs) asks for it with `test.use({ canvas: true })`.
  // The view specs measure the page's own geometry and the keys it takes, which
  // a Chat pane beside it changes, so they run with it off, and the one spec of
  // that path is canvas-optout.spec.ts.
  canvas: [false, { option: true }],
  canvasPreference: [
    async ({ request, canvas }, use) => {
      const put = await request.put("/api/me/visual-workspace", { data: { enabled: canvas } });
      if (!put.ok()) throw new Error(`Could not set the canvas preference: ${put.status()}`);
      await use(undefined);
    },
    { auto: true },
  ],
  // Whether the test is about the welcome flow. Every other test starts with the suite's
  // one user having agreed to the rules in force, as a returning user has, so the flow's
  // dialog does not stand over the page the test is about. A spec of the flow asks for
  // `test.use({ welcome: true })`, and makes its user new itself.
  welcome: [false, { option: true }],
  welcomeAgreement: [
    async ({ request, welcome }, use) => {
      if (!welcome) await agreeToRules(request);
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

/**
 * The tab's live stream, whichever the app uses: the canvas's events stream
 * (`/api/workspaces/{id}/events`, which carries the inquiry ids) for a user with
 * the canvas on, the default, and `/api/web/subscribe` for one who opted out.
 */
export const STREAM_URL = /\/api\/(?:web\/subscribe(?:$|\?(?!probe))|workspaces\/[^/]+\/events)/;

/** Resolves when the page's live stream has answered. */
export function streamOpened(page: Page, ok = false) {
  return page.waitForResponse((response) => STREAM_URL.test(response.url()) && (!ok || response.status() === 200));
}

/** The globs `page.route` matches the live stream by, either of the two. */
export const STREAM_ROUTES = ["**/api/web/subscribe", "**/api/workspaces/*/events"] as const;

/** Refuse the page's live stream connections, whichever stream it uses, until `page.unroute`. */
export async function abortStream(page: Page) {
  for (const route of STREAM_ROUTES) await page.route(route, (request) => request.abort());
}

/** Allow the browser's error line for each stream route a test refused. */
export function allowStreamErrors(allowErrors: (pattern: RegExp) => void, status: number | string = "ERR_FAILED") {
  allowErrors(failedResource("/api/web/subscribe", status));
  allowErrors(failedResource("/api/workspaces/", status));
}
