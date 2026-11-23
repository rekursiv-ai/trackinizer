import { beforeEach, expect, test, vi } from "vitest";
import type { ReadFile, Walked } from "./importFolder";
import { MAX_BLOBS_PER_REQUEST, MAX_REQUEST_TEXT_BYTES, MAX_SNAPSHOT_FILES } from "./limits";
import { uploadWalk } from "./upload";

const { getMissingBlobs, putCodeBlobs, putCodeSnapshot } = vi.hoisted(() => ({
  getMissingBlobs: vi.fn(),
  putCodeBlobs: vi.fn(),
  putCodeSnapshot: vi.fn(),
}));

vi.mock("../api/code", () => ({ getMissingBlobs, putCodeBlobs, putCodeSnapshot }));

const RECORD = "11111111-1111-1111-1111-111111111111";

beforeEach(() => {
  getMissingBlobs.mockReset();
  putCodeBlobs.mockReset().mockResolvedValue(0);
  putCodeSnapshot.mockReset().mockResolvedValue({ file_count: 0 });
});

/** A walk of `files`, with nothing skipped. */
function walk(files: readonly ReadFile[]): Walked {
  return { files, root: "repo", skipped: { ignored: 0, binary: 0, "too-large": 0, "empty-path": 0 } };
}

function file(path: string, text: string, sha256 = `${text}-hash`.padEnd(64, "0")): ReadFile {
  return { path, text, sha256 };
}

test("only the blobs the store lacks are sent, and the snapshot still maps every path", async () => {
  // The whole point of content addressing: a second import of a tree the store holds
  // uploads nothing, yet the snapshot must still name all of its files.
  const files = [file("a.ts", "aaa"), file("b.ts", "bbb")];
  getMissingBlobs.mockResolvedValue([files[1]!.sha256]);
  const done = await uploadWalk(RECORD, walk(files), { owner: "me@acme.co" });

  expect(putCodeBlobs).toHaveBeenCalledTimes(1);
  expect(putCodeBlobs.mock.calls[0]![1]).toEqual([{ path: "b.ts", text: "bbb", sha256: files[1]!.sha256 }]);
  expect(done).toEqual({ files: 2, sent: 1, dropped: 0 });
  const [record, snapshot] = putCodeSnapshot.mock.calls[0]!;
  expect(record).toBe(RECORD);
  expect(snapshot.files).toEqual({ "a.ts": files[0]!.sha256, "b.ts": files[1]!.sha256 });
});

test("two paths with the same content send one blob, so a repo of empty files is one upload", async () => {
  const same = "d".padEnd(64, "0");
  const files = [file("one/__init__.py", "", same), file("two/__init__.py", "", same)];
  getMissingBlobs.mockResolvedValue([same]);
  const done = await uploadWalk(RECORD, walk(files), { owner: "me@acme.co" });

  // Asked about once, sent once, mapped twice.
  expect(getMissingBlobs.mock.calls[0]![1]).toEqual([same]);
  expect(putCodeBlobs.mock.calls[0]![1]).toHaveLength(1);
  expect(done.sent).toBe(1);
  expect(Object.keys(putCodeSnapshot.mock.calls[0]![1].files)).toHaveLength(2);
});

test("nothing missing means no blobs request at all, only the snapshot", async () => {
  getMissingBlobs.mockResolvedValue([]);
  const done = await uploadWalk(RECORD, walk([file("a.ts", "aaa")]), { owner: "me@acme.co" });
  expect(putCodeBlobs).not.toHaveBeenCalled();
  expect(putCodeSnapshot).toHaveBeenCalledTimes(1);
  expect(done).toEqual({ files: 1, sent: 0, dropped: 0 });
});

