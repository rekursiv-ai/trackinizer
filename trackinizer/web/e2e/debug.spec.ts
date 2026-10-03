import { writeFileSync } from "node:fs";
import type { APIRequestContext, APIResponse, Page, TestInfo } from "@playwright/test";
import { expect, failedResource, test } from "./fixtures";

// Copy details against the e2e server: a render crash, a read and a write the
// server refuses. Each shows Copy details, whose request id is the one the
// server echoed on that response; the server's request line logs that same id
// (RequestLoggingMiddleware, server/api/app.py). The e2e server keeps only
// WARNING lines, so a 4xx's line is not in its log to grep; the echo is.

test.beforeEach(async ({ context }) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
});

async function createIssue(request: APIRequestContext, title: string): Promise<string> {
  const response = await request.post("/api/inquiries/issue", { data: { title, idempotency_key: crypto.randomUUID() } });
  expect(response.ok(), await response.text()).toBe(true);
  return (await response.json()).id;
}

/** Press Copy details, and return what it put on the clipboard. */
async function copyDetails(page: Page, info: TestInfo, name: string): Promise<string> {
  await page.screenshot({ path: info.outputPath(`${name}-before.png`) });
  await page.getByRole("button", { name: "Copy details" }).click();
  await expect(page.getByRole("button", { name: "Details copied" })).toBeVisible();
  const text = await page.evaluate(() => navigator.clipboard.readText());
  // For a person to read after the run, beside the screenshot.
  writeFileSync(info.outputPath(`${name}.txt`), text);
  await page.screenshot({ path: info.outputPath(`${name}.png`) });
  return text;
}

/** The id the server echoed on `response`. */
function echoed(response: { headers(): { [name: string]: string } }): string {
  const id = response.headers()["x-request-id"];
  expect(id).toMatch(/^[0-9a-f-]{36}$/);
  return id!;
}

test("a render crash shows what happened, with Copy details and Reload; the details name the request behind it", async ({
  page,
  allowErrors,
}) => {
  // The crash is logged as an error on purpose; the browser prints nothing else.
  allowErrors(/^console\.error: trackinizer render\.crash error=TypeError /);
  const answered: { response?: APIResponse } = {};
  // The server's own answer, with its echoed id, less the kind list: server drift
  // the router cannot read.
  await page.route("**/api/meta/enums", async (route) => {
    const response = await route.fetch();
    answered.response = response;
    await route.fulfill({ response, json: { ...(await response.json()), inquiry_kind_all: 5 } });
  });
  await page.goto("/app/");
  await expect(page.getByRole("heading", { name: "Trackinizer stopped on an error" })).toBeVisible();
  // The bundle is minified, so the name before `.find` is not the source's.
  await expect(page.getByRole("alert")).toHaveText(/^\w+\.find is not a function$/);
  await expect(page.getByRole("button", { name: "Reload" })).toBeVisible();

  const text = await copyDetails(page, test.info(), "crash");
  expect(text).toMatch(/^Trackinizer web app: The page crashed: \w+\.find is not a function\n/);
  expect(text).toMatch(/\nbuild: [0-9a-f]{40}\n/);
  expect(text).toMatch(/\nerror: TypeError: \w+\.find is not a function\nstack: TypeError: /);
  expect(text).toMatch(/ error render\.crash error=TypeError /);
  expect(text).toMatch(new RegExp(` debug request method=GET path=/api/meta/enums status=200 ms=\\d+ request_id=${echoed(answered.response!)}\\n`));

  await page.unroute("**/api/meta/enums");
  await page.getByRole("button", { name: "Reload" }).click();
  await expect(page.getByLabel("Signed in")).toBeVisible();
});

test("a read the server refuses shows Copy details with the id it echoed", async ({ page, allowErrors }) => {
  // The server's 400, which the browser logs as a failed resource.
  allowErrors(failedResource("/api/inquiries?", 400));
  // The list's first read, sent on with a filter field the server does not know.
  let once = false;
  await page.route("**/api/inquiries?*", (route) => {
    if (once || !route.request().url().includes("kind=Paper")) return route.continue();
    once = true;
    const url = new URL(route.request().url());
    url.searchParams.append("filter", JSON.stringify({ field: "l1b_no_such_field", op: "is", value: "x" }));
    return route.continue({ url: url.toString() });
  });
  const refused = page.waitForResponse((response) => response.url().includes("l1b_no_such_field"));
  await page.goto("/app/#/list/Paper");
  const response = await refused;
  expect(response.status()).toBe(400);
  const alert = page.getByRole("alert");
  await expect(alert).toContainText("l1b_no_such_field");

  const text = await copyDetails(page, test.info(), "read");
  expect(text).toMatch(/^Trackinizer web app: .*l1b_no_such_field/);
  expect(text).toContain("\npage: #/list/Paper\n");
  expect(text).toMatch(new RegExp(`\\nfailed: request method=GET path=/api/inquiries status=400 ms=\\d+ request_id=${echoed(response)} attempt=\\d+ at=`));
});

test("a write the server refuses shows Copy details with the id it echoed", async ({ page, request, allowErrors }) => {
  const id = await createIssue(request, "L1b write refused");
  // The server's 422, which the browser logs as a failed resource.
  allowErrors(failedResource(`/api/inquiries/${id}/status`, 422));
  // The status write, sent on with a value the server's schema refuses.
  await page.route(`**/api/inquiries/${id}/status`, (route) =>
    route.continue({ postData: JSON.stringify({ ...route.request().postDataJSON(), value: 5 }) }),
  );
  await page.goto(`/app/#/lookup/${id}`);
  const status = page.getByRole("complementary", { name: "Properties" }).locator('[data-field="status"] button.prop-btn');
  await expect(status).toHaveText("Active");
  const refused = page.waitForResponse((response) => response.url().endsWith(`/api/inquiries/${id}/status`));
  await status.click();
  await page.getByRole("option", { name: "Complete" }).click();
  const response = await refused;
  expect(response.status()).toBe(422);
  await expect(page.getByRole("alert")).toContainText("value");

  const text = await copyDetails(page, test.info(), "write");
  expect(text).toContain(`\npage: #/lookup/${id}\n`);
  expect(text).toMatch(
    new RegExp(`\\nfailed: request method=PUT path=/api/inquiries/${id}/status status=422 ms=\\d+ request_id=${echoed(response)} attempt=1 at=`),
  );
  // The write's own events, by route and key, and never the value sent.
  expect(text).toMatch(/ info write\.send route=setField key=[0-9a-f-]{36}\n/);
  expect(text).toMatch(/ warn write\.failed route=setField key=[0-9a-f-]{36} method=PUT /);
});

// The guard's positive control: a page error nobody allowed must fail its test.
test("the error guard fails a test whose page throws, unless the test allowed it", async ({ page, context }) => {
  test.fail();
  await page.goto("/app/");
  await expect(page.getByLabel("Signed in")).toBeVisible();
  const thrown = context.waitForEvent("weberror");
  await page.evaluate(() => {
    setTimeout(() => {
      throw new Error("l1b guard probe");
    });
  });
  await thrown;
});
