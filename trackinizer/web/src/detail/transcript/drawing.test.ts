import { expect, test } from "vitest";
import { placeholderHeight } from "./drawing";

test("a message's placeholder is its header and a row per 100 characters of each line, a folded one no taller than its fold", () => {
  expect(placeholderHeight("Done.", false)).toBe(22 + 22);
  // A line of 250 characters wraps to three rows; a blank line between paragraphs is a row.
  expect(placeholderHeight("x".repeat(250), false)).toBe(22 + 3 * 22);
  expect(placeholderHeight("First.\n\nSecond.", false)).toBe(22 + 3 * 22);
  // 75 lines and the empty one after the last newline.
  expect(placeholderHeight("Measured it.\n".repeat(75), false)).toBe(22 + 76 * 22);
  expect(placeholderHeight("Measured it.\n".repeat(75), true)).toBe(22 + 240);
});
