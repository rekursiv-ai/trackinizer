import { QueryClientProvider, QueryObserver } from "@tanstack/react-query";
import { cleanup, renderHook, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, expect, test, vi } from "vitest";
import type { Ancestor } from "../api/inquiries";
import { stubFetch } from "../api/testing";
import { testClient, uuid } from "../live/testing";
import { AncestryLive, ancestryKey, useAncestry } from "./ancestry";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const parent = (n: number, child: string): Ancestor => ({
  id: uuid(n),
  kind: "Issue",
  seq: n,
  title: `Issue ${n}`,
  status: "active",
  child_ids: [child],
});

/** Answer each read with every id its filter names, each under parent 900, and a look-alike row it did not ask for. */
function serveAncestry() {
  return stubFetch((request) => {
    const filter = JSON.parse(new URL(request.url).searchParams.get("filter")!) as { value: string };
    const ends = filter.value.slice(1, -2).split("|");
    const rows = ends.map((end) => uuid(Number(end.slice(-7))));
    return Response.json([...rows.map((id) => ({ id, ancestors: [parent(900, id)] })), { id: uuid(999_999), ancestors: [] }]);
  });
}

function ancestryOf(ids: readonly string[] | null) {
  const client = testClient();
  const wrapper = ({ children }: { children: ReactNode }) => createElement(QueryClientProvider, { client }, children);
  return renderHook(() => useAncestry(ids), { wrapper });
}

test("a page's ancestry is one read of its ids: Issues, with ancestors=narrows and the id field alone", async () => {
  const sent = serveAncestry();
  const { result } = ancestryOf([uuid(2), uuid(1)]);
  await waitFor(() => expect(result.current.ancestry.size).toBe(2));
  expect(result.current.ancestry.get(uuid(1))).toEqual([parent(900, uuid(1))]);
  // A row whose id only ends like an asked one's is not taken.
  expect(result.current.ancestry.has(uuid(999_999))).toBe(false);
  expect(sent).toHaveLength(1);
  const query = new URLSearchParams(sent[0]!.query);
  expect(query.getAll("kind")).toEqual(["Issue"]);
  expect(query.getAll("fields")).toEqual(["id"]);
  expect(query.get("ancestors")).toBe("narrows");
  // Room for rows whose ids end alike, as the live layer's membership checks leave.
  expect(query.get("limit")).toBe("4");
  expect(JSON.parse(query.get("filter")!)).toMatchObject({ field: "id", op: "re" });
});

test("over 63 ids, the ancestry is read in parts the server's filter length allows", async () => {
  const sent = serveAncestry();
  const ids = Array.from({ length: 64 }, (_, n) => uuid(n + 1));
  const { result } = ancestryOf(ids);
  await waitFor(() => expect(result.current.ancestry.size).toBe(64));
  expect(sent.map((request) => new URLSearchParams(request.query).get("limit")).toSorted()).toEqual(["126", "2"]);
});

test("no ids reads nothing", async () => {
  const sent = serveAncestry();
  const { result } = ancestryOf(null);
  expect(result.current.pending).toBe(false);
  await Promise.resolve();
  expect(sent).toEqual([]);
});

/** An ancestry read on screen holding row 1 under parent 900, counting its fetches. */
function shownAncestry() {
  const client = testClient();
  let fetches = 0;
  const key = ancestryKey([uuid(1)]);
  const rows = [{ id: uuid(1), ancestors: [parent(900, uuid(1))] }];
  client.setQueryData(key, rows);
  new QueryObserver(client, {
    queryKey: key,
    queryFn: () => {
      fetches += 1;
      return rows;
    },
    staleTime: Infinity,
  }).subscribe(() => {});
  return { live: new AncestryLive(client), fetches: () => fetches };
}

test("a change to a row shown or to one of its ancestors refetches the ancestry, as a narrows edge changed writes both ends", async () => {
  const { live, fetches } = shownAncestry();
  await live.update({ ids: new Set([uuid(5)]), gap: false });
  expect(fetches()).toBe(0);
  await live.update({ ids: new Set([uuid(1)]), gap: false });
  expect(fetches()).toBe(1);
  await live.update({ ids: new Set([uuid(900)]), gap: false });
  expect(fetches()).toBe(2);
});

test("a gap refetches the ancestry", async () => {
  const { live, fetches } = shownAncestry();
  await live.update({ ids: new Set(), gap: true });
  expect(fetches()).toBe(1);
});
