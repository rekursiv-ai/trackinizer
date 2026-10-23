import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeAll, expect, test, vi } from "vitest";
import { stubFetch } from "../api/testing";
import type { CanvasState } from "../api/canvases";
import { Files } from "./Files";
import type { FilesDoc } from "./filesDoc";
import { answerFiles, canvasActions, FILES, FILES_RECORD, filesRow, TREE_DIFF } from "./testing";
import { type CanvasActions, CanvasActionsProvider } from "./canvasActions";

type Params = CanvasState["visuals"][number]["params"];

// The highlighter is a chunk of its own; loaded here, its first evaluation is not
// counted against a test's 100 ms.
beforeAll(() => import("../markdown/highlight"));

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  // The diff layout a test chose is kept for the browser; the next test starts at split.
  localStorage.removeItem("trax.diff-mode");
});

/** A diff of one changed line of `path`. */
const diffOf = (path: string) => `--- a/${path}\n+++ b/${path}\n@@ -1 +1 @@\n-old\n+new\n`;

/** A tree under `pkg/`: two changes named `bar.py` in two directories, one file deleted outside it. */
const NESTED: FilesDoc = {
  ...FILES,
  files: ["README.md", "docs/guide.md", "pkg/foo/bar.py", "pkg/foo/baz.txt", "pkg/foobar.py", "pkg/lib/bar.py"],
  changed: [
    { path: "gone/old.py", change: "deleted", diff: "", note: "omitted" },
    { path: "pkg/foo/bar.py", change: "modified", diff: diffOf("pkg/foo/bar.py"), note: "" },
    { path: "pkg/lib/bar.py", change: "added", diff: diffOf("pkg/lib/bar.py"), note: "" },
  ],
};

/** Serve the files reads with `rows`; anything else answers 404. */
function serve(rows: readonly unknown[] = [filesRow(FILES)]) {
  return stubFetch((request) => answerFiles(new URL(request.url), rows) ?? Response.json({ detail: "not found" }, { status: 404 }));
}

/** Show the files visual on `record` with `params`, in a canvas that can do `actions`; `rerender` shows it with other params. */
function mount({
  record = FILES_RECORD,
  params = {},
  actions = null,
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } }),
}: { record?: string | null; params?: Params; actions?: CanvasActions | null; client?: QueryClient } = {}) {
  const pane = (shown: Params) => {
    const instance = { id: "files pane", type: "trax.files", version: 1, placement: "main" as const, record_id: record, params: shown };
    const canvas = { id: "w", revision: 1, visuals: [instance] } as unknown as CanvasState;
    return (
      <QueryClientProvider client={client}>
        <CanvasActionsProvider value={actions}>
          <Files instance={instance} canvas={canvas} focused onCanvasChanged={vi.fn()} />
        </CanvasActionsProvider>
      </QueryClientProvider>
    );
  };
  const view = render(pane(params));
  return { rerender: (shown: Params) => view.rerender(pane(shown)) };
}

/** The pane once its document is read. */
const loaded = () => screen.findByLabelText("Filter by path");

/** Each changed file's line: its path and how it changed. */
const changedLines = () => [...screen.getByLabelText("Changed files").querySelectorAll("summary")].map((line) => line.textContent);

/** The paths each changed file that shows its diff names. */
const openedDiffs = () => [...screen.getByLabelText("Changed files").querySelectorAll("details[open] > summary .fv-path")].map((path) => path.textContent);

/** The names the file tree draws, directories and files, in order. */
const treeNames = () => [...screen.getByLabelText("File tree").querySelectorAll(".fv-path")].map((name) => name.textContent);

/** The headers of the changed list and the file tree. */
const headers = () => [...document.querySelectorAll(".fv-part h3")].map((header) => header.textContent);

test("on no record the pane asks for one and reads nothing", () => {
  serve();
  const client = new QueryClient();
  mount({ record: null, client });
  expect(screen.getByText("Open a record to see its files.")).toBeTruthy();
  expect(client.isFetching()).toBe(0);
});

/** A side-by-side diff's rows: a hunk header alone, else each side's number and line. */
function splitCells(diff: HTMLElement): string[][] {
  return [...diff.querySelectorAll(".sd-split tbody tr")].map((row) => [...row.querySelectorAll("td")].map((cell) => cell.textContent ?? ""));
}

