import { afterEach, expect, test, vi } from "vitest";

afterEach(() => {
  vi.restoreAllMocks();
  localStorage.clear();
  sessionStorage.clear();
});

/** The store as a page that has just loaded sees it, reading what the tab kept. */
async function fresh() {
  vi.resetModules();
  return import("./store");
}

test("a tab starts with no station", async () => {
  expect((await fresh()).currentStation()).toEqual({ id: null, landing: null });
});

test("a reload keeps the tab's station, and a new tab, with empty sessionStorage, starts with none", async () => {
  (await fresh()).setStation("w3", "#/graph");
  expect((await fresh()).currentStation()).toEqual({ id: "w3", landing: null });
  sessionStorage.clear();
  expect((await fresh()).currentStation().id).toBeNull();
});

test("null forgets the station, so a reload asks the server again", async () => {
  const store = await fresh();
  store.setStation("w2", "#/list/Issue");
  expect(store.currentStation()).toEqual({ id: "w2", landing: "#/list/Issue" });
  store.setStation(null);
  expect((await fresh()).currentStation().id).toBeNull();
});

test("the key an older build kept in localStorage is removed, not read", async () => {
  localStorage.setItem("trackinizer.station.current", "w9");
  expect((await fresh()).currentStation().id).toBeNull();
  expect(localStorage.getItem("trackinizer.station.current")).toBeNull();
});

test("storage that is off still switches the tab", async () => {
  vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("denied"); });
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("denied"); });
  vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => { throw new Error("denied"); });
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
