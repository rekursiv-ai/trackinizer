import type { Page } from "@playwright/test";
import { expect, failedResource, test } from "./fixtures";

// The sign-in page (server/assets/login.html). This server has no Google
// sign-in, so a test that needs the page to offer it answers its check,
// `/auth/login/ready`, itself.

const READY = "**/auth/login/ready";

test("offers Google sign-in once the server says it can, passing the link's view on as next", async ({ page }) => {
  let answer = () => {};
  const asked = new Promise<void>((resolve) => (answer = resolve));
  await page.route(READY, async (route) => {
    await asked;
    await route.fulfill({ status: 204 });
  });
  await page.goto("/auth/login_page?next=%2Fapp%2F#/list/Paper");
  const google = page.getByRole("link", { name: "Sign in with Google" });
  // While the server is asked, the page offers nothing and says nothing.
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  await expect(google).toBeHidden();
  await expect(page.getByRole("status")).toHaveText("");

  answer();
  await expect(google).toBeVisible();
  await expect(google).toHaveAttribute("href", `/auth/login?next=${encodeURIComponent("/app/#/list/Paper")}`);
  await expect(page.getByRole("status")).toHaveText("");
  // Where sign-in leads is the link's business, not the page's.
  await expect(page.locator("body")).not.toContainText("/app/", { useInnerText: true });
});

test("a next that is not a path on this server is dropped", async ({ page }) => {
  await page.route(READY, (route) => route.fulfill({ status: 204 }));
  await page.goto("/auth/login_page?next=https%3A%2F%2Fexample.com%2F");
  await expect(page.getByRole("link", { name: "Sign in with Google" })).toHaveAttribute("href", "/auth/login");
});

for (const [status, why] of [
  [404, "where sign-in is not installed"],
  [503, "where it is installed but not configured"],
] as const) {
  test(`says sign-in is not configured ${why}`, async ({ page, allowErrors }) => {
    // The check's answer, which this test provokes.
    allowErrors(failedResource("/auth/login/ready", status));
    if (status !== 404) await page.route(READY, (route) => route.fulfill({ status }));
    await page.goto("/auth/login_page?next=%2Fapp%2F");
    await expect(page.getByRole("status")).toHaveText("Sign-in is not configured on this server.");
    await expect(page.getByRole("link", { name: "Sign in with Google" })).toBeHidden();
  });
}

test("says so when the server cannot be reached", async ({ page, allowErrors }) => {
  // The aborted check, which this test provokes.
  allowErrors(failedResource("/auth/login/ready", "ERR_FAILED"));
  await page.route(READY, (route) => route.abort());
  await page.goto("/auth/login_page?next=%2Fapp%2F");
  await expect(page.getByRole("status")).toHaveText("Could not reach the server. Reload to try again.");
  await expect(page.getByRole("link", { name: "Sign in with Google" })).toBeHidden();
});

test.describe("the backdrop", () => {
  test.beforeEach(async ({ page }) => {
    await page.route(READY, (route) => route.fulfill({ status: 204 }));
  });

  test("moves, and stops while the tab is hidden", async ({ page }) => {
    await page.goto("/auth/login_page");
    await expect.poll(() => moved(page)).toBe(true);
    await page.evaluate(() => {
      Object.defineProperty(document, "hidden", { configurable: true, get: () => true });
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(await moved(page)).toBe(false);
    await page.evaluate(() => {
      Object.defineProperty(document, "hidden", { configurable: true, get: () => false });
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await expect.poll(() => moved(page)).toBe(true);
  });

  test("holds still under reduced motion, and still draws the graph", async ({ page }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.goto("/auth/login_page");
    await expect(page.getByRole("link", { name: "Sign in with Google" })).toBeVisible();
    expect(await moved(page)).toBe(false);
    expect(await page.locator("canvas").evaluate(colouredPixels)).toBeGreaterThan(0);

    // Asked for motion again, it moves.
    await page.emulateMedia({ reducedMotion: "no-preference" });
    await expect.poll(() => moved(page)).toBe(true);
  });
});

/** Whether the backdrop's canvas changed over the next ten frames. */
async function moved(page: Page): Promise<boolean> {
  return page.locator("canvas").evaluate(async (canvas: HTMLCanvasElement) => {
    const before = canvas.toDataURL();
    for (let frame = 0; frame < 10; frame++) await new Promise((resolve) => requestAnimationFrame(resolve));
    return canvas.toDataURL() !== before;
  });
}

/** How many of the canvas's pixels are drawn on, in a sample of every 97th. */
function colouredPixels(canvas: HTMLCanvasElement): number {
  const { data } = canvas.getContext("2d")!.getImageData(0, 0, canvas.width, canvas.height);
  let drawn = 0;
  for (let at = 3; at < data.length; at += 4 * 97) if (data[at]) drawn++;
  return drawn;
}
