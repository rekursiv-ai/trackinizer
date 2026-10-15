// Test helpers for the canvas's visuals; only tests import this file.
import { vi } from "vitest";
import { FILES_LABEL, FILES_OF, type FilesDoc } from "./filesDoc";
import type { WorkspaceActions } from "./workspaceActions";

/**
 * A canvas's operations for a view under test: idle, `operate` a mock, and no
 * visual types offered; `fields` override any of them.
 */
export function canvasActions(fields: Partial<WorkspaceActions> = {}): WorkspaceActions {
  return {
    busy: false,
    writeError: null,
    operate: vi.fn(),
    visualTypes: new Set(),
    ...fields,
  };
}

/** The record the files fixtures describe. */
export const FILES_RECORD = "5d1f0c1e-7a3b-4c2d-9e8f-0a1b2c3d4e5f";

/** The account a source files its documents with, which the profile `answerFiles` sends trusts. */
export const FILES_ACCOUNT = "files@service.invalid";

/** The diff git gives of the one file the fixture tree changed, with a line that ends the file unterminated. */
export const TREE_DIFF = [
  "diff --git a/attn.py b/attn.py",
  "index 1111111..2222222 100644",
  "--- a/attn.py",
  "+++ b/attn.py",
  "@@ -3,3 +3,3 @@ def tile():",
  " def tile():",
  "-    warps = 4",
  "+    warps = 2",
  "     return warps",
  "\\ No newline at end of file",
  "",
].join("\n");

/** A files document: three files, one change with its diff, one held back, one deleted unread. */
export const FILES: FilesDoc = {
  base: "origin/main",
  files: ["attn.py", "bench/run.py", "notes.txt"],
  more_files: false,
  changed: [
    { path: "attn.py", change: "modified", diff: TREE_DIFF, note: "" },
    { path: "bench/run.py", change: "added", diff: "", note: "withheld" },
    { path: "old.py", change: "deleted", diff: "", note: "omitted" },
  ],
  more_changes: false,
  hidden: 0,
  version: 1,
  kind: "files",
};

/**
 * A files document's row as `GET /api/inquiries` sends it: `document` in its
 * description, labelled for `record`, filed by `account`, last changed at `modified`.
 */
export function filesRow(
  document: object,
  { record = FILES_RECORD, account = FILES_ACCOUNT, modified = "2026-10-12T09:00:00+00:00" }: { record?: string; account?: string; modified?: string } = {},
) {
  return {
    id: `files ${modified}`,
    kind: "Artifact",
    seq: 900,
    title: "Files",
    status: "complete",
    owner: null,
    labels: [FILES_LABEL, `${FILES_OF}${record}`],
    account,
    description: JSON.stringify(document),
    marginal_cost: { agent_usd: 0, resource_usd: 0 },
    created: modified,
    modified,
  };
}

/**
 * The answer to a files read: the profile, which trusts `FILES_ACCOUNT`, or the
 * Artifacts `rows`; null for any other request.
 */
export function answerFiles(url: URL, rows: readonly unknown[] = []): Response | null {
  if (url.pathname === "/api/me/profile") return Response.json({ summary_accounts: [FILES_ACCOUNT] });
  if (url.pathname === "/api/inquiries" && url.searchParams.getAll("kind").includes("Artifact")) return Response.json(rows);
  return null;
}

/** A file of 30 lines, `line 1` to `line 30`: the new side of `TWO_HUNKS`. */
export const THIRTY = Array.from({ length: 30 }, (_, k) => `line ${k + 1}`);

/**
 * Two hunks of that file at 3 lines of context: the old side lacked line 9, so its
 * numbers run one behind until line 25 changed.
 */
export const TWO_HUNKS = [
  "@@ -6,6 +6,7 @@",
  " line 6",
  " line 7",
  " line 8",
  "+line 9",
  " line 10",
  " line 11",
  " line 12",
  "@@ -21,7 +22,7 @@",
  " line 22",
  " line 23",
  " line 24",
  "-old 25",
  "+line 25",
  " line 26",
  " line 27",
  " line 28",
];
