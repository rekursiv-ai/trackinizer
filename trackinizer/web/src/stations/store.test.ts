import { afterEach, expect, test, vi } from "vitest";

afterEach(() => {
  vi.restoreAllMocks();
  localStorage.clear();
});

/** The store as a page that has just loaded sees it, reading what the browser kept. */
async function fresh() {
  vi.resetModules();
  return import("./store");
}

test("a tab starts with the station it kept as the last opened, or none", async () => {
  expect((await fresh()).currentStation()).toEqual({ id: null, landing: null });
  localStorage.setItem("trackinizer.station.current", "w9");
  expect((await fresh()).currentStation()).toEqual({ id: "w9", landing: null });
});

test("a switch is kept in the browser with the page it was sent to, and null forgets it", async () => {
  const store = await fresh();
  store.setStation("w2", "#/list/Issue");
  expect(store.currentStation()).toEqual({ id: "w2", landing: "#/list/Issue" });
  expect(localStorage.getItem("trackinizer.station.current")).toBe("w2");
  store.setStation(null);
  expect(localStorage.getItem("trackinizer.station.current")).toBeNull();
  expect(store.currentStation()).toEqual({ id: null, landing: null });
});

test("storage that is off still switches the tab", async () => {
  vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("denied"); });
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("denied"); });
  const store = await fresh();
  expect(store.currentStation().id).toBeNull();
  expect(() => store.setStation("w3")).not.toThrow();
  expect(store.currentStation().id).toBe("w3");
});

test("setting what is already shown leaves the state as it was", async () => {
  const store = await fresh();
  store.setStation("w1", "#/graph");
  const before = store.currentStation();
  store.setStation("w1", "#/graph");
  expect(store.currentStation()).toBe(before);
});
