import { afterEach, expect, test, vi } from "vitest";
import { ApiError, TIMEOUT_MS } from "./client";
import { searchInquiries } from "./search";
import { stubFetch } from "./testing";

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

test("a search is a bodiless GET of one kind, with the query as typed, backslashes kept", async () => {
  const hit = { id: "0b6f7c1e-2f7a-4c55-9d7e-1f0e6b1d2a33", kind: "Issue", seq: 7, title: "Retry 42" };
  const sent = stubFetch(() => Response.json([hit]));
  expect(await searchInquiries({ q: 'title:\\d+ "two words"', kind: "Issue", limit: 5 })).toEqual([hit]);
  expect(sent).toHaveLength(1);
  expect(sent[0]).toMatchObject({ method: "GET", path: "/api/web/search", headers: {}, body: undefined });
  expect(Object.fromEntries(new URLSearchParams(sent[0]!.query))).toEqual({
    q: 'title:\\d+ "two words"',
    kind: "Issue",
    limit: "5",
  });
});

test("a search with no kind asks every kind in one request", async () => {
  const hits = [
    { id: "0b6f7c1e-2f7a-4c55-9d7e-1f0e6b1d2a33", kind: "Belief", seq: 9 },
    { id: "1c7a8d2f-3a8b-4d66-8e9f-2a1b7c2e3b44", kind: "Issue", seq: 7 },
  ];
  const sent = stubFetch(() => Response.json(hits));
  expect(await searchInquiries({ q: "retry", limit: 50 })).toEqual(hits);
  expect(sent).toHaveLength(1);
  expect(Object.fromEntries(new URLSearchParams(sent[0]!.query))).toEqual({ q: "retry", limit: "50" });
});

test("a search can name the only keys its hits carry, one fields param each", async () => {
  const hit = { id: "0b6f7c1e-2f7a-4c55-9d7e-1f0e6b1d2a33", title: "Retry 42", judgement: null };
  const sent = stubFetch(() => Response.json([hit]));
  const hits = await searchInquiries({ q: "retry", kind: "Issue", limit: 5, fields: ["id", "title", "judgement"] });
  expect(hits).toEqual([hit]);
  expect(new URLSearchParams(sent[0]!.query).getAll("fields")).toEqual(["id", "title", "judgement"]);
  // A hit is typed with the keys asked for alone.
  // @ts-expect-error
  expect(hits[0]!.status).toBeUndefined();
});

test("a search over the server's time budget is an ApiError with the server's message", async () => {
  const detail = "query exceeded the time budget; narrow the filters or add more specific terms";
  stubFetch(() => Response.json({ detail }, { status: 400 }));
  const error = await searchInquiries({ q: "slow", kind: "Issue", limit: 5 }).catch((caught: unknown) => caught);
  expect(error).toBeInstanceOf(ApiError);
  expect(error).toMatchObject({ status: 400, detail });
});

test("a search that gets no answer fails at the search timeout, not the read timeout", async () => {
  vi.useFakeTimers();
  stubFetch(
    (request) =>
      new Promise((_, reject) => request.signal.addEventListener("abort", () => reject(request.signal.reason))),
  );
  const failed = searchInquiries({ q: "slow", kind: "Issue", limit: 5 }).catch((caught: unknown) => caught);
  await vi.advanceTimersByTimeAsync(TIMEOUT_MS.search);
  expect(await failed).toMatchObject({ status: 0, code: "timeout" });
});

test("a kind this build does not know fails before any request, rather than as the server's 422 (R2-X3)", async () => {
  const sent = stubFetch(() => Response.json([]));
  await expect(searchInquiries({ q: "x", kind: "Ticket", limit: 5 })).rejects.toThrow(
    "This build does not know the inquiry kind Ticket; reload to get the server's build.",
  );
  expect(sent).toEqual([]);
});
