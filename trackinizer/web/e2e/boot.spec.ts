import { expect, failedResource, test } from "./fixtures";

// The server runs with --no-auth, so every request is the synthetic admin.
test("the shell boots and shows the signed-in profile", async ({ page }) => {
  await page.goto("/app/");
  await expect(page).toHaveTitle("Trackinizer");
  const me = page.getByLabel("Signed in");
  await expect(me.getByText("no-auth@localhost")).toBeVisible();
  await expect(me.getByText("admin", { exact: true })).toBeVisible();
  const favicon = await page.locator('link[rel="icon"]').getAttribute("href");
  expect((await page.request.get(favicon!)).status()).toBe(200);
});

test("a 401 sends the browser to the login page, and the next boot restores the hash", async ({
  page,
  allowErrors,
}) => {
  // The profile's 401, which the route answers below.
  allowErrors(failedResource("/api/me/profile", 401));
  // The login page asks whether it can offer Google sign-in, and this server,
  // which has none configured, answers 404 by design.
  allowErrors(failedResource("/auth/login/ready", 404));
  await page.route("**/api/me/profile", (route) =>
    route.fulfill({ status: 401, json: { detail: "not signed in" } }),
  );
  await page.goto("/app/#/list/Paper");
  await page.waitForURL("**/auth/login_page?next=%2Fapp%2F");

  // Signed in again: the login page returns to /app/ with no hash.
  await page.unroute("**/api/me/profile");
  await page.goto("/app/");
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Papers");
  expect(new URL(page.url()).hash).toBe("#/list/Paper");
});
