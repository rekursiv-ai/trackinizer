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

test("a tab starts with no canvas", async () => {
  expect((await fresh()).currentCanvas()).toEqual({ id: null, landing: null });
});

test("a reload keeps the tab's canvas, and a new tab, with empty sessionStorage, starts with none", async () => {
  (await fresh()).setCanvas("w3", "#/graph");
  expect((await fresh()).currentCanvas()).toEqual({ id: "w3", landing: null });
  sessionStorage.clear();
  expect((await fresh()).currentCanvas().id).toBeNull();
});

test("null forgets the canvas, so a reload asks the server again", async () => {
  const store = await fresh();
  store.setCanvas("w2", "#/list/Issue");
  expect(store.currentCanvas()).toEqual({ id: "w2", landing: "#/list/Issue" });
  store.setCanvas(null);
  expect((await fresh()).currentCanvas().id).toBeNull();
});

test("the key an older build kept in localStorage is removed, not read", async () => {
  localStorage.setItem("trackinizer.station.current", "w9");
  expect((await fresh()).currentCanvas().id).toBeNull();
  expect(localStorage.getItem("trackinizer.station.current")).toBeNull();
});

test("storage that is off still switches the tab", async () => {
  vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("denied"); });
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("denied"); });
  vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => { throw new Error("denied"); });
  const store = await fresh();
  expect(store.currentCanvas().id).toBeNull();
  expect(() => store.setCanvas("w3")).not.toThrow();
  expect(store.currentCanvas().id).toBe("w3");
});

test("setting what is already shown leaves the state as it was", async () => {
  const store = await fresh();
  store.setCanvas("w1", "#/graph");
  const before = store.currentCanvas();
  store.setCanvas("w1", "#/graph");
  expect(store.currentCanvas()).toBe(before);
});


test("the pre-cutover tab keeps its canvas once, without resurrecting it after close", async () => {
  sessionStorage.setItem("trackinizer.station.tab", "old-id");
  const store = await fresh();
  expect(store.currentCanvas().id).toBe("old-id");
  expect(sessionStorage.getItem("trackinizer.canvas.tab")).toBe("old-id");
  expect(sessionStorage.getItem("trackinizer.station.tab")).toBeNull();
  store.setCanvas(null);
  expect((await fresh()).currentCanvas().id).toBeNull();
});

test("a new tab choice wins over a retained upgrade input", async () => {
  sessionStorage.setItem("trackinizer.station.tab", "old-id");
  sessionStorage.setItem("trackinizer.canvas.tab", "new-id");
  expect((await fresh()).currentCanvas().id).toBe("new-id");
  expect(sessionStorage.getItem("trackinizer.station.tab")).toBeNull();
});

test("a readable upgrade input survives storage refusing the new write", async () => {
  sessionStorage.setItem("trackinizer.station.tab", "old-id");
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("full"); });
  expect((await fresh()).currentCanvas().id).toBe("old-id");
});
