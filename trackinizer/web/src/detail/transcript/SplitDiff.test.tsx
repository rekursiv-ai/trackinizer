import { cleanup, render } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import { FileDiff } from "./SplitDiff.tsx";

afterEach(() => {
  cleanup();
});

/** A new file of six lines, as a diff of one path holds it. */
const ADDED = { path: "n.txt", head: ["--- a/n.txt", "+++ b/n.txt"], body: ["@@ -0,0 +1,6 @@", ...Array.from({ length: 6 }, (_, k) => `+line ${k}`)] };

test("past its most rows a diff draws that many and counts the rest, split or unified", () => {
  const { container, rerender } = render(<FileDiff file={ADDED} mode="split" maxRows={3} />);
  expect(container.querySelectorAll(".sd-split tbody tr")).toHaveLength(3);
  expect(container.querySelector(".sd-more")!.textContent).toBe("… 4 more rows");
  rerender(<FileDiff file={ADDED} mode="unified" maxRows={3} />);
  expect(container.querySelectorAll(".tr-diff .ln")).toHaveLength(3);
  expect(container.querySelector(".sd-more")!.textContent).toBe("… 4 more lines");
});
