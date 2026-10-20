import { expect, test } from "vitest";
import { type ChangedFile, diffParts, fileTree, openedPaths, parseFilesDoc, pathMatcher } from "./filesDoc";
import { FILES, TREE_DIFF } from "./testing";

test("a files document reads back as written, its base too", () => {
  const doc = parseFilesDoc(JSON.stringify(FILES));
  expect(doc).toEqual({ ...FILES, copied: [] });
  expect(doc?.base).toBe("origin/main");
});

test("a document filed before copied trees existed reads with none", () => {
  expect(parseFilesDoc(JSON.stringify({ ...FILES, copied: undefined }))?.copied).toEqual([]);
});

test("a document's copied trees read back, a directory with its trailing slash and a single file alike", () => {
  const copied = [
    { path: "node_modules/", files: 1790 },
    { path: "NOTES.md", files: 1 },
  ];
  expect(parseFilesDoc(JSON.stringify({ ...FILES, copied }))?.copied).toEqual(copied);
});

test.each([
  ["a session document", { kind: "session", version: 1, session: "s" }],
  ["a change it does not know", { ...FILES, changed: [{ ...FILES.changed[0], change: "renamed" }] }],
  ["a note it does not know", { ...FILES, changed: [{ ...FILES.changed[0], note: "hidden" }] }],
  ["a path that is not text", { ...FILES, files: [7] }],
  ["no base", { ...FILES, base: undefined }],
  ["a base that is not text", { ...FILES, base: 7 }],
  ["no more_files", { ...FILES, more_files: undefined }],
  ["no more_changes", { ...FILES, more_changes: "no" }],
  ["copied trees that are not a list", { ...FILES, copied: "node_modules/" }],
  ["copied trees that are null", { ...FILES, copied: null }],
  ["a copied tree that is not an object", { ...FILES, copied: ["node_modules/"] }],
  ["a copied tree with no path", { ...FILES, copied: [{ path: "", files: 1 }] }],
  ["a copied tree with no count of files", { ...FILES, copied: [{ path: "node_modules/" }] }],
  ["a copied tree of no file", { ...FILES, copied: [{ path: "node_modules/", files: 0 }] }],
  ["a copied tree of a fraction of a file", { ...FILES, copied: [{ path: "node_modules/", files: 1.5 }] }],
  ["no count of hidden paths", { ...FILES, hidden: undefined }],
  ["a negative count of hidden paths", { ...FILES, hidden: -1 }],
  ["a count of hidden paths that is no whole number", { ...FILES, hidden: 1.5 }],
  ["a later version", { ...FILES, version: 2 }],
  ["another kind", { ...FILES, kind: "tree" }],
])("%s is no files document", (_, doc) => {
  expect(parseFilesDoc(JSON.stringify(doc))).toBeNull();
});

test("a description that is not JSON, or none, is no files document", () => {
  expect(parseFilesDoc("not json")).toBeNull();
  expect(parseFilesDoc(null)).toBeNull();
});

test.each([
  ["", "anything/at.all", true],
  ["pkg/foo/", "pkg/foo/bar.py", true],
  ["pkg/foo/", "pkg/foobar.py", false],
  ["pkg/foo", "pkg/foobar.py", true],
  ["PKG/Foo/", "pkg/foo/bar.py", true],
  ["pkg/foo/", "Pkg/Foo/Bar.py", true],
  ["**/*.py", "attn.py", true],
  ["**/*.py", "a/b/c.py", true],
  ["**/*.py", "notes.txt", false],
  ["**/b.py", "ab.py", false],
  ["src/**/x.py", "src/x.py", true],
  ["src/**/x.py", "src/a/b/x.py", true],
  ["src/**/x.py", "src/ax.py", false],
  ["src/**/x.py", "src/a/bx.py", false],
  ["src/**", "src/a/b/x.py", true],
  ["src/**", "src/", true],
  ["**", "a/b", true],
  ["**a**b", "xa/yb", true],
  ["**a**b", "xa/ybz", false],
  ["*/x.py", "a/x.py", true],
  ["*/x.py", "a/b/x.py", false],
  ["?", "", false],
  ["*.py", "attn.py", true],
  ["*.PY", "attn.py", true],
  ["*.py", "bench/run.py", false],
  ["*.py", "attnxpy", false],
  ["bench/ru?.py", "bench/run.py", true],
  ["a?b", "a/b", false],
  ["c++/*.h", "c++/x.h", true],
  ["c++/*.h", "cc/x.h", false],
  ["lib(1)/*.py", "lib(1)/x.py", true],
  ["lib(1)/*.py", "lib1/x.py", false],
])("path %j matches %j: %s", (path, candidate, matches) => {
  expect(pathMatcher(path)(candidate)).toBe(matches);
});

