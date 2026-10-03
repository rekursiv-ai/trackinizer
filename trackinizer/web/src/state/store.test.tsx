import { act, cleanup, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { ProfileContext } from "../app/boot";
import { meFilter, meNames, mePattern, useMe } from "./me";
import { storageKey, useBrowserState } from "./store";
import { EMPTY_STATE } from "./value";

const KEY = `trackinizer.v2.${location.origin}.ada@example.com`;
const PROFILE = { user_id: "u1", email: "ada@example.com", name: "Ada", role: "writer", last_login: null, visual_workspace_enabled: false };

function wrapper({ children }: { children: ReactNode }) {
  return <ProfileContext value={PROFILE}>{children}</ProfileContext>;
}

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

test("the state lives under one key per origin and email", () => {
  expect(storageKey("ada@example.com")).toBe(KEY);
});

test("a stored change shows in every hook at once, and is stored whole under the key", () => {
  const first = renderHook(() => useBrowserState(), { wrapper });
  const second = renderHook(() => useBrowserState(), { wrapper });
  expect(first.result.current[0]).toEqual(EMPTY_STATE);
  act(() => first.result.current[1]((state) => ({ ...state, stars: ["a"] })));
  expect(second.result.current[0].stars).toEqual(["a"]);
  expect(JSON.parse(localStorage.getItem(KEY)!)).toEqual({ ...EMPTY_STATE, stars: ["a"] });
});

test("another tab's change arrives through the storage event", () => {
  const { result } = renderHook(() => useBrowserState(), { wrapper });
  act(() => {
    localStorage.setItem(KEY, JSON.stringify({ ...EMPTY_STATE, aliases: ["dan"] }));
    dispatchEvent(new StorageEvent("storage", { key: KEY }));
  });
  expect(result.current[0].aliases).toEqual(["dan"]);
});

test("a change applies to what is stored when it runs, so another tab's change is kept", () => {
  const { result } = renderHook(() => useBrowserState(), { wrapper });
  // Written by another tab, with no event seen yet.
  localStorage.setItem(KEY, JSON.stringify({ ...EMPTY_STATE, stars: ["theirs"] }));
  act(() => result.current[1]((state) => ({ ...state, stars: [...state.stars, "mine"] })));
  expect(result.current[0].stars).toEqual(["theirs", "mine"]);
});

test("a stored value this build cannot read shows as empty, and is never overwritten", () => {
  const unreadable = JSON.stringify({ version: 2, stars: ["from a newer build"] });
  localStorage.setItem(KEY, unreadable);
  const { result } = renderHook(() => useBrowserState(), { wrapper });
  expect(result.current[0]).toEqual(EMPTY_STATE);
  expect(() => result.current[1]((state) => ({ ...state, stars: ["x"] }))).toThrow(/version 2/);
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