test("a changed file opened shows its diff's header lines, then its hunks side by side, each side numbered", async () => {
  serve();
  mount({ params: { file: "attn.py" } });
  await loaded();
  expect(changedLines()).toEqual(["attn.pymodified", "bench/run.pyadded", "old.pydeleted"]);
  const [modified, added] = [...screen.getByLabelText("Changed files").querySelectorAll("li")];
  const diff = within(modified!).getByRole("group", { name: "Code diff" });
  expect(diff.querySelector(".fv-diff-head")!.textContent).toBe(TREE_DIFF.split("\n").slice(0, 4).join("\n"));
  expect(splitCells(diff)).toEqual([
    ["@@ -3,3 +3,3 @@ def tile():"],
    ["3", " def tile():", "3", " def tile():"],
    ["4", "-    warps = 4", "4", "+    warps = 2"],
    ["5", "     return warps", "5", "     return warps"],
    ["\\ No newline at end of file"],
  ]);
  expect(within(added!).queryByRole("group", { name: "Code diff" })).toBeNull();
});

test("Unified lays a diff out as one column of marked lines, each numbered, and the choice holds for the next diff", async () => {
  serve();
  mount({ params: { file: "attn.py" } });
  await loaded();
  fireEvent.click(screen.getByRole("button", { name: "Unified" }));
  const diff = screen.getByRole("group", { name: "Code diff" });
  expect(diff.querySelector(".sd-split")).toBeNull();
  expect([...diff.querySelectorAll(".tr-diff .ln")].map((line) => [line.className, line.getAttribute("data-n"), line.textContent])).toEqual([
    ["ln d-hunk", null, "@@ -3,3 +3,3 @@ def tile():"],
    ["ln d-ctx", "3", " def tile():"],
    ["ln d-del", "4", "-    warps = 4"],
    ["ln d-add", "4", "+    warps = 2"],
    ["ln d-ctx", "5", "     return warps"],
    ["ln d-hunk", null, "\\ No newline at end of file"],
  ]);
  expect(screen.getByRole("button", { name: "Unified" }).getAttribute("aria-pressed")).toBe("true");
  expect(window.localStorage.getItem("trax.diff-mode")).toBe("unified");
});

test("a diff past 400 lines draws its first 400 and counts the rest", async () => {
  // Unified, as the viewer chose it before: one element a line keeps the 400 cheap to draw.
  localStorage.setItem("trax.diff-mode", "unified");
  const long = ["--- a/big.txt", "+++ b/big.txt", "@@ -1,0 +1,404 @@", ...Array.from({ length: 404 }, (_, k) => `+line ${k}`)].join("\n");
  serve([filesRow({ ...FILES, changed: [{ path: "big.txt", change: "added", diff: long, note: "" }] })]);
  mount({ params: { file: "big.txt" } });
  await loaded();
  const diff = screen.getByRole("group", { name: "Code diff" });
  expect(diff.querySelectorAll(".tr-diff .ln")).toHaveLength(400);
  expect(diff.querySelector(".sd-more")!.textContent).toBe("… 5 more lines");
});

test("each change opens and folds on a click, and says why its diff is cut or missing", async () => {
  serve([filesRow({ ...FILES, changed: [{ ...FILES.changed[0]!, note: "clipped" }, ...FILES.changed.slice(1)] })]);
  mount();
  await loaded();
  expect(openedDiffs()).toEqual([]);
  const summaries = [...screen.getByLabelText("Changed files").querySelectorAll("summary")];
  for (const summary of summaries) fireEvent.click(summary);
  expect(openedDiffs()).toEqual(["attn.py", "bench/run.py", "old.py"]);
  const items = [...screen.getByLabelText("Changed files").querySelectorAll("li")];
  expect(items.map((item) => [!!within(item).queryByRole("group", { name: "Code diff" }), item.querySelector(":scope details > .fv-hint")?.textContent])).toEqual([
    [true, "The diff is cut here."],
    [false, "This diff is not shown."],
    [false, "Its changes are not shown."],
  ]);
  fireEvent.click(summaries[1]!);
  expect(openedDiffs()).toEqual(["attn.py", "old.py"]);
});

