import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { setCanvasLayout } from "../api/canvasManagement";
import { stubFetch } from "../api/testing";
import { ProfileContext } from "../app/boot";
import { PROFILE } from "../detail/testing";
import { DEFAULT_PREFERENCES, type Preferences, preferences, receivePreferences } from "../preferences";
import { setCanvas } from "../canvases/store";
import { flushLayouts, forgetLayouts } from "../visuals/canvasLayout";
import type { KeptView } from "./legacyViews";
import { orderViews, useViews } from "./views";

vi.mock("../api/canvasManagement", () => ({ setCanvasLayout: vi.fn().mockResolvedValue(undefined) }));

/** The canvas the hook keeps its open view in. */
const client = new QueryClient();

function canvas(layout: unknown = null) {
  setCanvas("s1");
  client.setQueryData(["canvas", "s1"], { id: "s1", layout });
}

/** A server that merges what it is sent over the preferences, as `PATCH /api/me/preferences` does, or refuses every write. */
function server({ refuse = false } = {}) {
  let held = DEFAULT_PREFERENCES;
  return stubFetch(async (request) => {
    if (refuse) return Response.json({ detail: "refused" }, { status: 422 });
    held = { ...held, ...(await request.json() as Partial<Preferences>) };
    return Response.json(held);
  });
}

beforeEach(() => canvas());

afterEach(async () => {
  // The test's own sends answer first, or their answers would put its data back.
  await new Promise((resolve) => setTimeout(resolve, 0));
  receivePreferences(DEFAULT_PREFERENCES);
  vi.unstubAllGlobals();
  client.clear();
  setCanvas(null);
  forgetLayouts();
  cleanup();
  vi.restoreAllMocks();
  localStorage.clear();
  sessionStorage.clear();
});

/** `useViews` for `email`, as a fresh page load would run it. */
function load(email = PROFILE.email) {
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}><ProfileContext value={{ ...PROFILE, email }}>{children}</ProfileContext></QueryClientProvider>;
  return renderHook(() => useViews(), { wrapper });
}

function view(id: string, created: string, pinned = false): KeptView {
  return { id, name: id, pinned, created, agents: [], rooms: [], clis: [], level: 1, range: { live: true } };
}

test("the rail lists pinned views first, then the rest, each newest first", () => {
  const views = [
    view("old", "2026-10-01T09:00:00Z"),
    view("pinned-old", "2026-10-01T08:00:00Z", true),
    view("new", "2026-10-02T09:00:00Z"),
    view("pinned-new", "2026-10-02T08:00:00Z", true),
  ];
  expect(orderViews(views).map(({ id }) => id)).toEqual(["pinned-new", "pinned-old", "new", "old"]);
});

test("a first visit opens an unsaved Untitled view of every agent's messages, live; its first change saves it, and later ones update it", async () => {
  server();
  const { result } = load();
  expect(result.current.views).toEqual([]);
  expect(result.current.open).toMatchObject({ name: "Untitled view", pinned: false, agents: [], rooms: [], clis: [], level: 1, range: { live: true } });
  act(() => result.current.change({ ...result.current.open, agents: ["atlas-*"] }));
  await waitFor(() => expect(result.current.open).toEqual(result.current.views[0]));
  act(() => result.current.change({ ...result.current.open, level: 2 }));
  expect(result.current.views).toEqual([expect.objectContaining({ name: "Untitled view", agents: ["atlas-*"], level: 2 })]);
  await waitFor(() => expect(result.current.open).toEqual(result.current.views[0]));
});

test("a change shows at once, is sent as only console_views, and is kept nowhere in the browser", async () => {
  const sent = server();
  const first = load();
  const second = load();
  act(() => first.result.current.change({ ...first.result.current.open, agents: ["atlas-*"], rooms: ["ops"] }));
  expect(second.result.current.views).toEqual([expect.objectContaining({ agents: ["atlas-*"], rooms: ["ops"] })]);
  await waitFor(() => expect(sent).toHaveLength(1));
  expect(sent.map(({ method, path, body }) => [method, path, body])).toEqual([
    ["PATCH", "/api/me/preferences", { console_views: [expect.objectContaining({ agents: ["atlas-*"], rooms: ["ops"] })] }],
  ]);
  expect(localStorage.length).toBe(0);
});

test("+ starts another Untitled view, and a reload opens the view this tab had open", async () => {
  server();
  const { result, unmount } = load();
  act(() => result.current.change({ ...result.current.open, clis: ["claude"] }));
  const first = result.current.open.id;
  act(() => result.current.openView(null));
  expect(result.current.open).toMatchObject({ name: "Untitled view", clis: [] });
  expect(result.current.open.id).not.toBe(first);
  act(() => result.current.change({ ...result.current.open, level: 3 }));
  // Pinned, the second view lists first, so only the tab's own memory opens the first.
  act(() => result.current.edit(result.current.open.id, (view) => ({ ...view, pinned: true })));
  act(() => result.current.openView(first));
  unmount();
  expect(load().result.current.open).toMatchObject({ id: first, clis: ["claude"] });
});

