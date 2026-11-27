import { afterEach, expect, test } from "vitest";
import { parseCssColor } from "./colors";

afterEach(() => {
  document.documentElement.removeAttribute("style");
});

test("parses direct CSS colors without producing non-finite channels", () => {
  expect(parseCssColor("#0af")).toEqual([0, 170 / 255, 1, 1]);
  expect(parseCssColor("rgba(255, 0, 128, 50%)")).toEqual([1, 0, 128 / 255, 0.5]);
  expect(parseCssColor("#not-a-color")).toEqual([0.5, 0.5, 0.5, 1]);
});
