import { render, screen } from "@testing-library/react";
import { expect, test } from "vitest";
import { FileDiff, PatchView } from "./SplitDiff";
import { patchFiles } from "./splitDiff";

const ADDED = ["--- a/n.txt", "+++ b/n.txt", "@@ -0,0 +1,6 @@", ...Array.from({ length: 6 }, (_, k) => `+line ${k}`)].join("\n");

test("past its most rows a diff draws that many and counts the rest, split or unified", () => {
  const [file] = patchFiles(ADDED);
  const { container, rerender } = render(<FileDiff file={file!} mode="split" maxRows={3} />);
  expect(container.querySelectorAll(".sd-split tbody tr")).toHaveLength(3);
  expect(container.querySelector(".sd-more")!.textContent).toBe("… 4 more rows");
  rerender(<FileDiff file={file!} mode="unified" maxRows={3} />);
  expect(container.querySelectorAll(".tr-diff .ln")).toHaveLength(3);
  expect(container.querySelector(".sd-more")!.textContent).toBe("… 4 more lines");
});

test("a patch shows each file under its path, a note beside it, and says when no file differs", () => {
  const patch = "diff -ruN a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\ndiff -ruN a/w.bin b/w.bin\nBinary files a/w.bin and b/w.bin differ\n";
  const { container, unmount } = render(<PatchView patch={patch} mode="split" />);
  const files = [...container.querySelectorAll(".sd-file")];
  expect(files.map((file) => [file.querySelector(".sd-path")?.textContent, file.querySelector(".sd-note")?.textContent ?? ""])).toEqual([
    ["b/x.py", ""],
    ["b/w.bin", "Binary files a/w.bin and b/w.bin differ"],
  ]);
  expect(screen.getByRole("group", { name: "Code diff" })).toBe(container.querySelector(".sd-patch"));
  unmount();
  render(<PatchView patch="" mode="split" />);
  expect(screen.getByText("No file differs.")).toBeTruthy();
});