test("a view is renamed, pinned and deleted from the rail; deleting the open one opens the first left", async () => {
  server();
  const { result } = load();
  act(() => result.current.change({ ...result.current.open, agents: ["a"] }));
  const a = result.current.open.id;
  act(() => result.current.openView(null));
  act(() => result.current.change({ ...result.current.open, agents: ["b"] }));
  const b = result.current.open.id;
  act(() => result.current.edit(a, (one) => ({ ...one, name: "Live leads", pinned: true })));
  expect(result.current.views.map(({ id, name, pinned }) => [id, name, pinned])).toEqual([
    [a, "Live leads", true],
    [b, "Untitled view", false],
  ]);
  act(() => result.current.edit(b, (one) => ({ ...one, name: "tiles" })));
  expect(result.current.open).toMatchObject({ id: b, name: "tiles" });
  act(() => result.current.remove(b));
  expect(result.current.views.map(({ id }) => id)).toEqual([a]);
  expect(result.current.open.id).toBe(a);
  act(() => result.current.remove(a));
  expect(result.current.open).toMatchObject({ name: "Untitled view", agents: [] });
});

test("views are the signed-in user's, read from the preferences the server sent", () => {
  const kept = view("kept", "2026-10-01T09:00:00Z");
  receivePreferences({ ...DEFAULT_PREFERENCES, console_views: [kept] });
  expect(load().result.current.views).toEqual([kept]);
  expect(load("bob@example.com").result.current.open.id).toBe(kept.id);
  receivePreferences({ ...DEFAULT_PREFERENCES, console_views: [] });
  expect(load().result.current.views).toEqual([]);
});

test("when the server refuses a change, the open view still shows it, unsaved, and the rail lists nothing", async () => {
  server({ refuse: true });
  const { result } = load();
  act(() => result.current.change({ ...result.current.open, level: 4 }));
  await waitFor(() => expect(result.current.views).toEqual([]));
  expect(result.current.open.level).toBe(4);
  expect(preferences().console_views).toEqual([]);
});

test("when the browser denies sessionStorage, the console still opens and its view still changes", async () => {
  server();
  vi.spyOn(window, "sessionStorage", "get").mockImplementation(() => {
    throw new DOMException("denied", "SecurityError");
  });
  const { result } = load();
  act(() => result.current.change({ ...result.current.open, level: 3 }));
  expect(result.current.open.level).toBe(3);
  await waitFor(() => expect(result.current.views).toHaveLength(1));
});

test("a view another tab edits or deletes is neither overwritten nor brought back by this tab", async () => {
  const sent = server();
  /** The preferences frame another tab's change brings to this one. */
  const otherTab = (views: KeptView[]) => act(() => receivePreferences({ ...DEFAULT_PREFERENCES, console_views: views }));
  const { result } = load();
  act(() => result.current.change({ ...result.current.open, agents: ["a"] }));
  const id = result.current.open.id;
  await waitFor(() => expect(sent).toHaveLength(1));
  await act(() => new Promise((resolve) => setTimeout(resolve, 0)));
  otherTab([{ ...view(id, result.current.open.created), agents: ["z"] }]);
  act(() => result.current.change({ ...result.current.open, level: 2 }));
  expect(preferences().console_views).toEqual([expect.objectContaining({ id, agents: ["z"], level: 2 })]);
  await act(() => new Promise((resolve) => setTimeout(resolve, 0)));
  otherTab([]);
  expect(result.current.open.id).not.toBe(id);
  act(() => result.current.change({ ...result.current.open, level: 3 }));
  expect(preferences().console_views.map((one) => one.id)).not.toContain(id);
});

test("the open view is kept in the canvas's layout, so another browser opens the same one", () => {
  server();
  const { result } = load();
  act(() => result.current.change({ ...result.current.open, clis: ["claude"] }));
  const id = result.current.open.id;
  flushLayouts();
  expect(setCanvasLayout).toHaveBeenLastCalledWith("s1", { console: id });
  // A browser that never saw the id reads it from the canvas.
  forgetLayouts();
  canvas({ console: id });
  expect(load().result.current.open.id).toBe(id);
});

test("a tab that kept its open view in sessionStorage hands it to the canvas once, and forgets it", () => {
  sessionStorage.setItem("trackinizer.v2.console.open", "old-view");
  load();
  expect(setCanvasLayout).toHaveBeenCalledWith("s1", { console: "old-view" });
  expect(sessionStorage.getItem("trackinizer.v2.console.open")).toBeNull();
});

test("a canvas that holds an open view keeps it over the one a tab kept", () => {
  canvas({ console: "kept" });
  sessionStorage.setItem("trackinizer.v2.console.open", "old-view");
  load();
  expect(setCanvasLayout).not.toHaveBeenCalled();
  expect(sessionStorage.getItem("trackinizer.v2.console.open")).toBeNull();
});
