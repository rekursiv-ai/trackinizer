import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { ProfileContext } from "../app/boot";
import { meFilter, meNames, mePattern, useMe } from "./me";
import { useBrowserState } from "./store";
import { EMPTY_STATE, storageKey } from "./value";
import { AGREED, stubFetch } from "../api/testing";
import { DEFAULT_PREFERENCES, type Preferences, receivePreferences } from "../preferences";

const KEY = `trackinizer.v2.${location.origin}.ada@example.com`;
const PROFILE = { user_id: "u1", email: "ada@example.com", name: "Ada", role: "writer", last_login: null, visual_canvas_enabled: false, ...AGREED };

function wrapper({ children }: { children: ReactNode }) {
  return <ProfileContext value={PROFILE}>{children}</ProfileContext>;
}

const VIEW = { id: "v", name: "Mine", request: { kinds: ["Issue"], filters: [{ field: "owner", op: "is" as const, value: "ada" }] } };

const patches = (sent: ReturnType<typeof stubFetch>) => sent.map(({ method, path, body }) => [method, path, body]);

/** A server that merges what it is sent over the preferences, as `PATCH /api/me/preferences` does. */
function server() {
  let kept = DEFAULT_PREFERENCES;
  return stubFetch(async (request) => {
    kept = { ...kept, ...(await request.json() as Partial<Preferences>) };
    return Response.json(kept);
  });
}

beforeEach(() => {
  localStorage.clear();
});

afterEach(async () => {
  cleanup();
  // The test's own sends answer first, or their answers would put its data back.
  await new Promise((resolve) => setTimeout(resolve, 0));
  receivePreferences(DEFAULT_PREFERENCES);
  vi.restoreAllMocks();
});

test("the tiles live under one key per origin and email", () => {
  expect(storageKey("ada@example.com")).toBe(KEY);
});

test("a change to the user's data shows in every hook at once, is sent as only its key, and is kept nowhere in the browser", async () => {
  const sent = server();
  const first = renderHook(() => useBrowserState(), { wrapper });
  const second = renderHook(() => useBrowserState(), { wrapper });
  expect(first.result.current[0]).toEqual(EMPTY_STATE);
  act(() => first.result.current[1]((state) => ({ ...state, stars: ["a"] })));
  expect(second.result.current[0].stars).toEqual(["a"]);
  await waitFor(() => expect(sent).toHaveLength(1));
  expect(patches(sent)).toEqual([["PATCH", "/api/me/preferences", { stars: ["a"] }]]);
  expect(localStorage.length).toBe(0);
});

test.each([
  ["stars", { stars: ["s"] }, { stars: ["s"] }],
  ["views", { views: [VIEW] }, { views: [VIEW] }],
  ["aliases", { aliases: ["ada"] }, { aliases: ["ada"] }],
  ["people", { people: { dan: { name: "Dan", type: "person" as const } } }, { people: { dan: { name: "Dan", type: "person" as const } } }],
  ["read", { read: { boundary: "2026-10-01T00:00:00Z", marks: ["c1"] } }, { read: { boundary: "2026-10-01T00:00:00Z", marks: ["c1"] } }],
  ["folded sections", { ui: { collapsed: ["browse"], lens: null, tiles: {} } }, { collapsed: ["browse"] }],
  ["the lens", { ui: { collapsed: [], lens: "graph", tiles: {} } }, { lens: "graph" }],
])("%s round-trip: sent under their key, found by a surface that reads the profile", async (_, part, body) => {
  const sent = server();
  const { result } = renderHook(() => useBrowserState(), { wrapper });
  act(() => result.current[1]((state) => ({ ...state, ...part })));
  await waitFor(() => expect(sent).toHaveLength(1));
  expect(sent[0]!.body).toEqual(body);
  // The next page load shows what the server answered.
  receivePreferences({ ...DEFAULT_PREFERENCES, ...body });
  expect(renderHook(() => useBrowserState(), { wrapper }).result.current[0]).toEqual({ ...EMPTY_STATE, ...part });
});