test("a glob of many `**` meets a long path in time linear in both, where a regular expression backtracks for minutes", () => {
  const glob = `${"**a".repeat(6)}**b`;
  const started = performance.now();
  expect(pathMatcher(glob)("a".repeat(200))).toBe(false);
  expect(pathMatcher(glob)(`${"a".repeat(199)}b`)).toBe(true);
  expect(performance.now() - started).toBeLessThan(50);
});

const changed = (...paths: string[]): ChangedFile[] => paths.map((path) => ({ path, change: "modified", diff: "", note: "" }));

test.each([
  ["the file at that path, though others share its name", changed("bar.py", "pkg/foo/bar.py"), "bar.py", ["bar.py"]],
  ["every file of that name", changed("pkg/foo/bar.py", "pkg/foo/baz.py", "pkg/lib/bar.py"), "bar.py", ["pkg/foo/bar.py", "pkg/lib/bar.py"]],
  ["every file whose path ends so", changed("pkg/foo/bar.py", "pkg/lib/bar.py"), "foo/bar.py", ["pkg/foo/bar.py"]],
  ["a name in another case", changed("pkg/foo/Bar.py", "pkg/lib/bar.py"), "BAR.py", ["pkg/foo/Bar.py", "pkg/lib/bar.py"]],
  ["no file whose name only ends so", changed("foo.py"), "o.py", []],
  ["none for no file", changed("foo.py"), "", []],
])("a file opens %s", (_, files, file, opened) => {
  expect(openedPaths(files, file)).toEqual(opened);
});

test("the tree puts directories before files, each by name, and joins a directory to its only child directory", () => {
  const file = (path: string) => ({ kind: "file", name: path.slice(path.lastIndexOf("/") + 1), path });
  expect(fileTree(["b.txt", "z/two.py", "src/lib/deep/n.py", "a/x.py", "z/one/only.py", "A.md", "src/lib/deep/m.py"])).toEqual([
    { kind: "dir", name: "a/", path: "a/", children: [file("a/x.py")] },
    { kind: "dir", name: "src/lib/deep/", path: "src/lib/deep/", children: [file("src/lib/deep/m.py"), file("src/lib/deep/n.py")] },
    {
      kind: "dir",
      name: "z/",
      path: "z/",
      children: [{ kind: "dir", name: "one/", path: "z/one/", children: [file("z/one/only.py")] }, file("z/two.py")],
    },
    file("A.md"),
    file("b.txt"),
  ]);
});

test("a diff splits into its header lines and its hunks, each line numbered in the file it is in", () => {
  const { head, lines } = diffParts(TREE_DIFF);
  expect(head).toEqual(["diff --git a/attn.py b/attn.py", "index 1111111..2222222 100644", "--- a/attn.py", "+++ b/attn.py"]);
  expect(lines.map(({ kind, line }) => [kind, line])).toEqual([
    ["hunk", null],
    ["ctx", 3],
    ["del", 4],
    ["add", 4],
    ["ctx", 5],
    ["hunk", null],
  ]);
  expect(lines.at(-1)!.text).toBe("\\ No newline at end of file");
});

test("each hunk numbers from its own header; a context line left empty is a space", () => {
  const { lines } = diffParts(["@@ -1,2 +1,2 @@", "-a", "+b", "@@ -10 +11,2 @@", "", "+c"].join("\n"));
  expect(lines.map(({ kind, text, line }) => [kind, text, line])).toEqual([
    ["hunk", "@@ -1,2 +1,2 @@", null],
    ["del", "-a", 1],
    ["add", "+b", 1],
    ["hunk", "@@ -10 +11,2 @@", null],
    ["ctx", " ", 11],
    ["add", "+c", 12],
  ]);
});

test("a diff with no hunk is all header, and no diff is nothing", () => {
  expect(diffParts("diff --git a/x.bin b/x.bin\nBinary files a/x.bin and b/x.bin differ\n")).toEqual({
    head: ["diff --git a/x.bin b/x.bin", "Binary files a/x.bin and b/x.bin differ"],
    lines: [],
  });
  expect(diffParts("")).toEqual({ head: [], lines: [] });
});
