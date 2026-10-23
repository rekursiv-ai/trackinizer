import { act, cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { setCanvasPage } from "../api/canvasManagement";
import { PAGE_EVERY_MS, usePageSync } from "./usePageSync";
import { setCanvas } from "./store";

vi.mock("../api/canvasManagement", () => ({ setCanvasPage: vi.fn() }));

function Sync() {
  usePageSync();
  return null;
}

const go = (hash: string) => act(() => {
  history.replaceState(null, "", hash);
  dispatchEvent(new HashChangeEvent("hashchange"));
});
const wait = (ms: number) => act(() => { vi.advanceTimersByTime(ms); });
const sent = () => vi.mocked(setCanvasPage).mock.calls;

beforeEach(() => {
  vi.useFakeTimers();
  vi.mocked(setCanvasPage).mockResolvedValue(undefined);
  history.replaceState(null, "", "#/graph");
  setCanvas("w1");
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.clearAllMocks();
  setCanvas(null);
  history.replaceState(null, "", "#/");
});

test("the page the tab shows is written to its canvas", () => {
  render(<Sync />);
  expect(sent()).toEqual([["w1", "#/graph"]]);
});

test("a page that follows within 500 ms waits for the time to be up, and then only the newest is written", () => {
  render(<Sync />);
  go("#/list/Issue");
  go("#/list/Paper");
  expect(sent()).toHaveLength(1);
  wait(PAGE_EVERY_MS - 1);
  expect(sent()).toHaveLength(1);
  wait(1);
  expect(sent()).toEqual([["w1", "#/graph"], ["w1", "#/list/Paper"]]);
  // Not again for the same page.
  wait(PAGE_EVERY_MS * 2);
  expect(sent()).toHaveLength(2);
});

test("a sheet over the page, Settings or Admin, is not written: the canvas keeps the page under it", () => {
  render(<Sync />);
  wait(PAGE_EVERY_MS);
  go("#/settings");
  wait(PAGE_EVERY_MS);
  go("#/admin");
  wait(PAGE_EVERY_MS);
  expect(sent()).toEqual([["w1", "#/graph"]]);
});

test("a page after a quiet half second is written at once", () => {
  render(<Sync />);
  wait(PAGE_EVERY_MS);
  go("#/activity");
  expect(sent().at(-1)).toEqual(["w1", "#/activity"]);
});

test("a page the server would refuse is not written", () => {
  render(<Sync />);
  wait(PAGE_EVERY_MS);
  go("#top");
  wait(PAGE_EVERY_MS * 2);
  expect(sent()).toEqual([["w1", "#/graph"]]);
});

test("a switch that moves the page writes nothing to the canvas it enters until the address is there, and then nothing for it", () => {
  render(<Sync />);
  wait(PAGE_EVERY_MS);
  vi.mocked(setCanvasPage).mockClear();
  act(() => setCanvas("w2", "#/list/Issue"));
  // The address has not moved yet: the page being left is not the new canvas's.
  wait(PAGE_EVERY_MS * 2);
  expect(setCanvasPage).not.toHaveBeenCalled();
  go("#/list/Issue");
  wait(PAGE_EVERY_MS * 2);
  expect(setCanvasPage).not.toHaveBeenCalled();
  // After the landing, the canvas's page follows the tab again.
  go("#/activity");
  expect(setCanvasPage).toHaveBeenCalledWith("w2", "#/activity");
});

test("a write that fails is dropped and does not stop the next", async () => {
  vi.mocked(setCanvasPage).mockRejectedValueOnce(new Error("down"));
  render(<Sync />);
  await act(async () => { await Promise.resolve(); });
  wait(PAGE_EVERY_MS);
  go("#/activity");
  expect(sent().at(-1)).toEqual(["w1", "#/activity"]);
});
