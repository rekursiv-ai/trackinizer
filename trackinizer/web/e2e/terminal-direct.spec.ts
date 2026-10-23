import type { Page } from "@playwright/test";
import type { CanvasState } from "../src/api/canvases";
import { expect, test } from "./fixtures";
import { resetCanvas } from "./canvasState";

// The direct link, with the host played from both sides: the server's relay socket is a script, and the page's
// RTCPeerConnection is replaced by one whose data channel answers like a host. What is measured is the page: it
// offers a channel over the relay, takes output and input by the channel once it is up, says so, and on a drop
// carries on over the relay and says that.

test.use({ canvas: true });

test.afterEach(async ({ request }) => {
  await resetCanvas(request);
});

const frame = (kind: number, payload: Buffer) => Buffer.concat([Buffer.from([kind, 0, 0, 0, payload.length]), payload]);
const ATTACH = 1;
const INPUT = 2;
const OUTPUT = 6;
const ACK = 7;
const SIGNAL = 9;
const ROUTE = 10;

type Relay = { readonly input: string[]; readonly routes: number[] };

async function directTerminal(page: Page, request: Parameters<typeof resetCanvas>[0], relay: Relay) {
  const id = await resetCanvas(request);
  const state = await (await request.get(`/api/canvases/${id}`)).json() as CanvasState;
  const shown = await request.post(`/api/canvases/${id}/operations`, {
    headers: { "Idempotency-Key": crypto.randomUUID() },
    data: { revision: state.revision, operation: { kind: "show", visual_type: "trax.terminal", placement: "main", params: { machine: "gx10" } } },
  });
  expect(shown.ok(), await shown.text()).toBe(true);
  await page.addInitScript(() => {
    const frameOf = (kind: number, payload: Uint8Array) => {
      const bytes = new Uint8Array(5 + payload.length);
      bytes[0] = kind;
      new DataView(bytes.buffer).setUint32(1, payload.length);
      bytes.set(payload, 5);
      return bytes.buffer;
    };
    const text = (value: string) => new TextEncoder().encode(value);
    type Fake = { channel: FakeChannel | null; config: unknown };
    class FakeChannel {
      binaryType = "";
      readyState = "open";
      onopen: (() => void) | null = null;
      onmessage: ((event: { data: ArrayBuffer }) => void) | null = null;
      onclose: (() => void) | null = null;
      private reply(kind: number, payload: Uint8Array) { setTimeout(() => this.onmessage?.({ data: frameOf(kind, payload) }), 1); }
      // The host: the route is granted from the offset asked for, the fence is answered, and typing is echoed.
      send(data: Uint8Array) {
        if (data[0] === 10 && data[5] === 1) {
          this.reply(7, data.slice(6, 14));
          this.reply(6, text("direct-on\r\n"));
          this.reply(10, new Uint8Array([1, 0, 0, 0, 0, 0, 0, 0, 0]));
        } else if (data[0] === 2) {
          this.reply(6, data.slice(5));
        }
      }
    }
    const fake: Fake = { channel: null, config: null };
    (window as unknown as { __direct: Fake }).__direct = fake;
    class FakePeer {
      iceGatheringState = "complete";
      connectionState = "new";
      localDescription = { sdp: "offer-sdp" };
      onicegatheringstatechange = null;
      onconnectionstatechange = null;
      constructor(config: unknown) { fake.config = config; }
      createDataChannel() { fake.channel = new FakeChannel(); return fake.channel; }
      createOffer() { return Promise.resolve({ type: "offer", sdp: "offer-sdp" }); }
      setLocalDescription() { return Promise.resolve(); }
      setRemoteDescription() { setTimeout(() => fake.channel?.onopen?.(), 1); return Promise.resolve(); }
      close() {}
    }
    (window as unknown as { RTCPeerConnection: unknown }).RTCPeerConnection = FakePeer;
  });
  await page.route("**/api/terminals/machines**", (route) => route.fulfill({
    json: { machines: [{ name: "gx10", status: "online", terminal_ready: true, confirmed: true }], terminals: [] },
  }));
  await page.route("**/api/terminals", (route) => route.fulfill({
    status: 201, json: { terminal_id: crypto.randomUUID(), ticket: "t".repeat(43), ice_servers: ["stun:s.test:3478"] },
  }));
  await page.routeWebSocket(/\/api\/terminals\/[0-9a-f-]{36}\/ws$/, (socket) => {
    socket.onMessage((message) => {
      const bytes = Buffer.from(message as Buffer);
      if (bytes[0] === ATTACH) {
        socket.send(frame(ACK, Buffer.alloc(8)));
        socket.send(frame(OUTPUT, Buffer.from("$ ")));
      } else if (bytes[0] === SIGNAL) {
        const offer = JSON.parse(bytes.subarray(5).toString()) as { gen: number };
        socket.send(frame(SIGNAL, Buffer.from(JSON.stringify({ gen: offer.gen, type: "answer", sdp: "answer-sdp" }))));
      } else if (bytes[0] === ROUTE) {
        relay.routes.push(bytes[5]!);
        // A return to the relay: the host resumes from the offset the page reached.
        if (bytes[5] === 0) {
          socket.send(frame(ACK, bytes.subarray(6, 14)));
          socket.send(frame(OUTPUT, Buffer.from("relay-back\r\n")));
        }
      } else if (bytes[0] === INPUT) {
        relay.input.push(bytes.subarray(5).toString());
        socket.send(frame(OUTPUT, bytes.subarray(5)));
      }
    });
  });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/app/#/list/Issue");
  await page.getByRole("button", { name: "Start terminal on gx10" }).click();
  const tile = page.locator(".visual-tile").filter({ has: page.locator(".terminal-tile") });
  await expect(tile.locator(".xterm-screen")).toBeVisible();
  return tile;
}

test("the page goes direct, types over the channel, and carries on over the relay when the channel drops", async ({ page, request }) => {
  const relay: Relay = { input: [], routes: [] };
  const tile = await directTerminal(page, request, relay);
  const rows = tile.locator(".xterm-rows");

  await expect(tile.getByText("Direct link", { exact: true })).toBeVisible();
  await expect(rows).toContainText("direct-on");
  expect(await page.evaluate(() => (window as unknown as { __direct: { config: unknown } }).__direct.config)).toEqual({
    iceServers: [{ urls: ["stun:s.test:3478"] }],
  });

  await page.keyboard.type("ab");
  await expect(rows).toContainText("ab");
  expect(relay.input).toEqual([]);

  await page.evaluate(() => (window as unknown as { __direct: { channel: { onclose: () => void } } }).__direct.channel.onclose());
  await expect(tile.getByText("Direct link lost; using the server.")).toBeVisible();
  await expect(tile.getByText("Direct link", { exact: true })).toHaveCount(0);
  await expect(rows).toContainText("relay-back");
  expect(relay.routes).toEqual([1, 0]);

  await page.keyboard.type("c");
  await expect.poll(() => relay.input).toEqual(["c"]);
});
