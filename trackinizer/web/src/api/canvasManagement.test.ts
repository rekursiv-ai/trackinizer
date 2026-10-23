import { afterEach, expect, test, vi } from "vitest";
import { clearChat, createCanvas, hideCanvas, openPersonalSettings, pinCanvas } from "./canvasManagement";
import { stubFetch } from "./testing";

afterEach(() => vi.unstubAllGlobals());

const state = { id: "w1", name: "alpha", conversation_id: null, page: null, revision: 1, focused_instance: null, visuals: [] };

test("opening personal settings gets the caller's Artifact id", async () => {
  const sent = stubFetch(() => Response.json({ artifact_id: "artifact-id" }));
  await expect(openPersonalSettings()).resolves.toBe("artifact-id");
  expect(sent).toMatchObject([{ method: "POST", path: "/api/me/settings-artifact" }]);
});

test("hide and pin patch their flags and return the canvas", async () => {
  const sent = stubFetch(() => Response.json(state));
  await expect(hideCanvas("w1")).resolves.toEqual(state);
  await expect(pinCanvas("w1", true)).resolves.toEqual(state);
  expect(sent).toMatchObject([
    { method: "PATCH", path: "/api/canvases/w1", body: { hidden: true } },
    { method: "PATCH", path: "/api/canvases/w1", body: { pinned: true } },
  ]);
});

test("clear chat posts with its idempotency key and returns the new canvas", async () => {
  const next = { ...state, id: "w2", conversation_id: null };
  const sent = stubFetch(() => Response.json(next));
  await expect(clearChat("w1", "key-1")).resolves.toEqual(next);
  expect(sent).toMatchObject([{
    method: "POST", path: "/api/canvases/w1/clear-chat", headers: { "idempotency-key": "key-1" },
  }]);
});

test("a canvas made with a provider names it, and one made without a provider sends no body", async () => {
  const sent = stubFetch(() => Response.json(state));
  await createCanvas({ provider: "codex" });
  await createCanvas();
  expect(sent.map(({ method, path, body }) => [method, path, body])).toEqual([
    ["POST", "/api/canvases/new", { provider: "codex" }],
    ["POST", "/api/canvases/new", undefined],
  ]);
});
