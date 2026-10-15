import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { type DiffMode, FileDiff, PatchView } from "./SplitDiff";
import { patchFiles } from "./splitDiff";
import { THIRTY, TWO_HUNKS } from "./testing";

afterEach(() => {
  cleanup();
});

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

/** A diff of `TWO_HUNKS`' file, whose text a press reads from `read`; each test's own query cache. */
function expandable(mode: DiffMode, read: () => Promise<string>, body: readonly string[] = TWO_HUNKS) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const source = { key: ["file", mode, body.length], read };
  return render(
    <QueryClientProvider client={client}>
      <FileDiff file={{ path: "b/n.txt", head: [], body }} mode={mode} language="" source={source} />
    </QueryClientProvider>,
  );
}

/** The expand button named `label`; a selector, since role queries over a diff's table are these tests' slowest part. */
function expander(label: string): HTMLButtonElement {
  return [...document.querySelectorAll<HTMLButtonElement>(".sd-expand")].find((button) => button.textContent!.endsWith(label))!;
}

/** A split diff's rows: an expander's text, a hunk header alone, else each side's number and line. */
function rows(container: HTMLElement): string[][] {
  return [...container.querySelectorAll(".sd-split tbody tr")].map((tr) =>
    tr.classList.contains("sd-gap") ? [`gap: ${tr.textContent}`] : [...tr.querySelectorAll("td")].map((cell) => cell.textContent ?? ""),
  );
}

test("split, a row above each hunk and below the last shows the unchanged lines the diff left out, the file read once, both sides numbered", async () => {
  const read = vi.fn(() => Promise.resolve(`${THIRTY.join("\n")}\n`));
  const { container } = expandable("split", read);
  expect(rows(container).filter(([cell]) => cell!.startsWith("gap: "))).toEqual([["gap: ↑Expand up5 unchanged lines"], ["gap: ↕Expand all9 unchanged lines"], ["gap: ↓Expand down"]]);
  expect(read).not.toHaveBeenCalled();
  fireEvent.click(expander("Expand up"));
  // The whole top shows: five lines, fewer than one press shows, so its row goes.
  await waitFor(() => expect(rows(container)[0]).toEqual(["@@ -1,11 +1,12 @@"]));
  expect(rows(container).slice(1, 3)).toEqual([
    ["1", " line 1", "1", " line 1"],
    ["2", " line 2", "2", " line 2"],
  ]);
  fireEvent.click(expander("Expand all"));
  fireEvent.click(expander("Expand down"));
  // The middle joins the two hunks; below, the file's last two lines, numbered 28 and 29 on the old side.
  expect(rows(container).filter(([cell]) => cell!.startsWith("@@") || cell!.startsWith("gap: "))).toEqual([["@@ -1,29 +1,30 @@"]]);
  expect(rows(container).slice(-2)).toEqual([
    ["28", " line 29", "29", " line 29"],
    ["29", " line 30", "30", " line 30"],
  ]);
  expect(read).toHaveBeenCalledTimes(1);
});

test("unified, the rows show the same lines, each numbered in the new file, twenty at a time where a gap hides more", async () => {
  const body = ["@@ -1 +1 @@", "-a", "+b", "@@ -40 +40 @@", "-c", "+d"];
  const file = ["b", ...Array.from({ length: 38 }, (_, k) => `u${k + 2}`), "d"];
  expandable("unified", () => Promise.resolve(file.join("\n")), body);
  expect(screen.getByText("38 unchanged lines")).toBeTruthy();
  // Below the second hunk lies the file's end, unknown until the file is read.
  fireEvent.click(screen.getAllByRole("button", { name: "Expand down" })[0]!);
  await screen.findByText("18 unchanged lines");
  const numbered = [...document.querySelectorAll(".tr-diff .ln")].map((line) => [line.getAttribute("data-n"), line.textContent]);
  expect(numbered.slice(0, 5)).toEqual([[null, "@@ -1,21 +1,21 @@"], ["1", "-a"], ["1", "+b"], ["2", " u2"], ["3", " u3"]]);
  expect(numbered.slice(22, 24)).toEqual([["21", " u21"], [null, "@@ -40 +40 @@"]]);
  // Eighteen left, fewer than a press shows: one press shows them all, joining the hunks.
  expect(screen.queryByRole("button", { name: "Expand up" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Expand all" }));
  expect(screen.queryByText(/unchanged lines/)).toBeNull();
  expect(document.querySelectorAll(".tr-diff")).toHaveLength(1);
  expect(screen.queryAllByRole("button")).toEqual([]);
});

test("a read that fails says so with Retry, and the rows wait for it; a diff cut inside a hunk, or one with no source, has no rows to expand", async () => {
  let fail = true;
  const read = vi.fn(() => (fail ? Promise.reject(new Error("store down")) : Promise.resolve(THIRTY.join("\n"))));
  const { container, unmount } = expandable("split", read);
  fireEvent.click(expander("Expand up"));
  const failure = await screen.findByRole("alert");
  expect(failure.textContent).toContain("store down");
  expect(expander("Expand all").disabled).toBe(true);
  fail = false;
  fireEvent.click(within(failure).getByRole("button", { name: "Retry" }));
  await waitFor(() => expect(rows(container)[1]).toEqual(["1", " line 1", "1", " line 1"]));
  unmount();
  const cut = expandable("split", read, TWO_HUNKS.slice(0, -2));
  expect(cut.container.querySelector(".sd-gap")).toBeNull();
  cut.unmount();
  const [file] = patchFiles(ADDED);
  const { container: plain } = render(<FileDiff file={file!} mode="unified" />);
  expect(plain.querySelector(".sd-gap")).toBeNull();
});
