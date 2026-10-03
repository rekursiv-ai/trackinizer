import { afterEach, expect, test, vi } from "vitest";
import { listChanges } from "./changes";
import { stubFetch } from "./testing";

afterEach(() => {
  vi.unstubAllGlobals();
});

test("a first page is a GET of one change kind and a limit, with no body", async () => {
  const sent = stubFetch(() => Response.json([]));
  expect(await listChanges({ kind: "status", limit: 50 })).toEqual([]);
  expect(sent).toEqual([
    { method: "GET", path: "/api/change_log", query: "?kind=status&limit=50", headers: {}, body: undefined },
  ]);
});

test("a later page names the last change shown; a catch-up names a time", async () => {
  const sent = stubFetch(() => Response.json([]));
  const last = "00000000-0000-4000-8000-000000000007";
  await listChanges({ kind: "edge_added", afterId: last, limit: 50 });
  await listChanges({ kind: "created", since: "2026-09-26T10:00:00+00:00", limit: 50 });
  expect(sent.map((request) => new URLSearchParams(request.query))).toEqual([
    new URLSearchParams({ kind: "edge_added", after_id: last, limit: "50" }),
    new URLSearchParams({ kind: "created", since: "2026-09-26T10:00:00+00:00", limit: "50" }),
  ]);
});

test("a page of several kinds repeats kind, in one request", async () => {
  const sent = stubFetch(() => Response.json([]));
  await listChanges({ kind: ["title", "issue_priority", "labels"], limit: 50 });
  expect(sent).toHaveLength(1);
  expect(new URLSearchParams(sent[0]!.query).getAll("kind")).toEqual(["title", "issue_priority", "labels"]);
  // A kind outside the schema's set does not compile.
  // @ts-expect-error
  await listChanges({ kind: ["title", "renamed"], limit: 50 });
});

test("a brief page asks for snapshots with their set keys alone and text cut short", async () => {
  const sent = stubFetch(() => Response.json([]));
  await listChanges({ kind: "description", limit: 50, brief: true });
  expect(sent[0]).toMatchObject({ method: "GET", path: "/api/change_log", body: undefined });
  expect(new URLSearchParams(sent[0]!.query)).toEqual(
    new URLSearchParams({ kind: "description", limit: "50", brief: "true" }),
  );
});

test("a cursor the server no longer has fails with its message", async () => {
  stubFetch(() => Response.json({ detail: "change 1 not found", code: "not_found" }, { status: 404 }));
  await expect(listChanges({ kind: "status", afterId: "1", limit: 50 })).rejects.toMatchObject({
    status: 404,
    detail: "change 1 not found",
  });
});