test("a change applies to the preferences as they stand when it runs, so another surface's change is kept", () => {
  server();
  const { result } = renderHook(() => useBrowserState(), { wrapper });
  // Read from the profile by another tab's refetch, with no render yet.
  receivePreferences({ ...DEFAULT_PREFERENCES, stars: ["theirs"] });
  act(() => result.current[1]((state) => ({ ...state, stars: [...state.stars, "mine"] })));
  expect(result.current[0].stars).toEqual(["theirs", "mine"]);
});

test("tiles stay in this browser: stored under the key, never sent", () => {
  const sent = server();
  const { result } = renderHook(() => useBrowserState(), { wrapper });
  const tiles = { chat: { collapsed: true, place: null } };
  act(() => result.current[1]((state) => ({ ...state, ui: { ...state.ui, tiles } })));
  expect(result.current[0].ui.tiles).toEqual(tiles);
  expect(JSON.parse(localStorage.getItem(KEY)!)).toEqual({ ...EMPTY_STATE, ui: { ...EMPTY_STATE.ui, tiles } });
  expect(sent).toEqual([]);
});

test("another tab's tiles arrive through the storage event", () => {
  const { result } = renderHook(() => useBrowserState(), { wrapper });
  const tiles = { chat: { collapsed: false, place: { left: 1, top: 2 } } };
  act(() => {
    localStorage.setItem(KEY, JSON.stringify({ ...EMPTY_STATE, ui: { ...EMPTY_STATE.ui, tiles } }));
    dispatchEvent(new StorageEvent("storage", { key: KEY }));
  });
  expect(result.current[0].ui.tiles).toEqual(tiles);
});

test("a local value this build cannot read shows no tiles, is never overwritten, and does not stop a change to the user's data", async () => {
  const sent = server();
  const unreadable = JSON.stringify({ version: 3, stars: ["from a newer build"] });
  localStorage.setItem(KEY, unreadable);
  const { result } = renderHook(() => useBrowserState(), { wrapper });
  expect(result.current[0]).toEqual(EMPTY_STATE);
  act(() => result.current[1]((state) => ({ ...state, stars: ["x"] })));
  expect(result.current[0].stars).toEqual(["x"]);
  await waitFor(() => expect(sent).toHaveLength(1));
  expect(() => result.current[1]((state) => ({ ...state, ui: { ...state.ui, tiles: { chat: { collapsed: true, place: null } } } }))).toThrow(/version 3/);
  expect(localStorage.getItem(KEY)).toBe(unreadable);
});

test("storage that cannot be read shows an empty state", () => {
  vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
    throw new DOMException("denied", "SecurityError");
  });
  const { result } = renderHook(() => useBrowserState(), { wrapper });
  expect(result.current[0]).toEqual(EMPTY_STATE);
});

test("me is the email and the ticked aliases, matched by one anchored, escaped regex", () => {
  expect(meNames("dan@example.com", ["dan", "dan@example.com", "/root"])).toEqual(["dan@example.com", "dan", "/root"]);
  expect(mePattern(["dan@example.com", "dan"])).toBe("^(dan@example\\.com|dan)$");
  // One name is still one regex, so every "me" filter has one shape.
  expect(meFilter("owner", ["ada@example.com"])).toEqual({ field: "owner", op: "re", value: "^(ada@example\\.com)$" });

  const pattern = new RegExp(mePattern(["a.b", "c+(d)", "dan"]));
  expect(["a.b", "c+(d)", "dan"].every((name) => pattern.test(name))).toBe(true);
  expect(["axb", "a.bc", "cc(d)", "dana", "xdan"].some((name) => pattern.test(name))).toBe(false);
});

test("useMe follows the aliases ticked in this browser", () => {
  const me = renderHook(() => useMe(), { wrapper });
  const state = renderHook(() => useBrowserState(), { wrapper });
  expect(me.result.current).toEqual(["ada@example.com"]);
  act(() => state.result.current[1]((value) => ({ ...value, aliases: ["ada"] })));
  expect(me.result.current).toEqual(["ada@example.com", "ada"]);
});
