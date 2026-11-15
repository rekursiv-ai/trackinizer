import { expect, test } from "vitest";
import { commonRoot, hashText, inSkippedDir, isBinaryPath, skippedCount, walkFileList } from "./importFolder";
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