test("the headers name the base and count each list, k of n while a path narrows it, + when the source listed fewer", async () => {
  serve();
  const { rerender } = mount({ actions: canvasActions() });
  await loaded();
  expect(headers()).toEqual(["Changed from origin/main (3)", "Files (3)"]);
  rerender({ path: "bench/" });
  expect(headers()).toEqual(["Changed from origin/main (1 of 3)", "Files (1 of 3)"]);
  cleanup();
  serve([filesRow({ ...FILES, base: "", more_files: true, more_changes: true })]);
  mount();
  await loaded();
  expect(headers()).toEqual(["Changed (3+)", "Files (3+)"]);
  expect(screen.getByText("More files changed than are listed here.")).toBeTruthy();
  expect(screen.getByText("More files exist than are listed here.")).toBeTruthy();
});

test("a path narrows both lists: a prefix to a directory, a glob across the tree", async () => {
  serve([filesRow(NESTED)]);
  const { rerender } = mount({ params: { path: "pkg/foo/" }, actions: canvasActions() });
  await loaded();
  expect(changedLines()).toEqual(["pkg/foo/bar.pymodified"]);
  expect(treeNames()).toEqual(["pkg/foo/", "bar.py", "baz.txt"]);
  rerender({ path: "**/*.py" });
  expect(changedLines()).toEqual(["gone/old.pydeleted", "pkg/foo/bar.pymodified", "pkg/lib/bar.pyadded"]);
  expect(treeNames()).toEqual(["pkg/", "foo/", "bar.py", "lib/", "bar.py", "foobar.py"]);
  rerender({ path: "PKG/FOO" });
  expect(treeNames()).toEqual(["pkg/", "foo/", "bar.py", "baz.txt", "foobar.py"]);
});

test("changed only hides the tree and keeps the changed files", async () => {
  serve();
  mount({ params: { changed: true } });
  await loaded();
  expect(changedLines()).toHaveLength(3);
  expect(screen.queryByLabelText("File tree")).toBeNull();
  expect(headers()).toEqual(["Changed from origin/main (3)"]);
});

