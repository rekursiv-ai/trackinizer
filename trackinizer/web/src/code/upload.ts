// Sending a walked folder to the code store: ask, send the gap, file the map.
//
// Three bounds shape this and none may be crossed, since each is a 422 that loses the whole
// import: a missing-blobs question asks about at most `MAX_MISSING_HASHES`, a blobs request
// carries at most `MAX_BLOBS_PER_REQUEST` of at most `MAX_REQUEST_TEXT_BYTES`, and a snapshot
// maps at most `MAX_SNAPSHOT_FILES`. So the question is asked in pages and the blobs are
// packed into requests that fit by BOTH count and bytes.
//
// The saving is the whole reason the store is content-addressed: a second import of the same
// tree asks about every hash and sends none of them.
import { getMissingBlobs, putCodeBlobs, putCodeSnapshot } from "../api/code";
import type { CallOptions } from "../api/client";
import type { ReadFile, Walked } from "./importFolder";
import { MAX_BLOBS_PER_REQUEST, MAX_MISSING_HASHES, MAX_REQUEST_TEXT_BYTES, MAX_SNAPSHOT_FILES } from "./limits";

/** What an import did, for the line the pane shows afterwards. */
export type Uploaded = {
  /** Files the snapshot maps. */
  readonly files: number;
  /** Blobs the store did not already hold, which is what the upload actually sent. */
  readonly sent: number;
  /** Files left out of the snapshot because it was already full. */
  readonly dropped: number;
};

/** `items` in runs of at most `size`. */
function pages<T>(items: readonly T[], size: number): readonly (readonly T[])[] {
  const out: T[][] = [];
  for (let at = 0; at < items.length; at += size) out.push(items.slice(at, at + size));
  return out;
}

/**
 * Which of `files`' hashes the store lacks for `owner`, asked in pages so no one question
 * passes `MAX_MISSING_HASHES`. Each distinct hash is asked once, however many paths hold it.
 */
async function missingOf(owner: string, files: readonly ReadFile[], options: CallOptions): Promise<ReadonlySet<string>> {
  const distinct = [...new Set(files.map(({ sha256 }) => sha256))];
  const missing = new Set<string>();
  for (const page of pages(distinct, MAX_MISSING_HASHES)) {
    for (const hash of await getMissingBlobs(owner, page, options)) missing.add(hash);
  }
  return missing;
}

/**
 * Pack `blobs` into requests that fit both bounds. A single blob is never split — it is
 * already capped at `MAX_BLOB_BYTES`, an eighth of a request's text — so a blob that would
 * pass the byte bound starts the next request instead.
 */
function packed(blobs: readonly ReadFile[]): readonly (readonly ReadFile[])[] {
  const out: ReadFile[][] = [];
  let batch: ReadFile[] = [];
  let bytes = 0;
  for (const blob of blobs) {
    const size = new TextEncoder().encode(blob.text).length;
    if (batch.length > 0 && (batch.length >= MAX_BLOBS_PER_REQUEST || bytes + size > MAX_REQUEST_TEXT_BYTES)) {
      out.push(batch);
      batch = [];
      bytes = 0;
    }
    batch.push(blob);
    bytes += size;
  }
  if (batch.length > 0) out.push(batch);
  return out;
}

/**
 * Store `walked`'s files for `owner` and file them as `record`'s snapshot, building on
 * `parentRecordId` so the store can diff this import against what came before.
 *
 * `onProgress` is called with the blobs sent so far and the total to send, which is what
 * the pane's bar reads; a big first import is many requests and must not look stalled.
 */
export async function uploadWalk(
  record: string,
  walked: Walked,
  {
    owner,
    parentRecordId = null,
    private: isPrivate = true,
    partial = "",
    onProgress,
    signal,
  }: CallOptions & {
    readonly owner: string;
    readonly parentRecordId?: string | null;
    readonly private?: boolean;
    readonly partial?: string;
    readonly onProgress?: (sent: number, total: number) => void;
  },
): Promise<Uploaded> {
  // A snapshot maps at most `MAX_SNAPSHOT_FILES`; past that the import keeps the first
  // paths in sorted order and says so, rather than failing at the last step.
  const kept = walked.files.slice(0, MAX_SNAPSHOT_FILES);
  const dropped = walked.files.length - kept.length;
  const missing = await missingOf(owner, kept, { signal });
  // One blob per distinct hash: a repo with twenty empty `__init__.py` sends one.
  const byHash = new Map<string, ReadFile>();
  for (const file of kept) if (missing.has(file.sha256)) byHash.set(file.sha256, file);
  const blobs = [...byHash.values()];
  let sent = 0;
  onProgress?.(0, blobs.length);
  for (const batch of packed(blobs)) {
    await putCodeBlobs(owner, batch, { signal });
    sent += batch.length;
    onProgress?.(sent, blobs.length);
  }
  const files: Record<string, string> = {};
  for (const file of kept) files[file.path] = file.sha256;
  const note = [partial, dropped > 0 ? `${dropped} files past the ${MAX_SNAPSHOT_FILES}-file limit were left out` : ""]
    .filter(Boolean)
    .join("; ");
  await putCodeSnapshot(record, { owner, private: isPrivate, parentRecordId, files, partial: note }, { signal });
  return { files: kept.length, sent: blobs.length, dropped };
}
