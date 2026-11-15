// The code store (`/api/code/...`): a record's code, named by content.
//
// Reads: how two records' code differs, one file of a record's code, and a record's whole
// snapshot. Writes: the three steps an import takes — ask which blobs the store lacks, send
// those, then file the path-to-hash map. A blob is one text file keyed by the sha256 of its
// UTF-8 bytes and stored once per owner, so re-importing a tree the store holds sends nothing.
import { ApiError, type CallOptions, client, send, TIMEOUT_MS } from "./client";
import type { components } from "./generated/schema";

/** The paths whose code differs between two records, sorted, each with its unified diff. */
export type CodeDiff = components["schemas"]["CodeDiff"];
/** How one path differs: `--- a/<path>` and `+++ b/<path>` head its diff whatever the change. */
export type CodeFileDiff = components["schemas"]["CodeFileDiff"];
/** One file of a record's code, with its text. */
export type CodeFile = components["schemas"]["CodeFile"];
/** A record's snapshot and its files, sorted by path. */
export type CodeSnapshot = components["schemas"]["CodeSnapshot"];
/** One path of a snapshot: where it lives, which blob it is, and how big. */
export type CodeSnapshotFile = components["schemas"]["CodeSnapshotFile"];
/** A snapshot without its files; the owner's email is never sent. */
export type CodeSnapshotHead = components["schemas"]["CodeSnapshotHead"];

/**
 * How `to`'s code differs from `from`'s, path by path, with `GET /api/code/diff`
 * (3 lines of context). Null when either record has no snapshot the reader may
 * read: the server answers 404 for both, so a private snapshot is never revealed.
 */
export async function getCodeDiff(from: string, to: string, { signal }: CallOptions = {}): Promise<CodeDiff | null> {
  let diff: CodeDiff;
  try {
    diff = await send(TIMEOUT_MS.read, signal, (signal) => client.GET("/api/code/diff", { params: { query: { from, to } }, signal }));
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) return null;
    throw error;
  }
  if (!diff || !Array.isArray(diff.files) || typeof diff.more !== "boolean") throw new Error("Invalid code diff");
  return diff;
}

/** One file of a record's code, by its path, with `GET /api/code/records/{id}/file`; 404 when the snapshot maps no such path. */
export async function readCodeFile(recordId: string, path: string, { signal }: CallOptions = {}): Promise<CodeFile> {
  const file = await send(TIMEOUT_MS.read, signal, (signal) =>
    client.GET("/api/code/records/{record_id}/file", { params: { path: { record_id: recordId }, query: { path } }, signal }),
  );
  if (!file || typeof file.text !== "string" || typeof file.sha256 !== "string") throw new Error("Invalid code file");
  return file;
}

/**
 * A record's whole snapshot, with `GET /api/code/records/{id}/snapshot`. Null when the
 * record has none the reader may read — an unfiled record and an unreadable one both
 * answer 404, so a private snapshot is never revealed by the difference.
 */
export async function readCodeSnapshot(recordId: string, { signal }: CallOptions = {}): Promise<CodeSnapshot | null> {
  try {
    const snapshot = await send(TIMEOUT_MS.read, signal, (signal) =>
      client.GET("/api/code/records/{record_id}/snapshot", { params: { path: { record_id: recordId } }, signal }),
    );
    if (!snapshot || !Array.isArray(snapshot.files)) throw new Error("Invalid code snapshot");
    return snapshot;
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) return null;
    throw error;
  }
}

/**
 * Which of `sha256` the store lacks for `owner`, with `POST /api/code/blobs/missing` — the
 * first step of an import, and what keeps a re-import from sending bytes already stored.
 * At most `MAX_MISSING_HASHES` per call; `code/upload.ts` pages them.
 */
export async function getMissingBlobs(
  owner: string,
  sha256: readonly string[],
  { signal }: CallOptions = {},
): Promise<readonly string[]> {
  const answer = await send(TIMEOUT_MS.read, signal, (signal) =>
    client.POST("/api/code/blobs/missing", { body: { owner, sha256: [...sha256] }, signal }),
  );
  if (!answer || !Array.isArray(answer.missing)) throw new Error("Invalid missing-blobs answer");
  return answer.missing;
}

/**
 * Store blobs for `owner` with `POST /api/code/blobs`; one already stored is a no-op. The
 * server rechecks each hash against the text, so a mismatch is refused rather than stored.
 * Answers how many were new. Bounded per call by count and by total text; `code/upload.ts` packs them.
 */
export async function putCodeBlobs(
  owner: string,
  blobs: readonly { readonly sha256: string; readonly text: string }[],
  { signal }: CallOptions = {},
): Promise<number> {
  const answer = await send(TIMEOUT_MS.write, signal, (signal) =>
    client.POST("/api/code/blobs", {
      body: { owner, blobs: blobs.map(({ sha256, text }) => ({ sha256, text })) },
      signal,
    }),
  );
  if (!answer || typeof answer.stored !== "number") throw new Error("Invalid blobs-stored answer");
  return answer.stored;
}

/**
 * File `recordId`'s code with `PUT /api/code/records/{id}/snapshot`, replacing the snapshot
 * it had. Every blob must already be stored for `owner`, or the write is a 409
 * `code_blobs_missing`; a named `parentRecordId` must itself hold one, or a 409
 * `code_parent_missing`.
 */
export async function putCodeSnapshot(
  recordId: string,
  snapshot: {
    readonly owner: string;
    readonly private: boolean;
    readonly parentRecordId: string | null;
    readonly files: Readonly<Record<string, string>>;
    readonly partial?: string;
  },
  { signal }: CallOptions = {},
): Promise<CodeSnapshotHead> {
  const head = await send(TIMEOUT_MS.write, signal, (signal) =>
    client.PUT("/api/code/records/{record_id}/snapshot", {
      params: { path: { record_id: recordId } },
      body: {
        owner: snapshot.owner,
        private: snapshot.private,
        parent_record_id: snapshot.parentRecordId,
        files: { ...snapshot.files },
        partial: snapshot.partial ?? "",
      },
      signal,
    }),
  );
  if (!head || typeof head.file_count !== "number") throw new Error("Invalid code snapshot head");
  return head;
}
