import { expect, test } from "vitest";
import { commonRoot, hashText, inSkippedDir, isBinaryPath, skippedCount, walkDataTransfer, walkFileList } from "./importFolder";
import { MAX_BLOB_BYTES } from "./limits";

/** A file as a folder pick reports it: its path under the picked folder, and its bytes. */
function picked(path: string, text: string): File {
  const file = new File([text], path.split("/").pop()!, { type: "text/plain" });
  // `webkitRelativePath` is read-only on File, so a pick is simulated by defining it.
  Object.defineProperty(file, "webkitRelativePath", { value: path });
  return file;
}

test("the hash is the sha256 of the text's UTF-8 bytes, which is the name the store checks", async () => {
  // The server recomputes this and refuses a mismatch, so a wrong encoding here loses
  // the whole import. Pinned against the known digest of "abc".
  expect(await hashText("abc")).toBe("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
  // Non-ASCII must hash as UTF-8, not as UTF-16 code units.
  expect(await hashText("é")).toBe(await hashText("é"));
});

test("build and VCS folders are skipped wherever they sit, not only at the root", () => {
  expect(inSkippedDir("node_modules/react/index.js")).toBe(true);
  expect(inSkippedDir("web/node_modules/react/index.js")).toBe(true);
  expect(inSkippedDir("a/.git/config")).toBe(true);
  expect(inSkippedDir("server/__pycache__/x.pyc")).toBe(true);
  expect(inSkippedDir("src/api/code.ts")).toBe(false);
  // A file whose own name matches is not in a skipped directory; the walk checks it apart.
  expect(inSkippedDir("src/build")).toBe(false);
});

test("binaries are known by extension, case-insensitively, and an extensionless file is not one", () => {
  expect(["a.PNG", "b/c.woff2", "m.safetensors", "x.pyc"].map(isBinaryPath)).toEqual([true, true, true, true]);
  expect(["Makefile", "src/a.ts", ".gitignore", "a.tsx"].map(isBinaryPath)).toEqual([false, false, false, false]);
});

test("the dropped folder's own name is stripped, so a snapshot's paths are relative to it", () => {
  expect(commonRoot(["repo/a.ts", "repo/src/b.ts"])).toBe("repo");
  // Loose files dropped together share no root, so nothing is stripped.
  expect(commonRoot(["a.ts", "b.ts"])).toBe("");
  expect(commonRoot(["one/a.ts", "two/b.ts"])).toBe("");
  expect(commonRoot(["a.ts"])).toBe("");
});

test("a picked folder yields its text files, relative to the folder, sorted by path", async () => {
  const walked = await walkFileList([
    picked("repo/src/b.ts", "export const b = 2;\n"),
    picked("repo/a.ts", "export const a = 1;\n"),
  ]);
  expect(walked.root).toBe("repo");
  expect(walked.files.map((file) => file.path)).toEqual(["a.ts", "src/b.ts"]);
  expect(walked.files[0]!.text).toBe("export const a = 1;\n");
  expect(walked.files[0]!.sha256).toBe(await hashText("export const a = 1;\n"));
  expect(skippedCount(walked.skipped)).toBe(0);
});

test("a drop of a real repo keeps the source and leaves the rest, counted by reason", async () => {
  // The case the pane exists to survive: source mixed with a dependency tree, a VCS
  // database, an image and an oversized file. A thin import must be explained, so each
  // exclusion is counted under its own reason rather than silently dropped.
  const walked = await walkFileList([
    picked("repo/src/app.ts", "ok\n"),
    picked("repo/node_modules/react/index.js", "module.exports = {}\n"),
    picked("repo/.git/config", "[core]\n"),
    picked("repo/logo.png", "not really a png"),
    picked("repo/huge.txt", "x".repeat(MAX_BLOB_BYTES + 1)),
    picked("repo/data.bin", "head\u0000tail"),
  ]);
  expect(walked.files.map((file) => file.path)).toEqual(["src/app.ts"]);
  expect(walked.skipped).toEqual({ ignored: 2, binary: 2, "too-large": 1, "empty-path": 0 });
  expect(skippedCount(walked.skipped)).toBe(5);
});

test("a file with no extension but NUL bytes is caught as binary by its own content", async () => {
  // The extension list cannot know this one, and a NUL in a blob is not text.
  const walked = await walkFileList([picked("repo/mystery", "ELF\u0000binary"), picked("repo/README", "hi\n")]);
  expect(walked.files.map((file) => file.path)).toEqual(["README"]);
  expect(walked.skipped.binary).toBe(1);
});

test("a file exactly at the blob limit is kept, and one byte over is not", async () => {
  const walked = await walkFileList([
    picked("repo/edge.txt", "x".repeat(MAX_BLOB_BYTES)),
    picked("repo/over.txt", "x".repeat(MAX_BLOB_BYTES + 1)),
  ]);
  expect(walked.files.map((file) => file.path)).toEqual(["edge.txt"]);
  expect(walked.skipped["too-large"]).toBe(1);
});

test("a multi-byte character counted in UTF-8 can put a file over the limit its size did not", async () => {
  // `File.size` is already UTF-8 here, but the walk measures the decoded text too, since a
  // text that decoded with replacement characters can grow past the bound.
  const text = "é".repeat(MAX_BLOB_BYTES / 2 + 1);
  const walked = await walkFileList([picked("repo/wide.txt", text)]);
  expect(walked.files).toEqual([]);
  expect(walked.skipped["too-large"]).toBe(1);
});

// ---- the drop path -----------------------------------------------------------------
//
// A drop does not hand over a FileList; it hands over directory ENTRIES, which must be
// walked. Two things about that API bite: `readEntries` answers a PAGE at a time and must
// be called until it answers an empty list, and a folder's own name is the root every path
// is relative to. These fake the entries so both are exercised without a real drag.

type Tree = { readonly [name: string]: Tree | string };

/** A `FileSystemEntry` tree built from a plain object; a string leaf is a file's text. */
function entryOf(name: string, node: Tree | string, pageSize = 100): FileSystemEntry {
  if (typeof node === "string") {
    return {
      isFile: true,
      isDirectory: false,
      name,
      file: (resolve: (file: File) => void) => resolve(new File([node], name)),
    } as unknown as FileSystemEntry;
  }
  const children = Object.entries(node).map(([child, inner]) => entryOf(child, inner, pageSize));
  return {
    isFile: false,
    isDirectory: true,
    name,
    createReader() {
      // `readEntries` pages: it answers a slice, then `[]` once the directory is done.
      let at = 0;
      return {
        readEntries(resolve: (entries: FileSystemEntry[]) => void) {
          const page = children.slice(at, at + pageSize);
          at += page.length;
          resolve(page);
        },
      };
    },
  } as unknown as FileSystemEntry;
}

/** A `DataTransfer` whose items answer `webkitGetAsEntry`, as a real drop's do. */
function dropOf(tree: Tree, pageSize = 100): DataTransfer {
  const entries = Object.entries(tree).map(([name, node]) => entryOf(name, node, pageSize));
  return {
    items: entries.map((entry) => ({ kind: "file", webkitGetAsEntry: () => entry })),
    files: [],
  } as unknown as DataTransfer;
}

test("a dropped folder is walked to its leaves, with paths relative to the folder", async () => {
  const walked = await walkDataTransfer(
    dropOf({ repo: { "README.md": "hi\n", src: { "app.ts": "a\n", util: { "math.py": "m\n" } } } }),
  );
  expect(walked.root).toBe("repo");
  expect(walked.files.map((file) => file.path)).toEqual(["README.md", "src/app.ts", "src/util/math.py"]);
  expect(walked.files[2]!.sha256).toBe(await hashText("m\n"));
});

test("readEntries is called until it answers empty, so a folder bigger than one page is whole", async () => {
  // The classic bug in this API: one call is NOT the directory. With a page of two and six
  // files, a single call would import a third of the folder and report success.
  const many: Tree = Object.fromEntries(Array.from({ length: 6 }, (_, n) => [`f${n}.ts`, `x${n}\n`]));
  const walked = await walkDataTransfer(dropOf({ repo: many }, 2));
  expect(walked.files).toHaveLength(6);
  expect(walked.files.map((file) => file.path)).toEqual(["f0.ts", "f1.ts", "f2.ts", "f3.ts", "f4.ts", "f5.ts"]);
});

test("a skipped directory is never descended into, so its contents cost nothing to refuse", async () => {
  let opened = 0;
  const heavy: Tree = { "index.js": "module.exports={}\n" };
  const drop = dropOf({ repo: { node_modules: heavy, ".git": { config: "[core]\n" }, "app.ts": "a\n" } });
  // Count reads of the skipped trees: the walk must not even ask for their entries.
  for (const item of (drop as unknown as { items: { webkitGetAsEntry: () => FileSystemEntry }[] }).items) {
    const root = item.webkitGetAsEntry() as unknown as { createReader: () => unknown };
    const inner = root.createReader;
    root.createReader = () => {
      opened += 1;
      return (inner as () => never).call(root);
    };
  }
  const walked = await walkDataTransfer(drop);
  expect(walked.files.map((file) => file.path)).toEqual(["app.ts"]);
  // The root was opened; `node_modules` and `.git` were refused by name, not by reading them.
  expect(opened).toBe(1);
});

test("loose files dropped together keep their own names, with no root stripped", async () => {
  const walked = await walkDataTransfer(dropOf({ "a.ts": "a\n", "b.ts": "b\n" }));
  expect(walked.root).toBe("");
  expect(walked.files.map((file) => file.path)).toEqual(["a.ts", "b.ts"]);
});

test("a file the browser refuses to open is left out rather than failing the whole import", async () => {
  // One unreadable file in a tree of a thousand must not cost the other 999.
  const drop = dropOf({ repo: { "ok.ts": "a\n", "locked.ts": "b\n" } });
  const root = (drop as unknown as { items: { webkitGetAsEntry: () => FileSystemEntry }[] }).items[0]!.webkitGetAsEntry();
  const reader = (root as unknown as { createReader: () => { readEntries: (r: (e: FileSystemEntry[]) => void) => void } }).createReader();
  reader.readEntries((entries) => {
    const locked = entries.find((entry) => entry.name === "locked.ts")! as unknown as {
      file: (resolve: unknown, reject: (error: unknown) => void) => void;
    };
    locked.file = (_resolve, reject) => reject(new Error("NotReadableError"));
  });
  const walked = await walkDataTransfer(drop);
  expect(walked.files.map((file) => file.path)).toEqual(["ok.ts"]);
});

test("a browser that gives no entries still imports the files the drop carried", async () => {
  // `webkitGetAsEntry` answering null is the fallback path: a flat drop beats nothing.
  const file = new File(["a\n"], "a.ts");
  const transfer = {
    items: [{ kind: "file", webkitGetAsEntry: () => null }],
    files: [file],
  } as unknown as DataTransfer;
  const walked = await walkDataTransfer(transfer);
  expect(walked.files.map((each) => each.path)).toEqual(["a.ts"]);
});
