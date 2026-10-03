import { act, cleanup, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, expect, test, vi } from "vitest";
import { ProfileContext } from "../app/boot";
import { PROFILE } from "../detail/testing";
import { orderViews, useViews, type View } from "./views";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  localStorage.clear();
  sessionStorage.clear();
});

/** `useViews` for `email`, as a fresh page load would run it. */
function load(email = PROFILE.email) {
  const wrapper = ({ children }: { children: ReactNode }) => <ProfileContext value={{ ...PROFILE, email }}>{children}</ProfileContext>;
  return renderHook(() => useViews(), { wrapper });
}

function view(id: string, created: string, pinned = false): View {
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

test("a first visit opens an unsaved Untitled view of every agent's messages, live; its first change saves it, and later ones update it", () => {
  const { result } = load();
  expect(result.current.views).toEqual([]);
  expect(result.current.open).toMatchObject({ name: "Untitled view", pinned: false, agents: [], rooms: [], clis: [], level: 1, range: { live: true } });
  act(() => result.current.change({ ...result.current.open, agents: ["atlas-*"] }));
  act(() => result.current.change({ ...result.current.open, level: 2 }));
  expect(result.current.views).toEqual([expect.objectContaining({ name: "Untitled view", agents: ["atlas-*"], level: 2 })]);
  expect(result.current.open).toEqual(result.current.views[0]);
});

test("+ starts another Untitled view, and a reload opens the view this tab had open", () => {
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

test("a view is renamed, pinned and deleted from the rail; deleting the open one opens the first left", () => {
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

test("views are kept per signed-in email, and a stored view of another shape is left out", () => {
  const { result } = load();
  act(() => result.current.change({ ...result.current.open, agents: ["a"] }));
  expect(load("bob@example.com").result.current.views).toEqual([]);
  const key = `trackinizer.v2.console.${PROFILE.email}`;
  const stored = JSON.parse(localStorage.getItem(key)!);
  localStorage.setItem(key, JSON.stringify([...stored, { id: "x", name: 3 }, "junk"]));
  expect(load().result.current.views).toHaveLength(1);
  localStorage.setItem(key, "not json");
  expect(load().result.current.views).toEqual([]);
});

test("when the browser refuses to store, the open view still changes, unsaved", () => {
  const { result } = load();
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
    throw new DOMException("full", "QuotaExceededError");
  });
  act(() => result.current.change({ ...result.current.open, level: 4 }));
  expect(result.current.open.level).toBe(4);
  expect(result.current.views).toEqual([]);
});

test("when the browser denies storage itself, the console still opens and its view still changes, unsaved", () => {
  for (const area of ["localStorage", "sessionStorage"] as const) {
    vi.spyOn(window, area, "get").mockImplementation(() => {
      throw new DOMException("denied", "SecurityError");
    });
  }
  const { result } = load();
  act(() => result.current.change({ ...result.current.open, level: 3 }));
  expect(result.current.open.level).toBe(3);
  expect(result.current.views).toEqual([]);
});

test("a stored view whose times do not parse, or whose range ends before it starts, is left out", () => {
  const key = `trackinizer.v2.console.${PROFILE.email}`;
  const good = { ...view("good", "2026-10-01T09:00:00Z"), range: { live: false, since: "2026-10-01T09:00:00Z", until: null } };
  const window = (since: string | null, until: string | null) => ({ live: false, since, until });
  localStorage.setItem(
    key,
    JSON.stringify([
      good,
      { ...view("garbage-since", "2026-10-01T09:00:00Z"), range: window("garbage", null) },
      { ...view("garbage-until", "2026-10-01T09:00:00Z"), range: window(null, "soon") },
      { ...view("backwards", "2026-10-01T09:00:00Z"), range: window("2026-10-01T10:00:00Z", "2026-10-01T09:00:00Z") },
      view("garbage-created", "yesterday"),
    ]),
  );
  const { result } = load();
  expect(result.current.views.map(({ id }) => id)).toEqual(["good"]);
  expect(result.current.open.id).toBe("good");
});

test("a view another tab edits or deletes is neither overwritten nor brought back by this tab", () => {
  const key = `trackinizer.v2.console.${PROFILE.email}`;
  /** Store `views` as another tab does: this tab hears of it only through the `storage` event. */
  const otherTab = (views: readonly View[]) =>
    act(() => {
      localStorage.setItem(key, JSON.stringify(views));
      dispatchEvent(new StorageEvent("storage", { key }));
    });
  const stored = (): View[] => JSON.parse(localStorage.getItem(key)!);
  const { result } = load();
  act(() => result.current.change({ ...result.current.open, agents: ["a"] }));
  const id = result.current.open.id;
  otherTab([{ ...result.current.open, agents: ["z"] }]);
  act(() => result.current.change({ ...result.current.open, level: 2 }));
  expect(stored()).toEqual([expect.objectContaining({ id, agents: ["z"], level: 2 })]);
  otherTab([]);
  expect(result.current.open.id).not.toBe(id);
  act(() => result.current.change({ ...result.current.open, level: 3 }));
  expect(stored().map((one) => one.id)).not.toContain(id);
});
