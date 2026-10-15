// The code store's reads (`/api/code/...`): how two records' code differs, and one file of a record's code.
import { ApiError, type CallOptions, client, send, TIMEOUT_MS } from "./client";
import type { components } from "./generated/schema";

/** The paths whose code differs between two records, sorted, each with its unified diff. */
export type CodeDiff = components["schemas"]["CodeDiff"];
/** How one path differs: `--- a/<path>` and `+++ b/<path>` head its diff whatever the change. */
export type CodeFileDiff = components["schemas"]["CodeFileDiff"];
/** One file of a record's code, with its text. */
export type CodeFile = components["schemas"]["CodeFile"];

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
