import { afterEach, expect, test, vi } from "vitest";
import { flipped, setFlipped } from "./terminalFlip";

afterEach(() => vi.restoreAllMocks());

test("the flip is per station and remembered in storage", () => {
  setFlipped("w1", true);
  expect(flipped("w1")).toBe(true);
  expect(flipped("w2")).toBe(false);
  expect(localStorage.getItem("trackinizer.chat.flip.w1")).toBe("terminal");
  setFlipped("w1", false);
  expect(flipped("w1")).toBe(false);
});

test("storage that throws leaves the flip working for the page", () => {
  vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("blocked"); });
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("blocked"); });
  setFlipped("w9", true);
  expect(flipped("w9")).toBe(true);
});
