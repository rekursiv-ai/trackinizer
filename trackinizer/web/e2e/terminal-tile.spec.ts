import type { Page } from "@playwright/test";
import type { WorkspaceState } from "../src/api/workspaces";
import { expect, test } from "./fixtures";
import { resetCanvas } from "./canvasState";

// The terminal tile on the canvas, against a server that opens no shell: the --no-auth server this suite
// runs refuses every terminal, so the page's four terminal calls are answered here and the socket is a
// script that gives the attach its ACK and one line of output. What is measured is the layout.

test.use({ canvas: true });

test.afterEach(async ({ request }) => {
  await resetCanvas(request);
});

const frame = (kind: number, payload: Buffer) => Buffer.concat([Buffer.from([kind, 0, 0, 0, payload.length]), payload]);

async function startedTile(page: Page, request: Parameters<typeof resetCanvas>[0]) {
  const id = await resetCanvas(request);
  const state = await (await request.get(`/api/workspaces/${id}`)).json() as WorkspaceState;
  const shown = await request.post(`/api/workspaces/${id}/operations`, {
    headers: { "Idempotency-Key": crypto.randomUUID() },
    data: { revision: state.revision, operation: { kind: "show", visual_type: "trax.terminal", placement: "main", params: { machine: "gx10" } } },
  });
  expect(shown.ok(), await shown.text()).toBe(true);
  await page.route("**/api/terminals/machines**", (route) => route.fulfill({
    json: { machines: [{ name: "gx10", status: "online", terminal_ready: true, confirmed: true }], terminals: [] },
  }));
  await page.route("**/api/terminals", (route) => route.fulfill({ status: 201, json: { terminal_id: crypto.randomUUID(), ticket: "t".repeat(43) } }));
  await page.routeWebSocket(/\/api\/terminals\/[0-9a-f-]{36}\/ws$/, (socket) => {
    socket.onMessage(() => {
      socket.send(frame(7, Buffer.alloc(8)));
      socket.send(frame(6, Buffer.from("$ ")));
    });
  });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/app/#/list/Issue");
  await page.getByRole("button", { name: "Start terminal on gx10" }).click();
  const tile = page.locator(".visual-tile").filter({ has: page.locator(".terminal-tile") });
  await expect(tile.locator(".xterm-screen")).toBeVisible();
  return tile;
}

test("the terminal fills its tile, however tall the tile is", async ({ page, request }) => {
  const tile = await startedTile(page, request);
  // What is left over is the maximize row, the screen's padding and under one row of the font's height.
  await expect.poll(async () => {
    const outer = (await tile.boundingBox())!;
    const screen = (await tile.locator(".xterm-screen").boundingBox())!;
    return outer.height - screen.height;
  }).toBeLessThan(90);
});

test("the maximize button stands clear of the screen's text", async ({ page, request }) => {
  const tile = await startedTile(page, request);
  await tile.getByRole("region", { name: "Terminal" }).hover();
  const button = (await tile.getByRole("button", { name: "Maximize terminal" }).boundingBox())!;
  const screen = (await tile.locator(".xterm-screen").boundingBox())!;
  expect(button.y + button.height).toBeLessThanOrEqual(screen.y);
});

test("New terminal opens a second shell in its own tab, and switching shows each screen", async ({ page, request }) => {
  const tile = await startedTile(page, request);
  await tile.getByRole("button", { name: "New terminal" }).click();
  await expect(tile.getByRole("tab")).toHaveCount(2);
  await expect(tile.locator(".xterm-screen:visible")).toHaveCount(1);
  await tile.getByRole("tab", { name: "Terminal 1" }).click();
  await expect(tile.locator(".xterm-screen:visible")).toHaveCount(1);
  await expect(tile.getByRole("tab", { name: "Terminal 1" })).toHaveAttribute("aria-selected", "true");
});
