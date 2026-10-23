import { afterEach, expect, test, vi } from "vitest";
import { createCanvas, createWorkspace, listWorkspaces, moveCanvas, setWorkspaceOrder, updateWorkspace, workspaceInfo } from "./workspaces";
import { stubFetch } from "./testing";

afterEach(() => vi.unstubAllGlobals());

test("workspace list carries only the supplied filters", async () => {
  const sent = stubFetch(() => Response.json([]));
  expect(await listWorkspaces({ q: "design", hidden: true, asker: "jp@example.com" })).toEqual([]);
  expect(sent).toEqual([{
    method: "GET", path: "/api/workspaces", query: "?q=design&hidden=true&asker=jp%40example.com", headers: {}, body: undefined,
  }]);
});

test("create, update, order, and info use the workspace routes and payloads", async () => {
  const sent = stubFetch(() => Response.json({ id: "ws", title: "Research", seq: 1, scratch: false, hidden: false, pinned: false }));
  await createWorkspace("Research");
  await updateWorkspace("ws", { title: "Research 2", pinned: true });
  await setWorkspaceOrder(["ws", "scratch"]);
  await workspaceInfo("ws");
  expect(sent.map(({ method, path, body }) => [method, path, body])).toEqual([
    ["POST", "/api/workspaces", { title: "Research" }],
    ["PATCH", "/api/workspaces/ws", { title: "Research 2", pinned: true }],
    ["PUT", "/api/workspaces/order", { ids: ["ws", "scratch"] }],
    ["GET", "/api/workspaces/ws", undefined],
  ]);
});

test("move and create canvas use their canvas routes, omitting Scratch selection", async () => {
  const sent = stubFetch(() => Response.json({ id: "canvas", name: "Chat", revision: 1, visuals: [] }));
  await moveCanvas("canvas", "ws");
  await createCanvas("ws");
  await createCanvas();
  expect(sent.map(({ method, path, body }) => [method, path, body])).toEqual([
    ["PUT", "/api/canvases/canvas/workspace", { workspace_id: "ws" }],
    ["POST", "/api/canvases/new", { workspace_id: "ws" }],
    ["POST", "/api/canvases/new", undefined],
  ]);
});

test("a canvas made in a workspace names the provider its chat starts on, and Scratch names it alone", async () => {
  const sent = stubFetch(() => Response.json({ id: "canvas", name: "Chat", revision: 1, visuals: [] }));
  await createCanvas("ws", "claude");
  await createCanvas(undefined, "codex");
  expect(sent.map(({ method, path, body }) => [method, path, body])).toEqual([
    ["POST", "/api/canvases/new", { workspace_id: "ws", provider: "claude" }],
    ["POST", "/api/canvases/new", { provider: "codex" }],
  ]);
});
