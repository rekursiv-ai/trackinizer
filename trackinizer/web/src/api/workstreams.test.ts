import { afterEach, expect, test, vi } from "vitest";
import { createStation, createWorkstream, listWorkstreams, moveStation, setWorkstreamOrder, updateWorkstream, workstreamInfo } from "./workstreams";
import { stubFetch } from "./testing";

afterEach(() => vi.unstubAllGlobals());

test("workstream list carries only the supplied filters", async () => {
  const sent = stubFetch(() => Response.json([]));
  expect(await listWorkstreams({ q: "design", hidden: true, asker: "jp@example.com" })).toEqual([]);
  expect(sent).toEqual([{
    method: "GET", path: "/api/workstreams", query: "?q=design&hidden=true&asker=jp%40example.com", headers: {}, body: undefined,
  }]);
});

test("create, update, order, and info use the workstream routes and payloads", async () => {
  const sent = stubFetch(() => Response.json({ id: "ws", title: "Research", seq: 1, scratch: false, hidden: false, pinned: false }));
  await createWorkstream("Research");
  await updateWorkstream("ws", { title: "Research 2", pinned: true });
  await setWorkstreamOrder(["ws", "scratch"]);
  await workstreamInfo("ws");
  expect(sent.map(({ method, path, body }) => [method, path, body])).toEqual([
    ["POST", "/api/workstreams", { title: "Research" }],
    ["PATCH", "/api/workstreams/ws", { title: "Research 2", pinned: true }],
    ["PUT", "/api/workstreams/order", { ids: ["ws", "scratch"] }],
    ["GET", "/api/workstreams/ws", undefined],
  ]);
});

test("move and create station use their workspace routes, omitting Scratch selection", async () => {
  const sent = stubFetch(() => Response.json({ id: "station", name: "Chat", revision: 1, visuals: [] }));
  await moveStation("station", "ws");
  await createStation("ws");
  await createStation();
  expect(sent.map(({ method, path, body }) => [method, path, body])).toEqual([
    ["PUT", "/api/workspaces/station/workstream", { workstream_id: "ws" }],
    ["POST", "/api/workspaces/new", { workstream_id: "ws" }],
    ["POST", "/api/workspaces/new", undefined],
  ]);
});

test("a station made in a workstream names the provider its chat starts on, and Scratch names it alone", async () => {
  const sent = stubFetch(() => Response.json({ id: "station", name: "Chat", revision: 1, visuals: [] }));
  await createStation("ws", "claude");
  await createStation(undefined, "codex");
  expect(sent.map(({ method, path, body }) => [method, path, body])).toEqual([
    ["POST", "/api/workspaces/new", { workstream_id: "ws", provider: "claude" }],
    ["POST", "/api/workspaces/new", { provider: "codex" }],
  ]);
});
