import { afterEach, expect, test, vi } from "vitest";
import { FILES, FILES_ACCOUNT, FILES_RECORD, filesRow } from "../visuals/testing";
import { ApiError } from "./client";
import { loadFiles } from "./files";
import { stubFetch } from "./testing";

afterEach(() => vi.unstubAllGlobals());

/** The account the record is filed under: a person, not a service account the profile trusts. */
const OWNER = "ada@example.com";

/**
 * Answer the list with `rows`, and the record itself with `record`: the account it
 * is filed under, or a status when it cannot be read.
 */
function serve(rows: readonly unknown[], record: string | number = OWNER) {
  return stubFetch((request) => {
    const { pathname } = new URL(request.url);
    if (pathname === "/api/inquiries") return Response.json(rows);
    if (pathname !== `/api/inquiries/${FILES_RECORD}`) return Response.json({ detail: "not found" }, { status: 404 });
    if (typeof record === "number") return Response.json({ detail: "record unavailable" }, { status: record });
    return Response.json({ id: FILES_RECORD, kind: "AgentSession", account: record });
  });
}

test("a record's files are asked for by the files label, the record's, and each extra label, as Artifacts, and the record is not read", async () => {
  const sent = serve([filesRow(FILES)]);

  expect(await loadFiles(FILES_RECORD, [FILES_ACCOUNT], { labels: ["campaign:c"] })).toEqual({ doc: FILES, count: 1 });

  expect(sent.map(({ method, path }) => [method, path])).toEqual([["GET", "/api/inquiries"]]);
  const query = new URLSearchParams(sent[0]!.query);
  expect(query.getAll("kind")).toEqual(["Artifact"]);
  expect(query.has("fields")).toBe(false);
  expect(query.getAll("filter").map((each) => JSON.parse(each) as unknown)).toEqual([
    { field: "labels", op: "is", value: "files" },
    { field: "labels", op: "is", value: `files:of:${FILES_RECORD}` },
    { field: "labels", op: "is", value: "campaign:c" },
  ]);
});

test("a document another account plants on someone else's record is not read, though labelled alike", async () => {
  serve([filesRow({ ...FILES, files: ["planted.py"] }, { account: "mallory@example.com", modified: "2026-10-13T00:00:00+00:00" }), filesRow(FILES)]);

  expect(await loadFiles(FILES_RECORD, [FILES_ACCOUNT])).toEqual({ doc: FILES, count: 1 });
});

test("a document the record's own account filed is read, though no service account filed it", async () => {
  const sent = serve([filesRow({ ...FILES, base: "my-branch" }, { account: OWNER })]);

  const found = await loadFiles(FILES_RECORD, [FILES_ACCOUNT]);

  expect([found?.doc.base, found?.count]).toEqual(["my-branch", 1]);
  expect(sent.map(({ method, path }) => [method, path])).toEqual([
    ["GET", "/api/inquiries"],
    ["GET", `/api/inquiries/${FILES_RECORD}`],
  ]);
});

test("a record the reader may not read vouches for no account", async () => {
  serve([filesRow(FILES, { account: OWNER })], 404);

  expect(await loadFiles(FILES_RECORD, [FILES_ACCOUNT])).toBeNull();
});

test("a record that fails to read fails the read, rather than dropping its owner's document", async () => {
  serve([filesRow(FILES, { account: OWNER })], 500);

  await expect(loadFiles(FILES_RECORD, [FILES_ACCOUNT])).rejects.toBeInstanceOf(ApiError);
});

test("of several documents the newest changed wins, whatever the order, and all readable ones are counted", async () => {
  const older = filesRow({ ...FILES, base: "older" }, { modified: "2026-10-12T08:00:00+00:00" });
  const newest = filesRow({ ...FILES, base: "newest" }, { modified: "2026-10-12T10:00:00+00:00" });
  const middle = filesRow({ ...FILES, base: "middle" }, { modified: "2026-10-12T09:00:00+00:00" });
  const unreadable = filesRow({ ...FILES, version: 2 }, { modified: "2026-10-12T11:00:00+00:00" });
  stubFetch(() => Response.json([older, unreadable, newest, middle]));

  const found = await loadFiles(FILES_RECORD, [FILES_ACCOUNT]);

  expect([found?.doc.base, found?.count]).toEqual(["newest", 3]);
});

test("a record with no readable document has none", async () => {
  stubFetch(() => Response.json([filesRow({ kind: "session", version: 1 })]));
  expect(await loadFiles(FILES_RECORD, [FILES_ACCOUNT])).toBeNull();
  stubFetch(() => Response.json([]));
  expect(await loadFiles(FILES_RECORD, [FILES_ACCOUNT])).toBeNull();
});