test("a request is split at the blob count, since one over it is a 422 that loses the import", async () => {
  const files = Array.from({ length: MAX_BLOBS_PER_REQUEST + 5 }, (_, n) =>
    file(`f${n}.ts`, `x${n}`, `${n}`.padStart(64, "0")),
  );
  getMissingBlobs.mockResolvedValue(files.map((each) => each.sha256));
  await uploadWalk(RECORD, walk(files), { owner: "me@acme.co" });

  expect(putCodeBlobs).toHaveBeenCalledTimes(2);
  expect(putCodeBlobs.mock.calls[0]![1]).toHaveLength(MAX_BLOBS_PER_REQUEST);
  expect(putCodeBlobs.mock.calls[1]![1]).toHaveLength(5);
  for (const [, batch] of putCodeBlobs.mock.calls) expect(batch.length).toBeLessThanOrEqual(MAX_BLOBS_PER_REQUEST);
});

test("a request is split at the byte bound too, which a small count can still cross", async () => {
  // Five 1 MB blobs are well under the 1,000-blob count but over the 8 MB text bound,
  // so the count check alone would send an invalid request.
  const big = "x".repeat(1_048_576);
  const files = Array.from({ length: 10 }, (_, n) => file(`big${n}.txt`, big, `${n}`.padStart(64, "a")));
  getMissingBlobs.mockResolvedValue(files.map((each) => each.sha256));
  await uploadWalk(RECORD, walk(files), { owner: "me@acme.co" });

  expect(putCodeBlobs.mock.calls.length).toBeGreaterThan(1);
  for (const [, batch] of putCodeBlobs.mock.calls) {
    const bytes = batch.reduce((total: number, blob: ReadFile) => total + new TextEncoder().encode(blob.text).length, 0);
    expect(bytes).toBeLessThanOrEqual(MAX_REQUEST_TEXT_BYTES);
  }
});

test("the missing question is asked in pages, so a huge tree never asks about too many hashes", async () => {
  const files = Array.from({ length: 12_000 }, (_, n) => file(`f${n}.ts`, `x${n}`, `${n}`.padStart(64, "0")));
  getMissingBlobs.mockResolvedValue([]);
  await uploadWalk(RECORD, walk(files), { owner: "me@acme.co" });

  expect(getMissingBlobs.mock.calls.length).toBe(3);
  for (const [, page] of getMissingBlobs.mock.calls) expect(page.length).toBeLessThanOrEqual(5_000);
});

test("a tree past the snapshot limit is cut, and the snapshot says so rather than failing", async () => {
  const files = Array.from({ length: MAX_SNAPSHOT_FILES + 3 }, (_, n) =>
    file(`f${n}.ts`, `x${n}`, `${n}`.padStart(64, "0")),
  );
  getMissingBlobs.mockResolvedValue([]);
  const done = await uploadWalk(RECORD, walk(files), { owner: "me@acme.co" });

  expect(done.files).toBe(MAX_SNAPSHOT_FILES);
  expect(done.dropped).toBe(3);
  expect(Object.keys(putCodeSnapshot.mock.calls[0]![1].files)).toHaveLength(MAX_SNAPSHOT_FILES);
  expect(putCodeSnapshot.mock.calls[0]![1].partial).toContain("3 files past");
});

test("the snapshot is filed private, under the owner, on the parent it was told", async () => {
  getMissingBlobs.mockResolvedValue([]);
  await uploadWalk(RECORD, walk([file("a.ts", "aaa")]), {
    owner: "me@acme.co",
    parentRecordId: "22222222-2222-2222-2222-222222222222",
  });
  expect(putCodeSnapshot.mock.calls[0]![1]).toMatchObject({
    owner: "me@acme.co",
    private: true,
    parentRecordId: "22222222-2222-2222-2222-222222222222",
  });
});

test("progress counts the blobs actually sent, so a bar over already-stored files does not stall at zero", async () => {
  const files = [file("a.ts", "aaa"), file("b.ts", "bbb"), file("c.ts", "ccc")];
  getMissingBlobs.mockResolvedValue([files[0]!.sha256, files[2]!.sha256]);
  const seen: [number, number][] = [];
  await uploadWalk(RECORD, walk(files), { owner: "me@acme.co", onProgress: (sent, total) => seen.push([sent, total]) });
  // Totals are the blobs to send (2), not the files mapped (3), and it ends at the total.
  expect(seen[0]).toEqual([0, 2]);
  expect(seen.at(-1)).toEqual([2, 2]);
});
