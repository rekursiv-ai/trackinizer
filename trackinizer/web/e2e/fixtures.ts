// The suite's `test` and `expect`. Every spec imports them from here, not from
// @playwright/test, so every test runs under the error guard below.
import { test as base, expect } from "@playwright/test";

export { expect };

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
export const test = base.extend<{ allowErrors: (pattern: RegExp) => void }>({
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
