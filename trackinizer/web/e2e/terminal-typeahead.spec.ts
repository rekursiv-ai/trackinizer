import type { Page } from "@playwright/test";
import type { CanvasState } from "../src/api/canvases";
import { expect, test } from "./fixtures";
import { resetCanvas } from "./canvasState";

// Local echo on a slow link. The terminal's socket here answers each keystroke 300 ms later, as a far-away host
// does; the page must draw what is typed at once, dim, and settle it when the echo comes. A prompt that echoes
// nothing must show nothing.

test.use({ canvas: true });

test.afterEach(async ({ request }) => {
  await resetCanvas(request);
});

const frame = (kind: number, payload: Buffer) => Buffer.concat([Buffer.from([kind, 0, 0, 0, payload.length]), payload]);
const ATTACH = 1;
const INPUT = 2;
const OUTPUT = 6;
const ACK = 7;
const ECHO_DELAY_MS = 300;

/** The shell behind the socket: `echo` is off at a password prompt. */
type Shell = { echo: boolean };

async function slowTerminal(page: Page, request: Parameters<typeof resetCanvas>[0], shell: Shell) {
  const id = await resetCanvas(request);
  const state = await (await request.get(`/api/canvases/${id}`)).json() as CanvasState;
  const shown = await request.post(`/api/canvases/${id}/operations`, {
    headers: { "Idempotency-Key": crypto.randomUUID() },
    data: { revision: state.revision, operation: { kind: "show", visual_type: "trax.terminal", placement: "main", params: { machine: "gx10" } } },
  });
  expect(shown.ok(), await shown.text()).toBe(true);
  await page.route("**/api/terminals/machines**", (route) => route.fulfill({
    json: { machines: [{ name: "gx10", status: "online", terminal_ready: true, confirmed: true }], terminals: [] },
  }));
  await page.route("**/api/terminals", (route) => route.fulfill({ status: 201, json: { terminal_id: crypto.randomUUID(), ticket: "t".repeat(43) } }));
  await page.routeWebSocket(/\/api\/terminals\/[0-9a-f-]{36}\/ws$/, (socket) => {
    socket.onMessage((message) => {
      const bytes = Buffer.from(message as Buffer);
      if (bytes[0] === ATTACH) {
        socket.send(frame(ACK, Buffer.alloc(8)));
        socket.send(frame(OUTPUT, Buffer.from("$ ")));
      } else if (bytes[0] === INPUT) {
        const typed = bytes.subarray(5);
        setTimeout(() => {
          if (typed.toString() === "\r") shell.echo = false;
          const answer = typed.toString() === "\r" ? Buffer.from("\r\nPassword: ") : shell.echo ? typed : null;
          if (answer) socket.send(frame(OUTPUT, answer));
        }, ECHO_DELAY_MS);
      }
    });
  });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/app/#/list/Issue");
  await page.getByRole("button", { name: "Start terminal on gx10" }).click();
  const tile = page.locator(".visual-tile").filter({ has: page.locator(".terminal-tile") });
  await expect(tile.locator(".xterm-screen")).toBeVisible();
  await expect(tile.locator(".xterm-rows")).toContainText("$");
  return tile;
}

test("what is typed shows at once, dim, and settles when the slow echo arrives", async ({ page, request }) => {
  const tile = await slowTerminal(page, request, { echo: true });
  const rows = tile.locator(".xterm-rows");
  // Five echoes teach the page the link is slow; the first also validates the row.
  for (let count = 1; count <= 6; count++) {
    await page.keyboard.press("a");
    await expect(rows).toContainText(`$ ${"a".repeat(count)}`, { timeout: 2_000 });
    await expect(rows.locator(".xterm-dim")).toHaveCount(0, { timeout: 2_000 });
  }
  const typed = Date.now();
  await page.keyboard.press("x");
  await expect(rows).toContainText("$ aaaaaax", { timeout: 150 });
  expect(Date.now() - typed).toBeLessThan(ECHO_DELAY_MS);
  await expect(rows.locator(".xterm-dim")).toHaveText("x");
  await expect(rows.locator(".xterm-dim")).toHaveCount(0, { timeout: 2_000 });
  await expect(rows).toContainText("$ aaaaaax");
  expect((await rows.innerText()).match(/x/g)).toHaveLength(1);
});

test("a prompt that echoes nothing shows nothing of what is typed", async ({ page, request }) => {
  const tile = await slowTerminal(page, request, { echo: true });
  const rows = tile.locator(".xterm-rows");
  for (let count = 1; count <= 6; count++) {
    await page.keyboard.press("a");
    await expect(rows).toContainText(`$ ${"a".repeat(count)}`, { timeout: 2_000 });
  }
  await page.keyboard.press("Enter");
  await expect(rows).toContainText("Password:", { timeout: 2_000 });
  await page.keyboard.type("hunter2");
  for (let sample = 0; sample < 14; sample++) {
    expect(await rows.innerText()).not.toMatch(/Password:\s*\S/);
    await page.waitForTimeout(50);
  }
});