test("copied trees list after the changed files, with their count and base, and no change counts them", async () => {
  serve([filesRow({ ...FILES, copied: [{ path: "node_modules/", files: 1790 }, { path: "LICENSE", files: 1 }] })]);
  mount();
  await loaded();
  const copied = screen.getByLabelText("Copied unchanged");
  expect([...copied.querySelectorAll("li")].map((line) => line.textContent)).toEqual([
    "node_modules/1,790 files copied unchanged from origin/main",
    "LICENSE1 file copied unchanged from origin/main",
  ]);
  expect(screen.getByLabelText("Changed files").compareDocumentPosition(copied) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(headers()).toEqual(["Changed from origin/main (3)", "Files (3)"]);
});

test("copied trees stand above the hint that more files changed than are listed", async () => {
  serve([filesRow({ ...FILES, more_changes: true, copied: [{ path: "node_modules/", files: 1790 }] })]);
  mount();
  await loaded();
  const hint = screen.getByText("More files changed than are listed here.");
  expect(screen.getByLabelText("Copied unchanged").compareDocumentPosition(hint) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
});

test("copied trees name the base as the base when none is known, and count a file in the singular", async () => {
  serve([filesRow({ ...FILES, base: "", copied: [{ path: "build/", files: 1 }] })]);
  mount();
  await loaded();
  expect(screen.getByLabelText("Copied unchanged").textContent).toBe("build/1 file copied unchanged from the base");
});

test("a path narrows the copied trees too, and none that it leaves shows no list", async () => {
  serve([filesRow({ ...FILES, copied: [{ path: "node_modules/", files: 1790 }, { path: "src/gen/", files: 4 }] })]);
  mount({ params: { path: "src/" } });
  await loaded();
  expect([...screen.getByLabelText("Copied unchanged").querySelectorAll("li")].map((line) => line.textContent)).toEqual([
    "src/gen/4 files copied unchanged from origin/main",
  ]);
  fireEvent.change(screen.getByLabelText("Filter by path"), { target: { value: "bench/" } });
  expect(screen.queryByLabelText("Copied unchanged")).toBeNull();
});

test("a file opens its diff by path, or every change of that name, among those the path leaves, and narrows nothing", async () => {
  serve([filesRow(NESTED)]);
  const { rerender } = mount({ params: { file: "bar.py" }, actions: canvasActions() });
  await loaded();
  expect(openedDiffs()).toEqual(["pkg/foo/bar.py", "pkg/lib/bar.py"]);
  expect(changedLines()).toHaveLength(3);
  expect(screen.getByText("Opened").textContent).toBe("Opened bar.pyClose");
  rerender({ file: "pkg/lib/bar.py" });
  expect(openedDiffs()).toEqual(["pkg/lib/bar.py"]);
  rerender({ path: "pkg/foo/", file: "bar.py" });
  expect(openedDiffs()).toEqual(["pkg/foo/bar.py"]);
  rerender({ path: "pkg/foo/", file: "lib/bar.py" });
  expect(openedDiffs()).toEqual([]);
  expect(screen.getByText("No listed changed file is named lib/bar.py.")).toBeTruthy();
});

test("in a canvas, Changed only, Close, Enter and × each show the visual again with all three params", async () => {
  serve([filesRow(NESTED)]);
  const actions = canvasActions({ visualTypes: new Set(["trax.files"]) });
  mount({ params: { path: "pkg/", changed: false, file: "bar.py" }, actions });
  const filter = await loaded();
  fireEvent.click(screen.getByRole("checkbox", { name: "Changed only" }));
  fireEvent.click(screen.getByRole("button", { name: "Close" }));
  fireEvent.change(filter, { target: { value: " pkg/lib/ " } });
  // Typed, the filter narrows at once; only Enter keeps it.
  expect(changedLines()).toEqual(["pkg/lib/bar.pyadded"]);
  expect(vi.mocked(actions.operate)).toHaveBeenCalledTimes(2);
  fireEvent.keyDown(filter, { key: "Enter" });
  fireEvent.click(screen.getByRole("button", { name: "Clear the path filter" }));
  const show = (path: string, changed: boolean, file: string) => [
    { kind: "show", visual_type: "trax.files", record_id: FILES_RECORD, params: { path, changed, file } },
  ];
  expect(vi.mocked(actions.operate).mock.calls).toEqual([
    show("pkg/", true, "bar.py"),
    show("pkg/", false, ""),
    show("pkg/lib/", false, "bar.py"),
    show("", false, "bar.py"),
  ]);
});

test("while the canvas writes, the controls that would write again wait, Enter on the filter too", async () => {
  serve();
  const actions = canvasActions({ busy: true });
  mount({ params: { path: "bench/", file: "run.py" }, actions });
  const filter = await loaded();
  const controls = [screen.getByRole("checkbox", { name: "Changed only" }), ...["Close", "Clear the path filter"].map((name) => screen.getByRole("button", { name }))];
  expect(controls.map((control) => (control as HTMLInputElement | HTMLButtonElement).disabled)).toEqual([true, true, true]);
  fireEvent.change(filter, { target: { value: "notes" } });
  fireEvent.keyDown(filter, { key: "Enter" });
  expect(actions.operate).not.toHaveBeenCalled();
  expect((filter as HTMLInputElement).value).toBe("notes");
});

test("a show from outside replaces the typed filter and the reader's own folds", async () => {
  serve([filesRow(NESTED)]);
  const { rerender } = mount({ params: { file: "bar.py" }, actions: canvasActions() });
  const filter = (await loaded()) as HTMLInputElement;
  fireEvent.click(screen.getByLabelText("Changed files").querySelectorAll("summary")[1]!);
  fireEvent.change(filter, { target: { value: "pkg/" } });
  expect(openedDiffs()).toEqual(["pkg/lib/bar.py"]);
  rerender({ file: "bar.py" });
  expect([filter.value, openedDiffs()]).toEqual(["pkg/", ["pkg/lib/bar.py"]]);
  rerender({ file: "pkg/foo/bar.py" });
  expect([filter.value, openedDiffs()]).toEqual(["pkg/", ["pkg/foo/bar.py"]]);
  rerender({ path: "pkg/lib/", file: "pkg/foo/bar.py" });
  expect(filter.value).toBe("pkg/lib/");
});

test("outside a canvas the controls narrow this pane alone", async () => {
  serve([filesRow(NESTED)]);
  mount({ params: { file: "bar.py" } });
  const filter = await loaded();
  fireEvent.click(screen.getByRole("checkbox", { name: "Changed only" }));
  expect(screen.queryByLabelText("File tree")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Close" }));
  expect(screen.queryByText("Opened")).toBeNull();
  expect(openedDiffs()).toEqual([]);
  fireEvent.change(filter, { target: { value: "pkg/lib/" } });
  fireEvent.keyDown(filter, { key: "Enter" });
  fireEvent.click(screen.getByRole("checkbox", { name: "Changed only" }));
  expect(treeNames()).toEqual(["pkg/lib/", "bar.py"]);
  fireEvent.click(screen.getByRole("button", { name: "Clear the path filter" }));
  expect(changedLines()).toHaveLength(3);
});

test("in the tree a directory holding a change starts open, another folded, and a changed file's click opens its diff there", async () => {
  serve([filesRow(NESTED)]);
  const scrolled = vi.fn();
  Element.prototype.scrollIntoView = scrolled;
  try {
    mount();
    await loaded();
    expect(treeNames()).toEqual(["docs/", "pkg/", "foo/", "bar.py", "baz.txt", "lib/", "bar.py", "foobar.py", "README.md"]);
    fireEvent.click(screen.getByLabelText("File tree").querySelector("summary")!);
    expect(treeNames()).toContain("guide.md");
    const [, lib] = within(screen.getByLabelText("File tree")).getAllByRole("button", { name: /^bar\.py/ });
    fireEvent.click(lib!);
    expect(openedDiffs()).toEqual(["pkg/lib/bar.py"]);
    await waitFor(() => expect(scrolled).toHaveBeenCalledTimes(1));
    expect((scrolled.mock.contexts[0] as HTMLElement).dataset["path"]).toBe("pkg/lib/bar.py");
  } finally {
    Reflect.deleteProperty(Element.prototype, "scrollIntoView");
  }
});

test("the pane counts the paths the source withheld and the documents filed, and shows the newest", async () => {
  serve([
    filesRow({ ...FILES, hidden: 2, base: "older" }, { modified: "2026-10-12T08:00:00+00:00" }),
    filesRow({ ...FILES, hidden: 2 }, { modified: "2026-10-12T09:00:00+00:00" }),
  ]);
  mount();
  await loaded();
  expect(screen.getByText("2 paths are withheld by the source that filed this list.")).toBeTruthy();
  expect(screen.getByText("Newest of 2 files documents for this record.")).toBeTruthy();
  expect(headers()[0]).toBe("Changed from origin/main (3)");
  cleanup();
  serve([filesRow({ ...FILES, hidden: 1 })]);
  mount();
  await loaded();
  expect(screen.getByText("1 path is withheld by the source that filed this list.")).toBeTruthy();
  expect(screen.queryByText(/^Newest of/)).toBeNull();
});

test("a record with no document says so, and one with nothing changed says that", async () => {
  serve([]);
  mount();
  expect(await screen.findByText("No files document is filed for this record yet.")).toBeTruthy();
  cleanup();
  serve([filesRow({ ...FILES, changed: [] })]);
  mount();
  await loaded();
  expect(screen.getByText("No file differs from origin/main.")).toBeTruthy();
});

test("a read that fails says so with Retry, which reads again", async () => {
  let failing = true;
  stubFetch((request) => {
    const url = new URL(request.url);
    if (failing && url.pathname === "/api/inquiries") return Response.json({ detail: "files unavailable" }, { status: 500 });
    return answerFiles(url, [filesRow(FILES)])!;
  });
  mount();
  const failure = await screen.findByRole("alert");
  expect(failure.textContent).toContain("files unavailable");
  failing = false;
  fireEvent.click(within(failure).getByRole("button", { name: "Retry" }));
  await loaded();
  expect(screen.queryByRole("alert")).toBeNull();
});

test("a read that fails to refresh keeps the lists and says so beside them", async () => {
  let failing = false;
  stubFetch((request) => {
    const url = new URL(request.url);
    if (failing) return Response.json({ detail: "files unavailable" }, { status: 500 });
    return answerFiles(url, [filesRow(FILES)])!;
  });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  mount({ client });
  await loaded();
  const watched = client.getQueryCache().findAll({ queryKey: ["visual", "files", FILES_RECORD], exact: false }).flatMap((query) => query.observers);
  expect(watched.map((observer) => observer.options.refetchInterval)).toEqual([60_000]);
  failing = true;
  await act(() => client.refetchQueries({ queryKey: ["visual", "files"] }));
  expect((await screen.findByRole("alert")).textContent).toContain("Could not refresh the files: files unavailable");
  expect(changedLines()).toHaveLength(3);
});
