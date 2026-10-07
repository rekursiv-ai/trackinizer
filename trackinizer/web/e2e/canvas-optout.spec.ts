import { expect, test } from "./fixtures";

// A user who opts out of the canvas keeps the tab's older stream, /api/web/subscribe,
// and no canvas: the one path the other specs, which run with the default canvas
// on, do not take.

test("a user who opted out of the canvas has /api/web/subscribe, no canvas and no workspace stream", async ({ page, request }) => {
  const optedOut = await request.put("/api/me/visual-workspace", { data: { enabled: false } });
  expect(optedOut.ok(), await optedOut.text()).toBe(true);
  try {
    await page.addInitScript(() => {
      const Original = window.EventSource;
      const urls: string[] = [];
      Object.assign(window, { openedStreams: urls });
      window.EventSource = class extends Original {
        constructor(url: string | URL, init?: EventSourceInit) {
          super(url, init);
          urls.push(new URL(url, location.href).pathname.replace(/[0-9a-f-]{36}/, "<id>"));
        }
      };
    });
    const subscribed = page.waitForResponse((response) => response.url().includes("/api/web/subscribe"));
    const workspaces: string[] = [];
    page.on("request", (sent) => {
      if (new URL(sent.url()).pathname.startsWith("/api/workspaces")) workspaces.push(sent.url());
    });
    await page.goto("/app/#/list/Issue");
    await subscribed;
    await expect(page.getByRole("heading", { level: 1, name: "Issues" })).toBeVisible();
    await expect(page.getByRole("toolbar", { name: "Canvas controls" })).toHaveCount(0);
    expect(await page.evaluate(() => (window as unknown as { openedStreams: string[] }).openedStreams)).toEqual(["/api/web/subscribe"]);
    expect(workspaces).toEqual([]);
  } finally {
    // The next spec's fixture sets its own preference.
  }
});

test("turning the canvas back on in Settings moves the tab to the canvas's one stream", async ({ page, request }) => {
  const optedOut = await request.put("/api/me/visual-workspace", { data: { enabled: false } });
  expect(optedOut.ok(), await optedOut.text()).toBe(true);
  try {
    await page.goto("/app/#/settings");
    await page.getByRole("checkbox", { name: "Enable agent-guided canvas" }).click();
    await expect(page.getByRole("checkbox", { name: "Enable agent-guided canvas" })).toBeChecked();
    await page.goto("/app/#/list/Issue");
    await expect(page.getByRole("toolbar", { name: "Canvas controls" })).toBeVisible();
  } finally {
    // The next spec's fixture sets its own preference.
  }
});
